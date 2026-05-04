"""
Kraken public market data provider.

Uses only unauthenticated public endpoints — no API credentials required.
Covers 24/7 crypto pairs: BTC, ETH, SOL, etc.

Endpoints:
  GET /0/public/OHLC   — time-bucketed bars [time, O, H, L, C, vwap, vol, count]
  GET /0/public/Ticker — current bid/ask/last

Pair names follow Kraken convention: XXBTZUSD, XETHZUSD, etc.
The provider handles conversion from human-readable tickers automatically.

Valid bar intervals (minutes): 1, 5, 15, 30, 60, 240, 1440, 10080, 21600
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx


# ── Kraken-valid bar intervals (minutes) ──────────────────────────────────────

_VALID_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)


def _nearest_interval(minutes: int) -> int:
    """Snap an arbitrary minute value to the nearest valid Kraken interval."""
    return min(_VALID_INTERVALS, key=lambda v: abs(v - minutes))


# ── Ticker → Kraken pair conversion ──────────────────────────────────────────

def _pair_from_ticker(ticker: str) -> str:
    """
    Convert a human-readable ticker to Kraken's pair format.
    Mirrors KrakenAdapter._ticker_to_pair() so both stay in sync.

    Examples:
      BTCUSD -> XXBTZUSD
      ETHUSD -> XETHZUSD
      ETHBTC -> XETHXXBT
      sol    -> SOL
    """
    t = ticker.upper()
    if t.startswith("BTC"):
        t = "XBT" + t[3:]
    if t.endswith("USD"):
        return f"X{t.replace('USD', 'ZUSD')}"
    if t.endswith("EUR"):
        return f"X{t.replace('EUR', 'ZEUR')}"
    if t.endswith("GBP"):
        return f"X{t.replace('GBP', 'ZGBP')}"
    if t.endswith("BTC"):
        return f"X{t.replace('BTC', 'XXBT')}"
    return t


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class KrakenBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None


@dataclass
class KrakenQuote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime
    is_delayed: bool = False  # Kraken public data is real-time


# ── Provider ──────────────────────────────────────────────────────────────────

class KrakenMarketDataProvider:
    """
    Real-time crypto market data from Kraken's public REST API.

    No credentials required — all endpoints are publicly accessible.
    Cache TTL: 60 s for bars, 10 s for quotes.
    """

    BASE_URL = "https://api.kraken.com"
    BARS_CACHE_TTL = 60
    QUOTE_CACHE_TTL = 10

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    async def __aenter__(self) -> KrakenMarketDataProvider:
        self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=15.0)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("KrakenMarketDataProvider must be used as async context manager")
        return self._client

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _get_cached(self, key: str, ttl: int) -> Any | None:
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < ttl:
            return entry[1]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        errors = data.get("error", [])
        if errors:
            raise RuntimeError(f"Kraken public API error: {errors}")
        return data

    # ── Bars ──────────────────────────────────────────────────────────────────

    async def get_bars(
        self,
        ticker: str,
        *,
        multiplier: int = 60,
        timespan: str = "minute",  # accepted for interface parity with Alpaca; unused
        from_date: date | None = None,
        to_date: date | None = None,  # Kraken has no end-date param; ignored
        limit: int = 200,
    ) -> list[KrakenBar]:
        """
        Fetch OHLCV bars from Kraken's public OHLC endpoint.

        multiplier  — bar width in minutes (snapped to nearest valid Kraken interval)
        from_date   — earliest bar date; converted to Kraken's 'since' Unix timestamp
        limit       — maximum bars returned (taken from the tail of the response)
        """
        interval = _nearest_interval(multiplier)
        pair = _pair_from_ticker(ticker)
        cache_key = f"bars:{pair}:{interval}"
        cached = self._get_cached(cache_key, self.BARS_CACHE_TTL)
        if cached is not None:
            return cached[-limit:]

        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if from_date is not None:
            params["since"] = int(
                datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC).timestamp()
            )

        data = await self._get("/0/public/OHLC", params)
        result = data.get("result", {})

        # Result has one key per pair plus "last"; find the bar list.
        raw: list[Any] = []
        for key, val in result.items():
            if key != "last" and isinstance(val, list):
                raw = val
                break

        bars: list[KrakenBar] = []
        for row in raw:
            ts = datetime.fromtimestamp(int(row[0]), tz=UTC)
            bars.append(KrakenBar(
                timestamp=ts,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                vwap=Decimal(str(row[5])) if row[5] and row[5] != "0" else None,
                volume=Decimal(str(row[6])),
            ))

        self._set_cached(cache_key, bars)
        return bars[-limit:]

    # ── Quote ─────────────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> KrakenQuote:
        """
        Fetch current bid/ask/last from Kraken Ticker endpoint.
        Data is real-time on Kraken's public feed.
        """
        pair = _pair_from_ticker(ticker)
        cache_key = f"quote:{pair}"
        cached = self._get_cached(cache_key, self.QUOTE_CACHE_TTL)
        if cached is not None:
            return cached

        data = await self._get("/0/public/Ticker", {"pair": pair})
        result = data.get("result", {})

        ticker_data: dict[str, Any] = {}
        for val in result.values():
            ticker_data = val
            break

        # a = ask [price, whole_lot, lot], b = bid, c = last trade [price, lot]
        ask = Decimal(str(ticker_data["a"][0]))
        bid = Decimal(str(ticker_data["b"][0]))
        last = Decimal(str(ticker_data["c"][0]))

        quote = KrakenQuote(
            ticker=ticker,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=datetime.now(UTC),
        )
        self._set_cached(cache_key, quote)
        return quote
