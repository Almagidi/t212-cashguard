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
  - No rate limits on IEX data feed (unlimited calls)

Note: Polygon is STILL used for backtesting (historical data).
      Alpaca is used for LIVE signal generation only.
      You need BOTH keys:
        - ALPACA_API_KEY / ALPACA_API_SECRET  → live signals (real-time)
        - POLYGON_API_KEY                     → backtesting (historical)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import settings


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

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._api_key    = api_key    or settings.ALPACA_API_KEY
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

    async def __aenter__(self) -> "AlpacaMarketDataProvider":
        self._client = httpx.AsyncClient(
            base_url=self.DATA_BASE_URL,
            headers={
                "APCA-API-KEY-ID":     self._api_key,
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
            raise RuntimeError("Use as async context manager: async with AlpacaMarketDataProvider() as md:")
        return self._client

    def _cached(self, key: str) -> Any | None:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.monotonic() - ts < self.CACHE_TTL_SECONDS:
                return val
        return None

    def _cache_set(self, key: str, val: Any) -> None:
        self._cache[key] = (time.monotonic(), val)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.get(path, params=params)

        if resp.status_code == 401:
            raise AlpacaError(
                "Invalid Alpaca API credentials. "
                "Check ALPACA_API_KEY and ALPACA_API_SECRET in .env"
            )
        if resp.status_code == 403:
            raise AlpacaError(
                "Alpaca account does not have data access. "
                "Make sure you have a paper trading account at alpaca.markets"
            )
        if resp.status_code == 422:
            raise AlpacaError(f"Alpaca rejected request: {resp.text[:200]}")
        if resp.status_code == 429:
            raise AlpacaError("Alpaca rate limit hit — reduce request frequency")

        resp.raise_for_status()
        return resp.json()

    # ── Real-time bars ────────────────────────────────────────────────────────

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 5,
        timespan: str = "minute",
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
    ) -> list[AlpacaBar]:
        """
        Fetch recent aggregated bars (OHLCV).
        Default: last 50 five-minute bars (real-time, no delay).

        timespan options: "1Min", "5Min", "15Min", "1Hour", "1Day"
        """
        cache_key = f"bars:{ticker}:{multiplier}:{timespan}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        # Alpaca timeframe format: "5Min", "1Min", "15Min"
        tf_map = {
            (1, "minute"):  "1Min",
            (5, "minute"):  "5Min",
            (15, "minute"): "15Min",
            (1, "hour"):    "1Hour",
            (1, "day"):     "1Day",
        }
        timeframe = tf_map.get((multiplier, timespan), f"{multiplier}Min")

        today = date.today()
        # Go back 5 days to handle weekends/holidays
        start_dt = (from_date or (today - timedelta(days=5))).isoformat() + "T00:00:00Z"
        end_dt   = (to_date or today).isoformat() + "T23:59:59Z"

        data = await self._get(
            f"/v2/stocks/{ticker}/bars",
            params={
                "timeframe": timeframe,
                "start":     start_dt,
                "end":       end_dt,
                "limit":     limit,
                "adjustment": "raw",
                "feed":      "iex",   # IEX = free real-time feed
                "sort":      "asc",
            },
        )

        bars: list[AlpacaBar] = []
        for b in data.get("bars", []):
            ts_str = b.get("t", "")
            # Parse ISO timestamp
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            bars.append(AlpacaBar(
                timestamp=ts,
                open=Decimal(str(b["o"])),
                high=Decimal(str(b["h"])),
                low=Decimal(str(b["l"])),
                close=Decimal(str(b["c"])),
                volume=Decimal(str(b.get("v", 0))),
                vwap=Decimal(str(b["vw"])) if b.get("vw") else None,
            ))

        self._cache_set(cache_key, bars)
        return bars

    async def get_opening_range_bars(
        self,
        ticker: str,
        session_date: date | None = None,
        orb_minutes: int = 15,
    ) -> list[AlpacaBar]:
        """
        Fetch 1-minute bars covering the opening range window.
        Used by ORB strategy to compute opening range high/low.
        Real-time — no delay.
        """
        target_date = session_date or date.today()
        bars = await self.get_bars(
            ticker,
            multiplier=1,
            timespan="minute",
            from_date=target_date,
            to_date=target_date,
            limit=orb_minutes + 10,
        )

        # Filter to opening range: 09:30–09:45 ET = 13:30–13:45 UTC
        session_open_utc = datetime(
            target_date.year, target_date.month, target_date.day,
            13, 30, tzinfo=timezone.utc
        )
        cutoff = session_open_utc + timedelta(minutes=orb_minutes)
        return [b for b in bars if session_open_utc <= b.timestamp < cutoff]

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
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else datetime.now(timezone.utc)

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
            now = datetime.now(timezone.utc)
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
                "feed":    "iex",
            },
        )
        return data  # dict of {ticker: snapshot_data}

    def validate_staleness(self, quote: AlpacaQuote, max_age_seconds: int = 60) -> bool:
        """Return True if quote data is fresh enough to trade on."""
        age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
        return age <= max_age_seconds
