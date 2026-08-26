"""
Mock market data provider.
Returns realistic fake OHLCV data and quotes.
Used in mock mode and for strategy testing without real market data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.market_data.exchange_calendar import calendar_for_venue


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

    def __init__(self, *, profile: str = "default", seed: int = 212) -> None:
        self.profile = profile
        self.seed = seed

    def _orb_breakout_bars(
        self, ticker: str, *, interval_minutes: int, bars: int
    ) -> list[dict[str, Any]]:
        """Return a stable valid session with a genuine high-volume ORB breakout."""
        rng = random.Random(f"{self.seed}:{ticker.upper()}")
        base = MOCK_BASE_PRICES.get(ticker, 100.0) * (1 + rng.uniform(-0.02, 0.02))
        anchor = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
        result: list[dict[str, Any]] = []
        for index in range(min(max(bars, 0), 25)):
            if index < 3:
                open_price, high, low, close, volume = (
                    base,
                    base * 1.012,
                    base * 0.992,
                    base * (1 + index * 0.002),
                    20_000,
                )
            elif index == 24:
                open_price, high, low, close, volume = (
                    base * 1.036,
                    base * 1.05,
                    base * 1.032,
                    base * 1.045,
                    70_000,
                )
            else:
                close_factor = 1.014 + (index - 3) * 0.0011
                open_price, high, low, close, volume = (
                    base * (close_factor - 0.002),
                    base * (close_factor + 0.004),
                    base * (close_factor - 0.005),
                    base * close_factor,
                    20_000,
                )
            result.append(
                {
                    "timestamp": (anchor + timedelta(minutes=index * interval_minutes)).isoformat(),
                    "open": round(open_price, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(close, 4),
                    "volume": volume,
                }
            )
        return result

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
            timestamp=datetime.now(UTC),
            is_stale=False,
        )

    @staticmethod
    def _equity_bar_times(
        *,
        interval_minutes: int,
        bars: int,
        as_of: datetime | None = None,
    ) -> list[datetime]:
        """Return exact XNYS regular-session bar-open timestamps, oldest first."""
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        if bars <= 0:
            return []

        calendar = calendar_for_venue("XNYS")
        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        local_date = now.astimezone(ZoneInfo(calendar.exchange_timezone)).date()
        sessions = calendar.expected_sessions(local_date - timedelta(days=14), local_date)
        if not sessions:
            return []
        session = sessions[-1]
        active_session = calendar.session_for_timestamp(now)
        if active_session is not None:
            session = active_session
            elapsed_minutes = int((now - calendar.session_open(session)).total_seconds() // 60)
            completed_intervals = elapsed_minutes // interval_minutes
            if completed_intervals > 0:
                cursor = calendar.session_open(session) + timedelta(
                    minutes=(completed_intervals - 1) * interval_minutes
                )
            else:
                session = calendar.previous_session(session)
                cursor = calendar.session_close(session) - timedelta(minutes=interval_minutes)
        else:
            if now < calendar.session_open(session):
                session = calendar.previous_session(session)
            cursor = calendar.session_close(session) - timedelta(minutes=interval_minutes)

        timestamps: list[datetime] = []
        while len(timestamps) < bars:
            session_open = calendar.session_open(session)
            if cursor < session_open:
                session = calendar.previous_session(session)
                cursor = calendar.session_close(session) - timedelta(minutes=interval_minutes)
                continue
            timestamps.append(cursor)
            cursor -= timedelta(minutes=interval_minutes)
        timestamps.reverse()
        return timestamps

    def get_ohlcv(
        self,
        ticker: str,
        interval_minutes: int = 5,
        bars: int = 50,
    ) -> list[dict[str, Any]]:
        """Generate fake OHLCV bars."""
        if self.profile == "orb_breakout":
            return self._orb_breakout_bars(ticker, interval_minutes=interval_minutes, bars=bars)
        base = MOCK_BASE_PRICES.get(ticker, 100.0)
        result = []

        if interval_minutes < 24 * 60:
            timestamps = self._equity_bar_times(
                interval_minutes=interval_minutes,
                bars=bars,
            )
        else:
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            timestamps = [
                now - timedelta(minutes=index * interval_minutes) for index in range(bars, 0, -1)
            ]

        price = base
        for ts in timestamps:
            o = price
            h = o * (1 + random.uniform(0, 0.01))
            low = o * (1 - random.uniform(0, 0.01))
            c = random.uniform(low, h)
            v = random.randint(50000, 500000)
            result.append(
                {
                    "timestamp": ts.isoformat(),
                    "open": round(o, 4),
                    "high": round(h, 4),
                    "low": round(low, 4),
                    "close": round(c, 4),
                    "volume": v,
                }
            )
            price = c

        return result

    def is_market_open(self, ticker: str = "AAPL") -> bool:
        """Check if market is open based on current UTC time."""
        now = datetime.now(UTC)
        # NYSE/NASDAQ: 14:30-21:00 UTC on weekdays
        if now.weekday() >= 5:  # Weekend
            return False
        market_open = now.replace(hour=14, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=21, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    def validate_staleness(self, quote: Quote, max_age_seconds: int = 60) -> bool:
        """Return True if quote is fresh enough."""
        age = (datetime.now(UTC) - quote.timestamp).total_seconds()
        return age <= max_age_seconds


# Singleton
mock_market_data = MockMarketDataProvider()
