"""
Alpaca Markets real-time market data provider.

Why Alpaca instead of Polygon free tier:
  - Polygon free = 15-minute DELAYED data → signals fire on stale prices
  - Alpaca free  = REAL-TIME data with a paper trading account

Setup (free, takes 5 minutes):
  1. Go to https://alpaca.markets
  2. Create a free account (no money required)
  3. Choose "Paper Trading" account
  4. Go to API Keys → Generate
  5. Copy your API Key and API Secret
  6. Add to .env:
       ALPACA_API_KEY=your_key
       ALPACA_API_SECRET=your_secret

Alpaca's free tier includes:
  - Real-time 1-minute and 5-minute bars for all US equities
  - Real-time quotes (bid/ask/last)
  - Market status endpoint
  - IEX feed only (single-exchange volume); calls ARE rate limited (HTTP 429),
    so _get() retries a bounded number of times honouring Retry-After

Note: Polygon is STILL used for backtesting (historical data).
      Alpaca is used for LIVE signal generation only.
      You need BOTH keys:
        - ALPACA_API_KEY / ALPACA_API_SECRET  → live signals (real-time)
        - POLYGON_API_KEY                     → backtesting (historical)
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()


class AlpacaError(Exception):
    """Raised on Alpaca API errors."""


@dataclass
class AlpacaBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None


@dataclass
class AlpacaQuote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: int
    timestamp: datetime
    is_delayed: bool = False  # Always False for Alpaca


class OpeningRangeBars(list):  # type: ignore[type-arg]
    """Opening-range bars plus whether the window is known to be complete.

    A plain ``list`` subclass so existing callers keep working. ``complete`` is
    True only when the window has closed (now >= window end + 1 minute, so the
    last minute bar has been published) AND at least ``min_bars`` bars arrived.
    A partial window (fetched mid-window, or with gaps) has ``complete=False``
    and callers must not derive a final opening range from it. A market holiday
    or a symbol with no data yields an EMPTY result with ``complete=False``;
    use the exchange calendar to tell a holiday from an outage.
    """

    complete: bool
    expected_bars: int
    min_bars: int
    window_end: datetime | None

    def __init__(
        self,
        bars: Any = (),
        *,
        complete: bool = False,
        expected_bars: int = 0,
        min_bars: int = 0,
        window_end: datetime | None = None,
    ) -> None:
        super().__init__(bars)
        self.complete = complete
        self.expected_bars = expected_bars
        self.min_bars = min_bars
        self.window_end = window_end


class AlpacaMarketDataProvider:
    """
    Real-time US equity market data via Alpaca's free data API.

    Uses the IEX data feed which is free with any Alpaca account
    (including free paper trading accounts — no real money needed).

    All data is real-time during market hours.
    """

    # Alpaca data API base URL (separate from trading API)
    DATA_BASE_URL = "https://data.alpaca.markets"

    # Cache TTL — short because data is real-time
    CACHE_TTL_SECONDS = 30  # 30 seconds for live data

    # An opening window is "complete" with at least this share of its 1-minute
    # bars (IEX can omit illiquid minutes) once the window has closed.
    OPENING_RANGE_MIN_BAR_FRACTION = 0.8
    # Grace after the window end before the last bar is considered published.
    OPENING_RANGE_SETTLE = timedelta(minutes=1)

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.ALPACA_API_KEY
        self._api_secret = api_secret or settings.ALPACA_API_SECRET

        if not self._api_key or not self._api_secret:
            raise ValueError(
                "Alpaca API credentials not set.\n"
                "Get free real-time data:\n"
                "  1. Go to https://alpaca.markets\n"
                "  2. Create a free paper trading account\n"
                "  3. Generate API Keys\n"
                "  4. Add ALPACA_API_KEY and ALPACA_API_SECRET to .env"
            )

        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    async def __aenter__(self) -> AlpacaMarketDataProvider:
        self._client = httpx.AsyncClient(
            base_url=self.DATA_BASE_URL,
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._api_secret,
            },
            timeout=15.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError(
                "Use as async context manager: async with AlpacaMarketDataProvider() as md:"
            )
        return self._client

    def _cached(self, key: str) -> Any | None:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.monotonic() - ts < self.CACHE_TTL_SECONDS:
                return val
        return None

    def _cache_set(self, key: str, val: Any) -> None:
        self._cache[key] = (time.monotonic(), val)

    # 429 handling: at most MAX_ATTEMPTS requests in total.
    MAX_ATTEMPTS = 3

    # Bars pagination: Alpaca caps a page at 10000 rows.
    MAX_PAGE_SIZE = 10_000
    MAX_PAGES = 10
    BACKOFF_BASE_SECONDS = 1.0
    MAX_BACKOFF_SECONDS = 30.0
    # Hard cap on the TOTAL time one ``_get`` call may spend sleeping on 429s.
    # Non-exit callers use this budget. Protective-exit fetches (position
    # monitor) pass ``max_total_sleep=EXIT_MAX_TOTAL_SLEEP_SECONDS`` instead so a
    # rate-limited exit-path fetch stalls at most ~10 s (plus request time)
    # before it raises; a bounded stall beats hammering a rate-limited API.
    MAX_TOTAL_SLEEP_SECONDS = 45.0
    EXIT_MAX_TOTAL_SLEEP_SECONDS = 10.0

    # Injectable wall clock (tz-aware UTC) so tests are deterministic.
    _now = staticmethod(lambda: datetime.now(UTC))

    # Injectable so tests never really sleep / are deterministic.
    _sleep = staticmethod(asyncio.sleep)
    # Returns a float in [0, 1); used for equal-jitter on computed backoff.
    _random = staticmethod(random.random)

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            value = float(raw.strip())
        except ValueError:
            return None  # HTTP-date form is not honoured; fall back to backoff
        if not math.isfinite(value) or value < 0:
            return None
        return value

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_total_sleep: float | None = None,
    ) -> Any:
        budget = (
            self.MAX_TOTAL_SLEEP_SECONDS
            if max_total_sleep is None
            else min(max_total_sleep, self.MAX_TOTAL_SLEEP_SECONDS)
        )
        resp: httpx.Response | None = None
        slept = 0.0
        for attempt in range(self.MAX_ATTEMPTS):
            resp = await self._http.get(path, params=params)
            if resp.status_code != 429 or attempt + 1 >= self.MAX_ATTEMPTS:
                break
            remaining = min(self.MAX_BACKOFF_SECONDS, budget - slept)
            if remaining <= 0:
                break  # sleep budget exhausted: stop retrying, raise below
            delay = self._retry_after_seconds(resp)
            if delay is None:
                # Equal jitter: half fixed, half random, so concurrent callers
                # do not retry in lockstep.
                base = self.BACKOFF_BASE_SECONDS * (2**attempt)
                delay = min(base / 2 + (base / 2) * self._random(), remaining)
            elif delay > remaining:
                # Server-supplied Retry-After is the server's instruction: if it
                # does not fit the remaining budget, retrying early would just
                # be another 429, so give up now instead of ignoring it.
                break
            if delay > 0:
                await self._sleep(delay)
                slept += delay
        assert resp is not None

        if resp.status_code == 401:
            raise AlpacaError(
                "Invalid Alpaca API credentials. Check ALPACA_API_KEY and ALPACA_API_SECRET in .env"
            )
        if resp.status_code == 403:
            raise AlpacaError(
                "Alpaca account does not have data access. "
                "Make sure you have a paper trading account at alpaca.markets"
            )
        if resp.status_code == 422:
            raise AlpacaError(f"Alpaca rejected request: {resp.text[:200]}")
        if resp.status_code == 429:
            raise AlpacaError(
                "Alpaca rate limit hit (HTTP 429); retries exhausted or the "
                "retry-sleep budget was used up — reduce request frequency"
            )

        resp.raise_for_status()
        return resp.json()

    # ── Real-time bars ────────────────────────────────────────────────────────

    @staticmethod
    def _lookback_days(multiplier: int, timespan: str, limit: int) -> int:
        """Calendar days to look back so ``limit`` bars fit, covering weekends/holidays."""
        mult = max(multiplier, 1)
        if timespan == "day":
            trading_days = limit * mult
        elif timespan == "hour":
            trading_days = math.ceil(limit / (6.5 / mult))
        else:
            trading_days = math.ceil(limit / max(1, 390 // mult))
        # 7/5 for weekends plus slack for holidays; never below the old 5 days.
        return max(5, math.ceil(trading_days * 7 / 5) + 5)

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 5,
        timespan: str = "minute",
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
        sort: str = "desc",
        start: datetime | None = None,
        end: datetime | None = None,
        use_cache: bool = True,
        max_total_sleep: float | None = None,
    ) -> list[AlpacaBar]:
        """
        Fetch aggregated bars (OHLCV), always returned oldest -> newest.

        ``sort="desc"`` (default) is newest-N; ``sort="asc"`` is start-anchored
        (the FIRST ``limit`` bars at/after the window start - use it for
        "the opening window of a given day"). ``start`` / ``end`` (tz-aware
        datetimes) override the date-based window with an exact UTC range.

        With the default sort this returns the LATEST ``limit`` bars: the request is
        sorted descending (so the limit truncates the OLD end), pages are
        followed via ``next_page_token`` (page size capped at
        ``MAX_PAGE_SIZE``, at most ``MAX_PAGES`` pages), then the result is
        trimmed to ``limit`` and reversed. Without explicit dates the window
        is a lookback sized to the timeframe; with explicit ``from_date`` /
        ``to_date`` it is [from_date, to_date].

        timespan options: "1Min", "5Min", "15Min", "1Hour", "1Day"
        """
        # Default newest-first on the wire so ``limit`` truncates the OLD end;
        # "asc" is the explicit start-anchored mode.
        if sort not in ("asc", "desc"):
            raise ValueError(f"sort must be 'asc' or 'desc', got {sort!r}")
        _fmt = "%Y-%m-%dT%H:%M:%SZ"
        start_s = start.astimezone(UTC).strftime(_fmt) if start else "-"
        end_s = end.astimezone(UTC).strftime(_fmt) if end else "-"
        cache_key = (
            f"bars:{ticker}:{multiplier}:{timespan}:"
            f"{from_date.isoformat() if from_date else '-'}:"
            f"{to_date.isoformat() if to_date else '-'}:{start_s}:{end_s}:{limit}:{sort}"
        )
        cached = self._cached(cache_key) if use_cache else None
        if cached is not None:
            return cached

        # Alpaca timeframe format: "5Min", "1Min", "15Min"
        tf_map = {
            (1, "minute"): "1Min",
            (5, "minute"): "5Min",
            (15, "minute"): "15Min",
            (1, "hour"): "1Hour",
            (1, "day"): "1Day",
        }
        timeframe = tf_map.get((multiplier, timespan), f"{multiplier}Min")

        today = datetime.now(UTC).date()
        start_day = from_date or (
            today - timedelta(days=self._lookback_days(multiplier, timespan, limit))
        )
        start_dt = start_s if start else start_day.isoformat() + "T00:00:00Z"
        end_dt = end_s if end else (to_date or today).isoformat() + "T23:59:59Z"

        base_params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": start_dt,
            "end": end_dt,
            "adjustment": "raw",
            "feed": "iex",  # IEX = free real-time feed
            "sort": sort,
        }
        raw_bars: list[dict[str, Any]] = []
        page_token: str | None = None
        for page_no in range(self.MAX_PAGES):
            remaining = limit - len(raw_bars)
            if remaining <= 0:
                break
            params = {**base_params, "limit": min(remaining, self.MAX_PAGE_SIZE)}
            if page_token:
                params["page_token"] = page_token
            data = await self._get(
                f"/v2/stocks/{ticker}/bars", params=params, max_total_sleep=max_total_sleep
            )
            raw_bars.extend(data.get("bars") or [])
            page_token = data.get("next_page_token")
            if not page_token:
                break
            if page_no + 1 >= self.MAX_PAGES and len(raw_bars) < limit:
                log.warning(
                    "alpaca.bars_pagination_truncated",
                    ticker=ticker,
                    pages=self.MAX_PAGES,
                    bars_returned=len(raw_bars),
                    limit=limit,
                )
        raw_bars = raw_bars[:limit]
        if sort == "desc":
            raw_bars.reverse()  # newest-first -> callers always receive ascending order

        bars: list[AlpacaBar] = []
        for b in raw_bars:
            ts_str = b.get("t", "")
            # Parse ISO timestamp
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            bars.append(
                AlpacaBar(
                    timestamp=ts,
                    open=Decimal(str(b["o"])),
                    high=Decimal(str(b["h"])),
                    low=Decimal(str(b["l"])),
                    close=Decimal(str(b["c"])),
                    volume=Decimal(str(b.get("v", 0))),
                    vwap=Decimal(str(b["vw"])) if b.get("vw") else None,
                )
            )

        if use_cache:
            self._cache_set(cache_key, bars)
        return bars

    async def get_opening_range_bars(
        self,
        ticker: str,
        session_date: date | None = None,
        orb_minutes: int = 15,
    ) -> OpeningRangeBars:
        """
        Fetch 1-minute bars covering the opening range window.
        Used by ORB strategy to compute opening range high/low.
        Real-time — no delay.

        Returns ``OpeningRangeBars`` (a list); check ``.complete`` before using
        it as a FINAL range. While the window is still open (before window end
        + 1 minute) the result is partial by definition and is NOT cached, so
        the next call sees the newly published bars instead of a 30 s-old
        partial window. Holiday / no data = empty, ``complete=False``.
        """
        # The exchange session date is the America/New_York calendar date, NOT the
        # server-local date (which differs from ET after 20:00 ET when the server
        # runs in UTC, and around midnight on any non-ET host).
        target_date = session_date or self._now().astimezone(ZoneInfo("America/New_York")).date()
        # 09:30 ET converted to UTC (DST-correct: 13:30Z summer, 14:30Z winter).
        session_open_utc = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            9,
            30,
            tzinfo=ZoneInfo("America/New_York"),
        ).astimezone(UTC)
        cutoff = session_open_utc + timedelta(minutes=orb_minutes)
        # Start-anchored ascending fetch of exactly the opening window, so the
        # result never depends on how late in the day it is requested.
        window_settled = self._now() >= cutoff + self.OPENING_RANGE_SETTLE
        bars = await self.get_bars(
            ticker,
            multiplier=1,
            timespan="minute",
            limit=orb_minutes + 10,
            sort="asc",
            start=session_open_utc,
            end=cutoff,
            use_cache=window_settled,  # never cache a still-forming window
        )
        in_window = [b for b in bars if session_open_utc <= b.timestamp < cutoff]
        min_bars = max(1, math.ceil(orb_minutes * self.OPENING_RANGE_MIN_BAR_FRACTION))
        complete = window_settled and len(in_window) >= min_bars
        if window_settled and not complete:
            log.warning(
                "alpaca.opening_range_incomplete",
                ticker=ticker,
                bars=len(in_window),
                min_bars=min_bars,
                session_date=target_date.isoformat(),
            )
        return OpeningRangeBars(
            in_window,
            complete=complete,
            expected_bars=orb_minutes,
            min_bars=min_bars,
            window_end=cutoff,
        )

    # ── Latest quote ──────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> AlpacaQuote:
        """
        Fetch the latest real-time trade and quote for a ticker.
        Uses IEX feed — no delay, no extra cost.
        """
        cache_key = f"quote:{ticker}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        data = await self._get(
            f"/v2/stocks/{ticker}/quotes/latest",
            params={"feed": "iex"},
        )
        q = data.get("quote", {})

        # Also get latest trade for last price
        trade_data = await self._get(
            f"/v2/stocks/{ticker}/trades/latest",
            params={"feed": "iex"},
        )
        trade = trade_data.get("trade", {})

        last_price = Decimal(str(trade.get("p") or q.get("ap") or q.get("bp") or 0))
        bid = Decimal(str(q.get("bp") or last_price))
        ask = Decimal(str(q.get("ap") or last_price))

        ts_str = trade.get("t") or q.get("t", "")
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else datetime.now(UTC)

        quote = AlpacaQuote(
            ticker=ticker,
            bid=bid,
            ask=ask,
            last=last_price,
            volume=int(trade.get("s", 0)),
            timestamp=ts,
            is_delayed=False,  # Always real-time on Alpaca
        )
        self._cache_set(cache_key, quote)
        return quote

    # ── Market status ─────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        """Check if US equities market is currently open (real-time)."""
        cache_key = "market_open"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            data = await self._get("/v1/clock")
            is_open = bool(data.get("is_open", False))
        except Exception:
            # Fallback to UTC time check
            now = datetime.now(UTC)
            is_open = (
                now.weekday() < 5
                and not (now.hour == 13 and now.minute < 30)
                and 13 * 60 + 30 <= now.hour * 60 + now.minute < 20 * 60
            )

        self._cache_set(cache_key, is_open)
        return is_open

    async def get_snapshot(self, ticker: str) -> dict[str, Any]:
        """
        Full market snapshot for a ticker.
        Includes latest trade, quote, daily stats, and previous close.
        Useful for morning scanner.
        """
        cache_key = f"snapshot:{ticker}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        data = await self._get(
            f"/v2/stocks/{ticker}/snapshot",
            params={"feed": "iex"},
        )
        self._cache_set(cache_key, data)
        return data

    async def get_snapshots(self, tickers: list[str]) -> dict[str, Any]:
        """
        Batch snapshot for multiple tickers in one API call.
        Much more efficient than calling get_snapshot() in a loop.
        Used by morning scanner.
        """
        if not tickers:
            return {}

        data = await self._get(
            "/v2/stocks/snapshots",
            params={
                "symbols": ",".join(tickers),
                "feed": "iex",
            },
        )
        return data  # dict of {ticker: snapshot_data}

    def validate_staleness(self, quote: AlpacaQuote, max_age_seconds: int = 60) -> bool:
        """Return True if quote data is fresh enough to trade on."""
        age = (datetime.now(UTC) - quote.timestamp).total_seconds()
        return age <= max_age_seconds
