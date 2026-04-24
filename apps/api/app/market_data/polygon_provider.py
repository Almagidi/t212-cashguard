"""
Polygon.io market data provider.
Supports both free (delayed) and paid (real-time) tiers.
Used to feed OHLCV data to the strategy engine.

Get a free API key at: https://polygon.io
Free tier: 15-minute delayed data, 5 API calls/minute
Paid tier: real-time data, unlimited calls

Set POLYGON_API_KEY in your .env to enable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import settings


class PolygonError(Exception):
    """Raised on Polygon API errors."""


@dataclass
class Quote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: int
    timestamp: datetime
    is_delayed: bool = True
    is_stale: bool = False


@dataclass
class Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None


class PolygonMarketDataProvider:
    """
    Async Polygon.io client for intraday bars and latest quotes.
    Rate-limited: free tier allows 5 requests/minute.
    All data is cached in memory to avoid hammering the rate limit.
    """

    BASE_URL = "https://api.polygon.io"
    CACHE_TTL_SECONDS = 60  # 1 minute cache — respect free tier limits

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.POLYGON_API_KEY
        if not self._api_key:
            raise ValueError(
                "POLYGON_API_KEY not set. "
                "Get a free key at https://polygon.io and add it to .env"
            )
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    async def __aenter__(self) -> "PolygonMarketDataProvider":
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=10.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("Use as async context manager")
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
        resp = await self.client.get(path, params=params)
        if resp.status_code == 429:
            raise PolygonError("Rate limited — upgrade to paid tier or reduce poll frequency")
        if resp.status_code == 403:
            raise PolygonError("Invalid API key or subscription required for this endpoint")
        resp.raise_for_status()
        return resp.json()

    # ── Bars (OHLCV) ─────────────────────────────────────────────────────────

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 5,
        timespan: str = "minute",
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
    ) -> list[Bar]:
        """
        Fetch aggregated bars (OHLCV) for a ticker.
        Default: 5-minute bars for the last 2 trading days.

        Free tier note: data is 15 minutes delayed.
        """
        cache_key = f"bars:{ticker}:{multiplier}:{timespan}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        today = date.today()
        to_dt = to_date or today
        from_dt = from_date or (today - timedelta(days=3))  # 3 days covers weekends

        data = await self._get(
            f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}"
            f"/{from_dt.isoformat()}/{to_dt.isoformat()}",
            params={"adjusted": "true", "sort": "asc", "limit": limit},
        )

        bars: list[Bar] = []
        for result in data.get("results", []):
            bars.append(Bar(
                timestamp=datetime.fromtimestamp(
                    result["t"] / 1000, tz=timezone.utc
                ),
                open=Decimal(str(result["o"])),
                high=Decimal(str(result["h"])),
                low=Decimal(str(result["l"])),
                close=Decimal(str(result["c"])),
                volume=int(result.get("v", 0)),
                vwap=Decimal(str(result["vw"])) if "vw" in result else None,
            ))

        self._cache_set(cache_key, bars)
        return bars

    async def get_opening_range_bars(
        self,
        ticker: str,
        session_date: date | None = None,
        orb_minutes: int = 15,
    ) -> list[Bar]:
        """
        Fetch 1-minute bars for the first N minutes of the session.
        Used by ORB strategy to determine opening range high/low.
        """
        target_date = session_date or date.today()
        bars = await self.get_bars(
            ticker,
            multiplier=1,
            timespan="minute",
            from_date=target_date,
            to_date=target_date,
            limit=orb_minutes + 5,
        )
        # Filter to market open window (14:30–14:45 UTC = 09:30–09:45 ET)
        session_open_utc = datetime(
            target_date.year, target_date.month, target_date.day,
            14, 30, tzinfo=timezone.utc
        )
        cutoff = session_open_utc + timedelta(minutes=orb_minutes)
        return [b for b in bars if session_open_utc <= b.timestamp < cutoff]

    # ── Latest quote ─────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> Quote:
        """
        Fetch the latest trade/quote for a ticker.
        Free tier: ~15 min delayed. Paid tier: real-time.
        """
        cache_key = f"quote:{ticker}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        # Previous close + snapshot for current price
        data = await self._get(
            f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        )
        ticker_data = data.get("ticker", {})
        last_trade = ticker_data.get("lastTrade", {})
        last_quote = ticker_data.get("lastQuote", {})
        day = ticker_data.get("day", {})

        last_price = Decimal(str(last_trade.get("p") or day.get("c") or 0))
        bid = Decimal(str(last_quote.get("P") or last_price))
        ask = Decimal(str(last_quote.get("p") or last_price))

        # Updated timestamp
        updated_ms = ticker_data.get("updated", 0)
        ts = datetime.fromtimestamp(updated_ms / 1_000_000_000, tz=timezone.utc) \
            if updated_ms else datetime.now(timezone.utc)

        quote = Quote(
            ticker=ticker,
            bid=bid,
            ask=ask,
            last=last_price,
            volume=int(day.get("v", 0)),
            timestamp=ts,
            is_delayed=True,  # Always assume delayed for free tier
        )
        self._cache_set(cache_key, quote)
        return quote

    # ── Market hours ─────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        """Check if US equities market is open right now."""
        cache_key = "market_status"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            data = await self._get("/v1/marketstatus/now")
            is_open = data.get("market") == "open"
        except Exception:
            # Fallback: check UTC time window
            now = datetime.now(timezone.utc)
            is_open = (
                now.weekday() < 5
                and 14 <= now.hour < 21
                and not (now.hour == 14 and now.minute < 30)
            )

        self._cache_set(cache_key, is_open)
        return is_open

    def validate_staleness(self, quote: Quote, max_age_seconds: int = 300) -> bool:
        """Return True if quote is fresh enough to trade on."""
        age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
        return age <= max_age_seconds


class MarketDataProviderFactory:
    """Returns the configured market data provider."""

    @staticmethod
    def create() -> "PolygonMarketDataProvider | MockMarketDataFallback":
        key = getattr(settings, "POLYGON_API_KEY", "")
        if key:
            return PolygonMarketDataProvider(key)
        from app.market_data.mock_provider import MockMarketDataProvider
        return MockMarketDataProvider()  # type: ignore[return-value]


class MockMarketDataFallback:
    """Thin wrapper so Polygon and Mock share the same interface."""
    pass
