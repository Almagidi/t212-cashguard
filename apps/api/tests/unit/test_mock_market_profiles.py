"""Deterministic opt-in mock market profiles for real-worker evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from app.market_data.exchange_calendar import calendar_for_venue
from app.market_data.mock_provider import MockMarketDataProvider
from app.strategies.indicators import (
    Bar,
    atr,
    choppiness_index,
    is_trending_up,
    market_regime,
    relative_volume,
)
from app.strategies.orb_production import OpeningRangeBreakoutStrategy


def _bars(rows: list[dict[str, object]]) -> list[Bar]:
    return [
        Bar(
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
        )
        for row in rows
    ]


def test_seeded_breakout_profile_is_reproducible_and_seed_sensitive() -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    first = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=80, as_of=as_of
    )
    repeated = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=80, as_of=as_of
    )
    changed = MockMarketDataProvider(profile="orb_breakout", seed=213)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=80, as_of=as_of
    )

    assert first == repeated
    assert first != changed
    assert [row["timestamp"] for row in first] == [row["timestamp"] for row in repeated]
    assert all(
        row["low"]
        <= min(row["open"], row["close"])
        <= max(row["open"], row["close"])
        <= row["high"]
        and row["volume"] > 0
        for row in first
    )


def test_breakout_profile_returns_the_latest_window_at_the_requested_interval() -> None:
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA",
        interval_minutes=15,
        bars=4,
        as_of=datetime(2026, 1, 6, 19, 0, tzinfo=UTC),
    )
    timestamps = [datetime.fromisoformat(str(row["timestamp"])) for row in rows]

    assert timestamps == [
        datetime(2026, 1, 6, 18, 0, tzinfo=UTC),
        datetime(2026, 1, 6, 18, 15, tzinfo=UTC),
        datetime(2026, 1, 6, 18, 30, tzinfo=UTC),
        datetime(2026, 1, 6, 18, 45, tzinfo=UTC),
    ]


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
        datetime(2026, 11, 27, 17, 55, tzinfo=UTC),
        datetime(2026, 3, 9, 13, 32, tzinfo=UTC),
        datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    ],
    ids=["mid-session", "early-close", "after-dst-change", "holiday-weekend"],
)
@pytest.mark.parametrize("interval_minutes", [5, 7, 15, 1440])
@pytest.mark.parametrize("bars", [1, 2, 4, 20, 50, 78, 79])
def test_breakout_profile_window_is_a_suffix_of_the_full_history(
    interval_minutes: int, bars: int, as_of: datetime
) -> None:
    provider = MockMarketDataProvider(profile="orb_breakout", seed=212)

    full = provider._orb_breakout_bars(
        "NVDA", interval_minutes=interval_minutes, bars=500, as_of=as_of
    )
    window = provider._orb_breakout_bars(
        "NVDA", interval_minutes=interval_minutes, bars=bars, as_of=as_of
    )

    assert full
    assert window == full[-bars:]


def test_breakout_profile_short_window_ends_at_the_latest_completed_bar() -> None:
    as_of = datetime(2026, 7, 6, 18, 2, tzinfo=UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=50, as_of=as_of
    )
    timestamps = [datetime.fromisoformat(str(row["timestamp"])) for row in rows]

    assert len(rows) == 50
    assert timestamps[-1] == datetime(2026, 7, 6, 17, 55, tzinfo=UTC)
    assert timestamps == sorted(set(timestamps))
    assert all(later - earlier == timedelta(minutes=5) for earlier, later in pairwise(timestamps))
    assert rows[-1]["volume"] > rows[-2]["volume"]


def test_breakout_profile_before_first_completed_bar_returns_only_prior_close() -> None:
    as_of = datetime(2026, 7, 6, 13, 32, tzinfo=UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=50, as_of=as_of
    )

    assert [row["timestamp"] for row in rows] == [
        datetime(2026, 7, 2, 19, 55, tzinfo=UTC).isoformat()
    ]


@pytest.mark.parametrize("bars", [0, -1])
@pytest.mark.parametrize("interval_minutes", [5, 1440])
def test_breakout_profile_returns_nothing_for_a_non_positive_window(
    interval_minutes: int, bars: int
) -> None:
    rows = MockMarketDataProvider(profile="orb_breakout")._orb_breakout_bars(
        "NVDA",
        interval_minutes=interval_minutes,
        bars=bars,
        as_of=datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
    )

    assert rows == []


@pytest.mark.parametrize("interval_minutes", [0, -5, 1441, 10080])
def test_breakout_profile_rejects_unsupported_intervals(interval_minutes: int) -> None:
    with pytest.raises(ValueError, match="interval_minutes"):
        MockMarketDataProvider(profile="orb_breakout")._orb_breakout_bars(
            "NVDA",
            interval_minutes=interval_minutes,
            bars=10,
            as_of=datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
        )


def test_breakout_profile_daily_history_is_valid_xnys_sessions() -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    provider = MockMarketDataProvider(profile="orb_breakout", seed=212)
    daily = provider._orb_breakout_bars("NVDA", interval_minutes=1440, bars=90, as_of=as_of)
    calendar = calendar_for_venue("XNYS")
    exchange_timezone = ZoneInfo(calendar.exchange_timezone)
    local_dates = [
        datetime.fromisoformat(str(row["timestamp"])).astimezone(exchange_timezone).date()
        for row in daily
    ]
    expected_sessions = [
        session.local_date
        for session in calendar.expected_sessions(local_dates[0], date(2026, 7, 6))
    ]

    assert len(daily) == 90
    assert local_dates == expected_sessions
    assert date(2026, 7, 3) not in local_dates
    assert all(
        datetime.fromisoformat(str(row["timestamp"])).astimezone(exchange_timezone).time()
        == datetime.min.time()
        for row in daily
    )
    assert all(
        0 < row["low"] <= min(row["open"], row["close"])
        and max(row["open"], row["close"]) <= row["high"]
        and row["volume"] > 0
        for row in daily
    )


def test_breakout_profile_daily_history_agrees_with_its_intraday_bars() -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    provider = MockMarketDataProvider(profile="orb_breakout", seed=212)
    daily = provider._orb_breakout_bars("NVDA", interval_minutes=1440, bars=30, as_of=as_of)
    intraday = provider._orb_breakout_bars("NVDA", interval_minutes=5, bars=500, as_of=as_of)
    session_rows = intraday[1:]

    assert daily[-2]["close"] == intraday[0]["close"]
    assert daily[-1]["open"] == session_rows[0]["open"]
    assert daily[-1]["close"] == session_rows[-1]["close"]
    assert daily[-1]["high"] == max(row["high"] for row in session_rows)
    assert daily[-1]["low"] == min(row["low"] for row in session_rows)
    assert daily[-1]["volume"] == sum(row["volume"] for row in session_rows)


def test_breakout_profile_daily_history_is_reproducible_and_seed_sensitive() -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)

    def daily(seed: int) -> list[dict[str, object]]:
        return MockMarketDataProvider(profile="orb_breakout", seed=seed)._orb_breakout_bars(
            "SPY", interval_minutes=1440, bars=90, as_of=as_of
        )

    assert daily(212) == daily(212)
    assert daily(212) != daily(213)


def test_breakout_profile_daily_history_omits_a_session_with_no_completed_bar() -> None:
    as_of = datetime(2026, 7, 6, 13, 32, tzinfo=UTC)
    daily = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=1440, bars=5, as_of=as_of
    )
    exchange_timezone = ZoneInfo("America/New_York")

    assert len(daily) == 5
    assert datetime.fromisoformat(str(daily[-1]["timestamp"])).astimezone(
        exchange_timezone
    ).date() == date(2026, 7, 2)


def test_breakout_profile_get_ohlcv_serves_daily_and_intraday_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    original_bars = MockMarketDataProvider._orb_breakout_bars

    def pinned_bars(
        self: MockMarketDataProvider, ticker: str, *, interval_minutes: int, bars: int
    ) -> list[dict[str, object]]:
        return original_bars(
            self, ticker, interval_minutes=interval_minutes, bars=bars, as_of=as_of
        )

    monkeypatch.setattr(MockMarketDataProvider, "_orb_breakout_bars", pinned_bars)
    provider = MockMarketDataProvider(profile="orb_breakout", seed=212)

    daily = provider.get_ohlcv("SPY", interval_minutes=1440, bars=90)
    intraday = provider.get_ohlcv("SPY", interval_minutes=5, bars=50)

    assert len(daily) == 90
    assert len(intraday) == 50
    assert intraday[-1]["timestamp"] == datetime(2026, 7, 6, 17, 55, tzinfo=UTC).isoformat()
    assert daily[-1]["close"] == intraday[-1]["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 1, 7, 14, 32, tzinfo=UTC),
        datetime(2026, 1, 7, 14, 50, tzinfo=UTC),
        datetime(2026, 1, 7, 16, 35, tzinfo=UTC),
        datetime(2026, 1, 7, 20, 59, tzinfo=UTC),
        datetime(2026, 1, 10, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 7, 13, 0, tzinfo=UTC),
        datetime(2026, 11, 27, 17, 55, tzinfo=UTC),
    ],
    ids=[
        "no-completed-bar",
        "opening-range",
        "mid-session",
        "last-minute",
        "weekend",
        "pre-open",
        "early-close",
    ],
)
async def test_breakout_profile_gives_the_regime_service_a_classified_uptrend(
    monkeypatch: pytest.MonkeyPatch, as_of: datetime
) -> None:
    from app.core.config import settings
    from app.services import market_regime as market_regime_module
    from app.services.feed_health import reset_feed_health

    original_bars = MockMarketDataProvider._orb_breakout_bars

    def pinned_bars(
        self: MockMarketDataProvider, ticker: str, *, interval_minutes: int, bars: int
    ) -> list[dict[str, object]]:
        return original_bars(
            self, ticker, interval_minutes=interval_minutes, bars=bars, as_of=as_of
        )

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout")
    monkeypatch.setattr(MockMarketDataProvider, "_orb_breakout_bars", pinned_bars)
    monkeypatch.setattr(market_regime_module, "_cached_regime", None)
    monkeypatch.setattr(market_regime_module, "_last_evaluated_at", None)
    reset_feed_health()

    payload = await market_regime_module.MarketRegimeService().evaluate()

    assert payload["regime"] == "trending_up"
    assert "orb" not in payload["suppressed_strategies"]


@pytest.mark.parametrize(
    ("as_of", "expected_open"),
    [
        (
            datetime(2026, 1, 6, 19, 0, tzinfo=UTC),
            datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 13, 30, tzinfo=UTC),
        ),
    ],
)
def test_breakout_profile_uses_current_xnys_grid_with_previous_close(
    as_of: datetime,
    expected_open: datetime,
) -> None:
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA",
        interval_minutes=5,
        bars=80,
        as_of=as_of,
    )
    timestamps = [datetime.fromisoformat(str(row["timestamp"])) for row in rows]
    calendar = calendar_for_venue("XNYS")
    current_session = calendar.session_for_timestamp(expected_open)

    assert current_session is not None
    current_times = [
        timestamp
        for timestamp in timestamps
        if calendar.session_for_timestamp(timestamp) == current_session
    ]
    previous_session = calendar.previous_session(current_session)
    assert timestamps[0] == calendar.session_close(previous_session) - timedelta(minutes=5)
    assert calendar.is_terminal_bar(previous_session, timestamps[0], interval_minutes=5)
    assert current_times[0] == expected_open == calendar.session_open(current_session)
    assert all(timestamp + timedelta(minutes=5) <= as_of for timestamp in current_times)
    assert timestamps == sorted(timestamps)


def test_breakout_profile_satisfies_unmodified_orb_filters() -> None:
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA",
        interval_minutes=5,
        bars=80,
        as_of=datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
    )
    calendar = calendar_for_venue("XNYS")
    current_rows = [
        row
        for row in rows
        if calendar.session_for_timestamp(datetime.fromisoformat(str(row["timestamp"])))
        == calendar.session_for_timestamp(datetime(2026, 7, 6, 13, 30, tzinfo=UTC))
    ]
    bars = _bars(current_rows)
    previous_close = Decimal(str(rows[0]["close"]))
    atr_pct = float(atr(bars, 14) / bars[-1].close * 100)

    assert market_regime(bars) == "trending_up"
    assert float(choppiness_index(bars)) < 61.8
    assert 0.3 <= atr_pct <= 6.0
    assert float(relative_volume(bars, 20)) >= 1.5
    assert is_trending_up(bars, 9, 21)

    signal = OpeningRangeBreakoutStrategy().generate_signal(
        ticker="NVDA",
        bars=bars,
        account_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        current_time_utc="19:00",
        prev_close=previous_close,
    )

    assert signal is not None
    assert signal.side == "buy"


def test_default_profile_keeps_existing_random_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def midpoint(low: float, high: float) -> float:
        nonlocal calls
        calls += 1
        return (low + high) / 2

    monkeypatch.setattr("app.market_data.mock_provider.random.uniform", midpoint)

    rows = MockMarketDataProvider().get_ohlcv("NVDA", bars=4)

    assert len(rows) == 4
    assert calls > 0


def test_default_equity_timestamps_use_xnys_grid_and_retain_prior_terminal_bar() -> None:
    times = MockMarketDataProvider._equity_bar_times(
        interval_minutes=5,
        bars=80,
        as_of=datetime(2025, 1, 6, 15, 5, tzinfo=UTC),
    )

    assert times[-1] == datetime(2025, 1, 6, 15, 0, tzinfo=UTC)
    assert datetime(2025, 1, 3, 20, 55, tzinfo=UTC) in times
    assert all(timestamp.second == 0 and timestamp.microsecond == 0 for timestamp in times)


def test_default_equity_timestamps_never_emit_future_premarket_bars() -> None:
    as_of = datetime(2025, 1, 6, 13, 0, tzinfo=UTC)

    times = MockMarketDataProvider._equity_bar_times(
        interval_minutes=5,
        bars=2,
        as_of=as_of,
    )

    assert times == [
        datetime(2025, 1, 3, 20, 50, tzinfo=UTC),
        datetime(2025, 1, 3, 20, 55, tzinfo=UTC),
    ]
    assert all(timestamp < as_of for timestamp in times)


def test_default_daily_equity_timestamps_use_xnys_sessions() -> None:
    times = MockMarketDataProvider._equity_daily_bar_times(
        bars=4,
        as_of=datetime(2026, 1, 20, 15, 0, tzinfo=UTC),
    )

    exchange_timezone = ZoneInfo("America/New_York")
    assert [timestamp.astimezone(exchange_timezone).date() for timestamp in times] == [
        date(2026, 1, 14),
        date(2026, 1, 15),
        date(2026, 1, 16),
        date(2026, 1, 20),
    ]


def test_mock_market_open_uses_xnys_dst_and_holiday_rules() -> None:
    assert MockMarketDataProvider._equity_market_is_open(
        as_of=datetime(2026, 7, 6, 13, 45, tzinfo=UTC)
    )
    assert not MockMarketDataProvider._equity_market_is_open(
        as_of=datetime(2026, 7, 3, 14, 45, tzinfo=UTC)
    )


def test_explicit_mock_provider_uses_configured_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.market_data import get_live_provider

    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout")
    monkeypatch.setattr(settings, "MOCK_MARKET_SEED", 313)

    provider = get_live_provider()

    assert provider.profile == "orb_breakout"
    assert provider.seed == 313


def test_automatic_mock_fallback_uses_configured_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.market_data import get_live_provider

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "auto")
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout")
    monkeypatch.setattr(settings, "MOCK_MARKET_SEED", 313)
    monkeypatch.setattr(settings, "ALPACA_API_KEY", "")
    monkeypatch.setattr(settings, "ALPACA_API_SECRET", "")
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "")

    provider = get_live_provider()

    assert provider.profile == "orb_breakout"
    assert provider.seed == 313


@pytest.mark.parametrize("app_mode", ["paper", "demo", "live"])
def test_provider_selection_rejects_breakout_profile_outside_mock(
    monkeypatch: pytest.MonkeyPatch, app_mode: str
) -> None:
    from app.core.config import settings
    from app.market_data import get_live_provider

    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout")

    with pytest.raises(RuntimeError, match="mock market profile"):
        get_live_provider()


def test_breakout_profile_generates_supported_orb_signal() -> None:
    as_of = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=80, as_of=as_of
    )
    calendar = calendar_for_venue("XNYS")
    current_session = calendar.session_for_timestamp(as_of)
    current_rows = [
        row
        for row in rows
        if calendar.session_for_timestamp(datetime.fromisoformat(str(row["timestamp"])))
        == current_session
    ]
    bars = _bars(current_rows)

    signal = OpeningRangeBreakoutStrategy().generate_signal(
        ticker="NVDA",
        bars=bars,
        account_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        current_time_utc="19:00",
        prev_close=Decimal(str(rows[0]["close"])),
    )

    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"


def test_breakout_profile_signals_at_the_real_worker_proof_clock() -> None:
    # The required real-worker proof pins the worker to this instant: 25 completed
    # five-minute bars, enough for the 14-period Choppiness Index (15 bars).
    as_of = datetime(2026, 1, 7, 16, 35, tzinfo=UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=540, as_of=as_of
    )
    session_bars = _bars(rows[1:])

    assert len(session_bars) == 25
    assert market_regime(session_bars) in {"neutral", "trending_up"}
    signal = OpeningRangeBreakoutStrategy().generate_signal(
        ticker="NVDA",
        bars=session_bars,
        account_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        current_time_utc="16:35",
        prev_close=Decimal(str(rows[0]["close"])),
    )

    assert signal is not None
    assert signal.side == "buy"


def test_breakout_profile_gives_no_signal_before_the_index_can_be_computed() -> None:
    # 14 completed bars: one short of what the index needs, so the strategy must wait.
    as_of = datetime(2026, 1, 7, 15, 40, tzinfo=UTC)
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA", interval_minutes=5, bars=540, as_of=as_of
    )
    session_bars = _bars(rows[1:])

    assert len(session_bars) == 14
    assert market_regime(session_bars) == "unknown"
    assert (
        OpeningRangeBreakoutStrategy().generate_signal(
            ticker="NVDA",
            bars=session_bars,
            account_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            current_time_utc="15:40",
            prev_close=Decimal(str(rows[0]["close"])),
        )
        is None
    )


@pytest.mark.parametrize("app_mode", ["paper", "demo", "live"])
def test_startup_rejects_non_default_mock_profile_outside_mock(
    monkeypatch: pytest.MonkeyPatch, app_mode: str
) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout", raising=False)

    with pytest.raises(RuntimeError, match="mock market profile"):
        assert_startup_safe()


def test_startup_allows_breakout_profile_in_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.services.startup_validation import build_startup_report

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "MOCK_MARKET_PROFILE", "orb_breakout", raising=False)

    report = build_startup_report()

    assert not any(
        check["key"] == "mock_market_profile" and check["status"] == "fail"
        for check in report["checks"]
    )
