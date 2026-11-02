"""Bar-age gate: entries fail closed on stale/invalid bars; exits are never gated."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.market_data.bar_freshness import assess_bar_freshness, bars_are_stale
from app.services.signal_allocator import SignalAllocator
from app.services.strategy_runner import StrategyRunner
from app.strategies.indicators import Bar

# Tue 2024-01-02, XNYS regular session 14:30-21:00 UTC (EST)
OPEN = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def test_fresh_bar_during_session_is_not_stale():
    now = OPEN + timedelta(minutes=33)
    assert not bars_are_stale(OPEN + timedelta(minutes=25), 5, now=now)


def test_bar_older_than_2_5_intervals_plus_grace_is_stale():
    now = OPEN + timedelta(minutes=60)
    # 12.5 min + 60 s = 13.5 min threshold for 5-minute bars
    assert not bars_are_stale(now - timedelta(minutes=13), 5, now=now)
    result = assess_bar_freshness(now - timedelta(minutes=14), 5, now=now)
    assert result.stale and result.reason == "bars_stale"


def test_threshold_scales_with_interval():
    now = OPEN + timedelta(minutes=120)
    assert not bars_are_stale(now - timedelta(minutes=30), 15, now=now)
    assert bars_are_stale(now - timedelta(minutes=40), 15, now=now)


def test_never_stale_when_market_is_closed():
    last = datetime(2023, 12, 29, 20, 55, tzinfo=UTC)
    assert not bars_are_stale(last, 5, now=datetime(2023, 12, 29, 23, 0, tzinfo=UTC))  # after close
    assert not bars_are_stale(last, 5, now=datetime(2024, 1, 2, 12, 0, tzinfo=UTC))  # pre-market
    assert not bars_are_stale(last, 5, now=datetime(2024, 1, 6, 15, 0, tzinfo=UTC))  # Saturday
    assert not bars_are_stale(last, 5, now=datetime(2024, 1, 1, 15, 0, tzinfo=UTC))  # holiday


def test_prior_session_bar_does_not_count_as_stale_in_first_minutes():
    # Age is measured from the session open, not yesterday's close.
    assert not bars_are_stale(
        datetime(2024, 1, 1, 20, 55, tzinfo=UTC), 5, now=OPEN + timedelta(minutes=5)
    )
    assert bars_are_stale(
        datetime(2023, 12, 29, 20, 55, tzinfo=UTC), 5, now=OPEN + timedelta(minutes=20)
    )


def test_early_close_session_uses_early_close_hours():
    now = datetime(2024, 11, 29, 17, 30, tzinfo=UTC)  # day after Thanksgiving, closed 13:00 ET = 18:00 UTC
    assert bars_are_stale(now - timedelta(minutes=30), 5, now=now)
    assert not bars_are_stale(now - timedelta(minutes=30), 5, now=datetime(2024, 11, 29, 18, 30, tzinfo=UTC))


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "missing_timestamp"),
        ("2024-01-02T15:00:00Z", "invalid_timestamp"),
        (datetime(2024, 1, 2, 15, 0), "naive_timestamp"),
    ],
)
def test_missing_or_naive_timestamps_fail_closed(value, reason):
    result = assess_bar_freshness(value, 5, now=OPEN + timedelta(minutes=40))
    assert result.stale and result.reason == reason


def test_invalid_timestamps_fail_closed_even_when_market_is_closed():
    closed = datetime(2024, 1, 6, 15, 0, tzinfo=UTC)
    assert bars_are_stale(None, 5, now=closed)
    assert bars_are_stale(datetime(2024, 1, 5, 15, 0), 5, now=closed)


def test_future_timestamp_fails_closed_but_small_skew_is_tolerated():
    now = OPEN + timedelta(minutes=40)
    result = assess_bar_freshness(now + timedelta(minutes=10), 5, now=now)
    assert result.stale and result.reason == "future_timestamp"
    assert not bars_are_stale(now + timedelta(seconds=30), 5, now=now)


def test_invalid_interval_fails_closed():
    assert bars_are_stale(OPEN, 0, now=OPEN + timedelta(minutes=5))


def test_out_of_calendar_range_fails_closed():
    assert bars_are_stale(datetime(2101, 1, 4, 15, 0, tzinfo=UTC), 5, now=datetime(2101, 1, 4, 15, 5, tzinfo=UTC))


# ── runner wiring ────────────────────────────────────────────────────────────


def _runner() -> StrategyRunner:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("DB must not be called"))
    return StrategyRunner(db)


def _kwargs(*, pos_map: dict):
    strategy = MagicMock()
    strategy.type = "orb"
    strategy.params = {"session_open_utc": "14:30"}
    strategy.allowed_tickers = ["AAPL"]
    strategy.name = "s"
    engine = MagicMock()
    engine.params = strategy.params
    engine.required_bars = 4
    engine.history_days = 5
    engine.max_history_bars = 180
    engine.DATA_PROVIDER_TYPE = "equity"
    risk = MagicMock()
    risk.check_market_conditions = AsyncMock()
    allocator = SignalAllocator()
    return {
        "ticker": "AAPL", "strategy": strategy, "engine": engine, "risk": risk,
        "broker": MagicMock(), "cash": Decimal("10000"), "total": Decimal("10000"),
        "n_open": 0, "pos_map": pos_map, "all_positions": [],
        "intelligence": {"regime": {"regime": "trending_up"}},
        "allocator": allocator, "allocation_state": allocator.new_state(),
    }


def _fetch():
    d = Decimal("100")
    bars = [Bar(open=d, high=d, low=d, close=d, volume=Decimal("1000")) for _ in range(4)]
    times = [OPEN + timedelta(minutes=i) for i in range(4)]
    return AsyncMock(return_value=(bars, times, bars, times, Decimal("99"), "14:33"))


@pytest.mark.asyncio
@pytest.mark.real_bar_gate
async def test_runner_blocks_entry_on_stale_bars_before_risk_checks(monkeypatch):
    # "now" is far past the bars but still inside the 2024-01-02 session.
    monkeypatch.setattr("app.market_data.bar_freshness._utcnow", lambda: OPEN + timedelta(hours=3))
    runner = _runner()
    runner._fetch_market_context = _fetch()
    kwargs = _kwargs(pos_map={})

    result = await runner._process_ticker(**kwargs)

    assert result == (0, 0, 0)
    kwargs["risk"].check_market_conditions.assert_not_awaited()
    kwargs["engine"].generate_signal.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.real_bar_gate
async def test_runner_never_applies_stale_gate_to_protective_exit(monkeypatch):
    monkeypatch.setattr("app.market_data.bar_freshness._utcnow", lambda: OPEN + timedelta(hours=3))
    runner = _runner()
    runner._fetch_market_context = _fetch()
    runner._check_exit = AsyncMock(return_value=5)
    kwargs = _kwargs(pos_map={"AAPL": {"quantity": 5, "averagePrice": 100}})

    result = await runner._process_ticker(**kwargs)

    runner._check_exit.assert_awaited_once()
    assert result == (1, 5, 0)
