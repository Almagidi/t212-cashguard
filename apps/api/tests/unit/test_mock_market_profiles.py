"""Deterministic opt-in mock market profiles for real-worker evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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


def test_breakout_profile_respects_requested_bar_count_and_interval() -> None:
    rows = MockMarketDataProvider(profile="orb_breakout", seed=212)._orb_breakout_bars(
        "NVDA",
        interval_minutes=15,
        bars=4,
        as_of=datetime(2026, 1, 6, 19, 0, tzinfo=UTC),
    )
    timestamps = [datetime.fromisoformat(str(row["timestamp"])) for row in rows]

    assert len(rows) == 4
    assert timestamps[1] == datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    assert timestamps[2] - timestamps[1] == timedelta(minutes=15)


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
