"""End to end through the real scheduled entry point: ORB in its opening window.

`StrategyRunner.run_all_enabled()` is what the `run_strategy_signals` task calls. The
strategy runs with its default parameters (nothing relaxed) on a breakout that completes at
14:50 UTC, twenty minutes after the open. Only the market-data fetch is replaced, to supply
deterministic bars; risk, allocation and intelligence are stubbed exactly as in
`test_scheduled_signal_paper_fill_gap.py` because they are not strategy gates.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Order, Signal
from app.services import strategy_runner
from app.services.strategy_runner import StrategyRunner
from app.strategies.vwap_reclaim import VWAPReclaimStrategy
from tests.integration import test_scheduled_signal_paper_fill_gap as gap_cases
from tests.unit import test_strategy_operability as operability

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.strategies.indicators import Bar

BREAKOUT_TICK = 4


@pytest.fixture(autouse=True)
def _isolate_unrelated_subsystems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strategy_runner, "MarketIntelligenceMonitor", gap_cases._NoOpMarketIntelligenceMonitor
    )
    monkeypatch.setattr(strategy_runner, "RiskEngine", lambda _db: gap_cases._AllowingRiskEngine())
    monkeypatch.setattr(strategy_runner, "SignalAllocator", gap_cases._AllowingSignalAllocator)
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", lambda *_a, **_kw: None)
    for target in ("app.broker.trading212.Trading212Adapter", "app.broker.kraken.KrakenAdapter"):
        monkeypatch.setattr(target, gap_cases._live_adapter_sentinel)
    monkeypatch.setattr(
        strategy_runner, "create_trading212_provider_adapter", gap_cases._live_adapter_sentinel
    )


def _market_context(*, history: str, session: list[Bar] | None = None) -> Any:
    previous = operability._previous_session(end_close=Decimal("100.00"), bar_range=Decimal("0.40"))
    previous_times = operability._session_times(operability.PREVIOUS_DAY)
    session = session if session is not None else operability._breakout_session(BREAKOUT_TICK)
    session_times = operability._session_times(operability.SESSION_DAY, len(session))
    if history == "full":
        history_bars, history_times = previous + session, previous_times + session_times
    elif history == "none":
        history_bars, history_times = list(session), list(session_times)
    else:  # the previous session's last bar is missing, so nothing joins up
        history_bars = previous[:-1] + session
        history_times = previous_times[:-1] + session_times

    async def fetch(*_args: Any, **_kwargs: Any) -> Any:
        return session, session_times, history_bars, history_times, previous[-1].close, "14:50"

    return fetch


async def _run(db: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, history: str) -> Any:
    strategy = await gap_cases._seed_open_gates(db, is_live=True)
    strategy.params = {}  # default ORB parameters
    await db.flush()
    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", _market_context(history=history))
    summary = await service.run_all_enabled()
    await db.commit()
    return strategy.id, summary


@pytest.mark.asyncio
async def test_default_orb_places_one_paper_order_twenty_minutes_after_the_open(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.APP_MODE == "mock"

    strategy_id, summary = await _run(db, monkeypatch, history="full")

    assert summary["errors"] == []
    assert summary["signals_generated"] == 1
    assert summary["orders_submitted"] == 1
    signal = (
        await db.execute(select(Signal).where(Signal.strategy_id == strategy_id))
    ).scalar_one()
    order = (await db.execute(select(Order).where(Order.signal_id == signal.id))).scalar_one()
    assert signal.side == "buy"
    assert signal.status == "executed"
    assert order.execution_environment == "paper_mock"
    assert order.broker_response["no_broker_order_sent"] is True
    assert not order.broker_order_id
    assert operability._session_times(operability.SESSION_DAY, BREAKOUT_TICK)[-1] == datetime(
        2026, 1, 7, 14, 45, tzinfo=UTC
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["none", "broken"])
async def test_default_orb_places_nothing_without_an_unbroken_history(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    _strategy_id, summary = await _run(db, monkeypatch, history=history)

    assert summary["errors"] == []
    assert summary["signals_generated"] == 0
    assert summary["orders_submitted"] == 0
    assert (await db.execute(select(Order))).scalars().all() == []


def _history_must_not_be_needed(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("managing an open position must not depend on indicator history")


async def _next_tick(db: AsyncSession, monkeypatch: pytest.MonkeyPatch, session: list[Bar]) -> Any:
    """The scheduler's next run, five minutes later, with one more session bar."""
    monkeypatch.setattr(strategy_runner, "continuous_indicator_bars", _history_must_not_be_needed)
    service = StrategyRunner(db)
    monkeypatch.setattr(
        service, "_fetch_market_context", _market_context(history="full", session=session)
    )
    summary = await service.run_all_enabled()
    await db.commit()
    return summary


async def _signals(db: AsyncSession, strategy_id: Any) -> list[Signal]:
    rows = await db.execute(
        select(Signal).where(Signal.strategy_id == strategy_id).order_by(Signal.generated_at)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_position_entered_in_the_opening_window_is_still_held_on_the_next_tick(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy_id, _summary = await _run(db, monkeypatch, history="full")
    (entry,) = await _signals(db, strategy_id)
    session = operability._breakout_session(BREAKOUT_TICK + 1)
    assert entry.stop_price < session[-1].close < entry.take_profit_price
    exit_checks: list[int] = []
    check_exit = strategy_runner.OpeningRangeBreakoutStrategy.check_exit_conditions

    def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
        exit_checks.append(len(args[3]))
        return check_exit(self, *args, **kwargs)

    monkeypatch.setattr(
        strategy_runner.OpeningRangeBreakoutStrategy, "check_exit_conditions", recording
    )

    summary = await _next_tick(db, monkeypatch, session)

    assert exit_checks == [BREAKOUT_TICK + 1]
    assert summary["errors"] == []
    assert [signal.side for signal in await _signals(db, strategy_id)] == ["buy"]
    assert len((await db.execute(select(Order))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_position_entered_in_the_opening_window_is_sold_when_price_reaches_its_stop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy_id, _summary = await _run(db, monkeypatch, history="full")
    (entry,) = await _signals(db, strategy_id)
    stop = entry.stop_price
    falling_bar = operability._bar("101.40", "101.45", stop - Decimal("0.30"), stop, 200_000)
    session = [*operability._breakout_session(BREAKOUT_TICK), falling_bar]

    summary = await _next_tick(db, monkeypatch, session)

    assert summary["errors"] == []
    _entry, exit_signal = await _signals(db, strategy_id)
    assert (exit_signal.side, exit_signal.signal_type) == ("sell", "stop")
    assert exit_signal.stop_price == stop
    assert len((await db.execute(select(Order))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_strategy_that_takes_no_indicator_series_never_has_one_built(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = await gap_cases._seed_open_gates(db, is_live=True)
    strategy.type = "vwap_reclaim"
    strategy.params = {}
    await db.flush()
    received: list[set[str]] = []
    generate_signal = VWAPReclaimStrategy.generate_signal

    @functools.wraps(generate_signal)
    def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
        received.append(set(kwargs))
        return generate_signal(self, *args, **kwargs)

    monkeypatch.setattr(VWAPReclaimStrategy, "generate_signal", recording)

    # Fewer than 21 bars, so only the strategy's signature keeps the series from being built.
    summary = await _next_tick(db, monkeypatch, operability._breakout_session(20))

    assert summary["errors"] == []
    assert len(received) == 1
    assert "indicator_bars" not in received[0]


@pytest.mark.asyncio
async def test_series_is_not_built_once_the_session_has_twenty_one_bars(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = await gap_cases._seed_open_gates(db, is_live=True)
    strategy.params = {}
    await db.flush()
    evaluated: list[int] = []
    generate_signal = strategy_runner.OpeningRangeBreakoutStrategy.generate_signal

    @functools.wraps(generate_signal)
    def recording(self: Any, *args: Any, **kwargs: Any) -> Any:
        evaluated.append(len(kwargs["bars"]))
        assert kwargs["indicator_bars"] is None
        return generate_signal(self, *args, **kwargs)

    monkeypatch.setattr(strategy_runner.OpeningRangeBreakoutStrategy, "generate_signal", recording)

    summary = await _next_tick(db, monkeypatch, operability._late_breakout_session(21))

    assert summary["errors"] == []
    assert evaluated == [21]
