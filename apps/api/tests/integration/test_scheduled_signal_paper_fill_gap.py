"""
Regression coverage for the scheduled signal-to-paper-fill gap.

StrategyRunner._process_ticker creates a real Order via ExecutionEngine and
calls submit_order() without ever passing is_dry_run=(APP_MODE == "mock"),
unlike every other order-creation call site in this codebase (e.g.
app/services/position_monitor.py:506,607). In APP_MODE=mock this means an
enabled, is_live=True strategy that reaches order submission through the
real scheduled path (app/workers/tasks.py:run_strategy_signals ->
StrategyRunner.run_all_enabled()) does NOT produce a paper fill -- it
errors out inside require_order_submission_allowed()'s mock-mode broker
block, and the Order is orphaned at status="pending_intent" forever.

These tests pin down that exact behavior end-to-end against a real DB, the
real ExecutionEngine, and the real MockBrokerAdapter (nothing in the
order-creation / execution / safety-policy layer is mocked or stubbed), so
that:
  1. The gap is documented in code and won't silently regress further.
  2. Whoever adds the missing is_dry_run guard to strategy_runner.py has a
     test that will need to flip from "documents the gap" to "proves a
     fill" -- forcing a deliberate, reviewed change instead of a silent one.

Only MarketIntelligenceMonitor and RiskEngine are stubbed out (matching the
precedent set in test_strategy_runner_provider_equivalence.py), since
neither is part of the order-submission / broker-safety path this test
targets, and the real RiskEngine needs unrelated RiskProfile/portfolio
state to run.

See docs/SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md for the full writeup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order, Signal, Strategy, VenueConfig
from app.services import strategy_runner
from app.services.strategy_runner import StrategyRunner
from app.strategies.indicators import Bar
from app.strategies.orb_production import DEFAULT_PARAMS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Relaxed ORB params -- disables session/regime filters that would otherwise
# make signal generation depend on wall-clock time or luck. Mirrors the
# RELAXED fixture in test_orb_production.py.
RELAXED_ORB_PARAMS = {
    **DEFAULT_PARAMS,
    "avoid_first_minutes": 0,
    "avoid_last_minutes": 0,
    "avoid_lunch": False,
    "min_rvol": 1.0,
    "min_atr_pct": 0.0,
    "max_atr_pct": 100.0,
    "max_gap_pct": 100.0,
    "min_range_pct": 0.0,
    "max_range_pct": 100.0,
    "require_trend": False,
    "reward_risk_ratio_min": 1.0,
}


def _bar(o: float, h: float, low: float, c: float, v: float) -> Bar:
    return Bar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _deterministic_breakout_bars() -> list[Bar]:
    # Empirically verified against OpeningRangeBreakoutStrategy(RELAXED_ORB_PARAMS):
    # opening range 97-103, then a sustained close above the range high on
    # strong volume -- deterministically yields a "buy" entry signal.
    orb = [_bar(100, 103, 97, 101, 20_000)] * 3
    rest = [_bar(103, 108, 102, 107, 30_000)] * 22
    return orb + rest


async def _fake_market_context(
    *_args: Any, **_kwargs: Any
) -> tuple[list[Bar], list[datetime], list[Bar], list[datetime], Decimal | None, str]:
    bars = _deterministic_breakout_bars()
    times = [datetime(2026, 1, 2, 14, minute, tzinfo=UTC) for minute in range(len(bars))]
    return bars, times, bars, times, None, "15:00"


class _AllowingRiskEngine:
    """Permissive RiskEngine stand-in. Not part of the order-submission path under test."""

    async def check_market_conditions(self, **_kwargs: Any) -> None:
        return None

    async def run_all_checks(self, **_kwargs: Any) -> None:
        return None

    async def check_sector_and_correlation(self, **_kwargs: Any) -> None:
        return None

    async def check_kill_switch(self) -> None:
        return None


class _NoOpMarketIntelligenceMonitor:
    def __init__(self, _db: AsyncSession) -> None:
        pass

    async def evaluate_and_alert(self) -> dict[str, Any]:
        return {"regime": {"regime": "test"}}


class _AllowingSignalAllocator:
    """Always-allocate SignalAllocator stand-in. The real allocator's
    portfolio-heat scoring/threshold logic isn't part of the
    order-submission path this test targets (same rationale as
    _AllowingRiskEngine above)."""

    def new_state(self) -> object:
        return object()

    def allocate_one(self, *_args: Any, **_kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            status="allocated",
            reason="allocated",
            score=Decimal("1"),
            to_payload=lambda: {"status": "allocated", "reason": "allocated"},
        )


@pytest.fixture(autouse=True)
def _isolate_unrelated_subsystems(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the subsystems this module isn't targeting (matches the
    precedent in test_strategy_runner_provider_equivalence.py). The
    order-creation / ExecutionEngine / MockBrokerAdapter / safety_policy
    chain is left completely real."""
    monkeypatch.setattr(
        strategy_runner, "MarketIntelligenceMonitor", _NoOpMarketIntelligenceMonitor
    )
    monkeypatch.setattr(strategy_runner, "RiskEngine", lambda _db: _AllowingRiskEngine())
    monkeypatch.setattr(strategy_runner, "SignalAllocator", _AllowingSignalAllocator)
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", lambda *_a, **_kw: None)


async def _seed_open_gates(db: AsyncSession, *, is_live: bool) -> Strategy:
    """Open every gate that would otherwise block run_all_enabled() before
    reaching order submission: app settings, venue config, and a single
    enabled strategy. Mirrors the seeding pattern in test_operator_status_api.py."""
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    db.add(
        VenueConfig(
            venue="t212",
            kill_switch_active=False,
            auto_trading_enabled=True,
            degraded_mode_active=False,
        )
    )
    strategy = Strategy(
        id=uuid.uuid4(),
        name="Agent A Observation ORB",
        type="orb",
        is_enabled=True,
        is_live=is_live,
        params=RELAXED_ORB_PARAMS,
        # NVDA (not AAPL/MSFT) -- MockBrokerAdapter seeds fake existing
        # positions for AAPL and MSFT, which would route _process_ticker
        # into the exit-check branch instead of the entry-signal branch.
        allowed_tickers=["NVDA"],
        venue="t212",
    )
    db.add(strategy)
    await db.flush()
    return strategy


@pytest.mark.asyncio
async def test_scheduled_live_strategy_signal_errors_instead_of_paper_filling(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Documents the current (unintended) behavior: an enabled, is_live=True
    strategy that reaches a real signal through the real scheduled
    entrypoint (StrategyRunner.run_all_enabled(), the same method
    app/workers/tasks.py:run_strategy_signals calls) does NOT end in a
    paper/mock fill in APP_MODE=mock. It errors inside
    require_order_submission_allowed()'s mock-mode broker block, because
    strategy_runner.py's create_order_intent() call never passes
    is_dry_run=(APP_MODE == "mock") the way every other order-creation call
    site in the codebase does.

    If this test starts failing because an Order now reaches
    status="filled", that means the missing is_dry_run guard was added to
    strategy_runner.py -- update this test (and
    docs/SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md) to assert the new,
    fixed behavior instead of "fixing" the test back to pass.
    """
    assert settings.APP_MODE == "mock"
    strategy = await _seed_open_gates(db, is_live=True)
    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", _fake_market_context)

    summary = await service.run_all_enabled()
    await db.commit()

    assert summary["strategies_run"] == 1
    assert summary["signals_generated"] == 1
    assert summary["orders_submitted"] == 0, (
        "orders_submitted > 0 means the mock-mode paper-fill gap has been "
        "fixed -- see this test's docstring."
    )
    assert summary["errors"] == []  # the failure surfaces on the Signal, not summary["errors"]

    signal = (
        await db.execute(select(Signal).where(Signal.strategy_id == strategy.id))
    ).scalar_one()
    assert signal.status == "error"
    assert signal.risk_rejection_reason is not None
    assert "APP_MODE=mock must not call real broker endpoints" in signal.risk_rejection_reason

    order = (await db.execute(select(Order).where(Order.signal_id == signal.id))).scalar_one()
    assert order.status == "pending_intent"  # created, then permanently orphaned -- never filled
    assert order.is_dry_run is False

    blocked_audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "order_blocked_by_runtime_policy")
        )
    ).scalar_one()
    assert blocked_audit.payload["decision_code"] == "mock_broker_block"
    assert blocked_audit.payload["no_broker_order_sent"] is True

    # No live-broker call was made, and no success audit trail was written.
    placed_audit = await db.execute(
        select(AuditLog).where(AuditLog.action == "strategy_order_placed")
    )
    assert placed_audit.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_kill_switch_blocks_the_real_submission_path_independent_of_the_top_level_gate(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    require_order_submission_allowed() re-checks AppSettings.kill_switch_active
    itself, independent of run_all_enabled()'s own top-level gate -- a
    defense-in-depth check for the case where the kill switch is flipped on
    mid-run. This forces that scenario (top-level gate patched permissive,
    real DB row has kill_switch_active=True) to prove the *real*
    ExecutionEngine.submit_order() -> require_order_submission_allowed()
    path still blocks -- with no live broker call, and ahead of the
    mock-mode gap covered by the test above -- when the kill switch is
    active.
    """
    strategy = await _seed_open_gates(db, is_live=True)
    app_settings_row = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one()
    app_settings_row.kill_switch_active = True
    await db.flush()

    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", _fake_market_context)

    class _PermissiveTopGateSettings:
        auto_trading_enabled = True
        kill_switch_active = False  # bypass run_all_enabled's own top gate only
        live_trading_unlocked = False

    async def _permissive_get_settings() -> Any:
        return _PermissiveTopGateSettings()

    monkeypatch.setattr(service, "_get_settings", _permissive_get_settings)

    summary = await service.run_all_enabled()
    await db.commit()

    assert summary["orders_submitted"] == 0

    signal = (
        await db.execute(select(Signal).where(Signal.strategy_id == strategy.id))
    ).scalar_one()
    assert signal.status == "error"
    assert signal.risk_rejection_reason is not None
    assert "Kill switch is active" in signal.risk_rejection_reason

    order = (await db.execute(select(Order).where(Order.signal_id == signal.id))).scalar_one()
    assert order.status == "pending_intent"

    kill_switch_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "order_blocked_by_kill_switch"))
    ).scalar_one()
    assert kill_switch_audit.payload["decision"] == "blocked"

    # The kill switch short-circuits before the mock-broker check is reached.
    mock_broker_audit = await db.execute(
        select(AuditLog).where(AuditLog.action == "order_blocked_by_runtime_policy")
    )
    assert mock_broker_audit.scalar_one_or_none() is None
