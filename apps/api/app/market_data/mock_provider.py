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
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.market_data.exchange_calendar import calendar_for_venue

if TYPE_CHECKING:
    from app.market_data.exchange_calendar import TradingSession


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

_DAILY_INTERVAL_MINUTES = 24 * 60
# The orb_breakout profile builds its daily bar for the anchor session from
# these intraday bars, and every earlier day closes into this prior close.
_ORB_DAILY_SOURCE_INTERVAL_MINUTES = 5
_ORB_PRIOR_CLOSE_FACTOR = 0.999
_ORB_DAILY_VOLUME = 1_500_000


class MockMarketDataProvider:
    """
    Generates realistic-ish fake market data.
    Prices drift slightly on each call to simulate market movement.
    """

    def __init__(self, *, profile: str = "default", seed: int = 212) -> None:
        self.profile = profile
        self.seed = seed

    def _orb_base_price(self, ticker: str) -> float:
        rng = random.Random(f"{self.seed}:{ticker.upper()}")
        return MOCK_BASE_PRICES.get(ticker, 100.0) * (1 + rng.uniform(-0.02, 0.02))

    def _orb_breakout_bars(
        self,
        ticker: str,
        *,
        interval_minutes: int,
        bars: int,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return the latest ``bars`` completed bars of a seeded XNYS ORB breakout.

        The history is anchored on the session in progress at ``as_of`` (or the
        most recent finished one). Intraday history is that session's completed
        bars preceded by the previous session's terminal bar; daily history is
        one bar per XNYS session ending with the anchor session. Either way the
        result is the newest ``bars`` rows, oldest first, so a short window is a
        suffix of a longer one. The breakout volume sits on the latest completed
        intraday bar, so that bar's volume changes once a newer bar completes.
        """
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        if interval_minutes > _DAILY_INTERVAL_MINUTES:
            raise ValueError(
                "interval_minutes above one day is not supported by the orb_breakout profile"
            )
        if bars <= 0:
            return []

        calendar = calendar_for_venue("XNYS")
        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        local_date = now.astimezone(ZoneInfo(calendar.exchange_timezone)).date()
        sessions = calendar.expected_sessions(local_date - timedelta(days=14), local_date)
        if not sessions:
            return []

        session = calendar.session_for_timestamp(now)
        if session is None:
            session = sessions[-1]
            if now < calendar.session_close(session):
                session = calendar.previous_session(session)

        if interval_minutes == _DAILY_INTERVAL_MINUTES:
            rows = self._orb_daily_rows(ticker, bars=bars, session=session, now=now)
        else:
            rows = self._orb_intraday_rows(
                ticker, interval_minutes=interval_minutes, session=session, now=now
            )
        return rows[-bars:]

    def _orb_intraday_rows(
        self,
        ticker: str,
        *,
        interval_minutes: int,
        session: TradingSession,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Previous terminal bar plus every completed bar of ``session``."""
        base = self._orb_base_price(ticker)
        calendar = calendar_for_venue("XNYS")
        session_open = calendar.session_open(session)
        session_close = calendar.session_close(session)
        completed_through = min(now, session_close)
        current_bar_times: list[datetime] = []
        cursor = session_open
        interval = timedelta(minutes=interval_minutes)
        while cursor + interval <= completed_through:
            current_bar_times.append(cursor)
            cursor += interval

        previous_session = calendar.previous_session(session)
        previous_terminal = calendar.session_close(previous_session) - interval
        timestamps = [previous_terminal, *current_bar_times]

        result: list[dict[str, Any]] = []
        previous_close = base * _ORB_PRIOR_CLOSE_FACTOR
        for index, timestamp in enumerate(timestamps):
            if index == 0:
                open_price = base * 0.9988
                close = previous_close
                high = max(open_price, close) * 1.0005
                low = min(open_price, close) * 0.9995
                volume = 20_000
            else:
                session_index = index - 1
                if session_index < 3:
                    close_factors = (1.0001, 1.0004, 1.0007)
                    high_factors = (1.0012, 1.0014, 1.0015)
                    low_factors = (0.9992, 0.9994, 0.9997)
                    open_price = base if session_index == 0 else previous_close
                    close = base * close_factors[session_index]
                    high = base * high_factors[session_index]
                    low = base * low_factors[session_index]
                else:
                    open_price = previous_close
                    close_factor = 1.0027 + (session_index - 3) * 0.0027
                    close = base * close_factor
                    high = max(open_price, close) * 1.0004
                    low = min(open_price, close) * 0.9996
                volume = 70_000 if index == len(timestamps) - 1 else 20_000
                previous_close = close
            result.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "open": round(open_price, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(close, 4),
                    "volume": volume,
                }
            )
        return result

    def _orb_daily_rows(
        self,
        ticker: str,
        *,
        bars: int,
        session: TradingSession,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """The newest ``bars`` daily bars, ending with ``session`` once it has a bar.

        Earlier sessions rise steadily into the prior close the intraday history
        starts from, and the anchor session is the aggregate of its own completed
        intraday bars, so both views of the same market agree. The agreement holds
        for the five-minute view only: the intraday ramp is indexed by bar number,
        so a coarser intraday interval ends lower than the daily bar.
        """
        calendar = calendar_for_venue("XNYS")
        exchange_timezone = ZoneInfo(calendar.exchange_timezone)

        def label(day_session: TradingSession) -> str:
            return (
                datetime.combine(day_session.local_date, datetime.min.time(), exchange_timezone)
                .astimezone(UTC)
                .isoformat()
            )

        newest_first: list[dict[str, Any]] = []
        session_rows = self._orb_intraday_rows(
            ticker,
            interval_minutes=_ORB_DAILY_SOURCE_INTERVAL_MINUTES,
            session=session,
            now=now,
        )[1:]
        if session_rows:
            newest_first.append(
                {
                    "timestamp": label(session),
                    "open": session_rows[0]["open"],
                    "high": max(row["high"] for row in session_rows),
                    "low": min(row["low"] for row in session_rows),
                    "close": session_rows[-1]["close"],
                    "volume": sum(row["volume"] for row in session_rows),
                }
            )

        rng = random.Random(f"{self.seed}:{ticker.upper()}:daily")
        close = self._orb_base_price(ticker) * _ORB_PRIOR_CLOSE_FACTOR
        day_session = calendar.previous_session(session)
        while len(newest_first) < bars:
            open_price = close / (1 + rng.uniform(0.001, 0.004))
            newest_first.append(
                {
                    "timestamp": label(day_session),
                    "open": round(open_price, 4),
                    "high": round(close * 1.002, 4),
                    "low": round(open_price * 0.998, 4),
                    "close": round(close, 4),
                    "volume": _ORB_DAILY_VOLUME,
                }
            )
            close = open_price
            day_session = calendar.previous_session(day_session)
        newest_first.reverse()
        return newest_first

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

    @staticmethod
    def _equity_daily_bar_times(
        *,
        bars: int,
        as_of: datetime | None = None,
    ) -> list[datetime]:
        """Return exchange-local daily labels on valid XNYS sessions."""
        if bars <= 0:
            return []

        calendar = calendar_for_venue("XNYS")
        exchange_timezone = ZoneInfo(calendar.exchange_timezone)
        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        local_date = now.astimezone(exchange_timezone).date()
        local_sessions = calendar.expected_sessions(local_date, local_date)
        if local_sessions:
            session = local_sessions[0]
            if now < calendar.session_open(session):
                session = calendar.previous_session(session)
        else:
            sessions = calendar.expected_sessions(local_date - timedelta(days=14), local_date)
            if not sessions:
                return []
            session = sessions[-1]

        sessions_desc = [session]
        while len(sessions_desc) < bars:
            session = calendar.previous_session(session)
            sessions_desc.append(session)
        sessions_desc.reverse()
        return [
            datetime.combine(
                session.local_date,
                datetime.min.time(),
                exchange_timezone,
            ).astimezone(UTC)
            for session in sessions_desc
        ]

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
        elif interval_minutes == 24 * 60:
            timestamps = self._equity_daily_bar_times(bars=bars)
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

    @staticmethod
    def _equity_market_is_open(*, as_of: datetime | None = None) -> bool:
        calendar = calendar_for_venue("XNYS")
        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        return calendar.session_for_timestamp(now) is not None

    def is_market_open(self, ticker: str = "AAPL") -> bool:
        """Check the current XNYS regular-session state."""
        del ticker
        return self._equity_market_is_open()

    def validate_staleness(self, quote: Quote, max_age_seconds: int = 60) -> bool:
        """Return True if quote is fresh enough."""
        age = (datetime.now(UTC) - quote.timestamp).total_seconds()
        return age <= max_age_seconds


# Singleton
mock_market_data = MockMarketDataProvider()
