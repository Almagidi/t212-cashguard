"""
Mock market data provider.
Returns realistic fake OHLCV data and quotes.
Used in mock mode and for strategy testing without real market data.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any


@dataclass
class Quote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: int
    timestamp: datetime
    is_stale: bool = False


# Base prices for mock instruments
MOCK_BASE_PRICES: dict[str, float] = {
    "AAPL": 178.0,
    "MSFT": 395.0,
    "TSLA": 248.0,
    "GOOGL": 168.0,
    "AMZN": 198.0,
    "NVDA": 875.0,
    "META": 540.0,
    "SPY": 560.0,
    "QQQ": 480.0,
    "IWM": 220.0,
}

# Track current prices to simulate trending
_current_prices: dict[str, float] = dict(MOCK_BASE_PRICES)


class MockMarketDataProvider:
    """
    Generates realistic-ish fake market data.
    Prices drift slightly on each call to simulate market movement.
    """

    def get_quote(self, ticker: str) -> Quote:
        base = MOCK_BASE_PRICES.get(ticker, 100.0)
        current = _current_prices.get(ticker, base)

        # Random walk
        change_pct = random.gauss(0, 0.001)
        new_price = max(current * (1 + change_pct), 0.01)
        _current_prices[ticker] = new_price

        spread = new_price * 0.0001  # 1bp spread
        bid = Decimal(str(round(new_price - spread / 2, 4)))
        ask = Decimal(str(round(new_price + spread / 2, 4)))
        last = Decimal(str(round(new_price, 4)))

        return Quote(
            ticker=ticker,
            bid=bid,
            ask=ask,
            last=last,
            volume=random.randint(10000, 1000000),
            timestamp=datetime.now(timezone.utc),
            is_stale=False,
        )

    def get_ohlcv(
        self,
        ticker: str,
        interval_minutes: int = 5,
        bars: int = 50,
    ) -> list[dict[str, Any]]:
        """Generate fake OHLCV bars."""
        base = MOCK_BASE_PRICES.get(ticker, 100.0)
        now = datetime.now(timezone.utc)
        result = []

        price = base
        for i in range(bars, 0, -1):
            ts = now - timedelta(minutes=i * interval_minutes)
            o = price
            h = o * (1 + random.uniform(0, 0.01))
            l = o * (1 - random.uniform(0, 0.01))
            c = random.uniform(l, h)
            v = random.randint(50000, 500000)
            result.append({
                "timestamp": ts.isoformat(),
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(l, 4),
                "close": round(c, 4),
                "volume": v,
            })
            price = c

        return result

    def is_market_open(self, ticker: str = "AAPL") -> bool:
        """Check if market is open based on current UTC time."""
        now = datetime.now(timezone.utc)
        # NYSE/NASDAQ: 14:30-21:00 UTC on weekdays
        if now.weekday() >= 5:  # Weekend
            return False
        market_open = now.replace(hour=14, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=21, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    def validate_staleness(self, quote: Quote, max_age_seconds: int = 60) -> bool:
        """Return True if quote is fresh enough."""
        age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
        return age <= max_age_seconds


# Singleton
mock_market_data = MockMarketDataProvider()
