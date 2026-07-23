"""
Regression coverage for the scheduled signal-to-paper-fill gap (now fixed).

StrategyRunner._process_ticker and _check_exit each create a real Order via
ExecutionEngine.create_order_intent() and call submit_order(). Both call
sites now pass is_dry_run=(APP_MODE == "mock"), matching every other
order-creation call site in this codebase (e.g.
app/services/position_monitor.py:506,607, app/api/v1/routes/orders.py:359,
app/services/system_control.py:246). In APP_MODE=mock this means an
enabled, is_live=True strategy that reaches order submission through the
real scheduled path (app/workers/tasks.py:run_strategy_signals ->
StrategyRunner.run_all_enabled()) now produces a real paper/mock fill
instead of erroring out inside require_order_submission_allowed()'s
mock-mode broker block and orphaning the Order at status="pending_intent"
forever.

These tests pin down that exact behavior end-to-end against a real DB, the
real ExecutionEngine, real safety_policy gates, and the real
MockBrokerAdapter (nothing in the order-creation / execution /
safety-policy layer is mocked or stubbed), so that:
  1. The success path is proven and locked in by a regression test, not
     just a manual observation.
  2. Anyone who removes the is_dry_run guard from strategy_runner.py (or
     changes the safety-policy dry-run short-circuit) breaks a test loudly
     instead of silently reopening the gap.
  3. The kill switch is proven to still block order submission even for
     dry-run orders -- is_dry_run does not bypass safety_policy's kill
     switch check, only the broker-environment/live-readiness checks that
     come after it.

Only MarketIntelligenceMonitor and RiskEngine are stubbed out (matching the
precedent set in test_strategy_runner_provider_equivalence.py), since
neither is part of the order-submission / broker-safety path this test
targets, and the real RiskEngine needs unrelated RiskProfile/portfolio
state to run.

"No live broker adapter/provider is invoked" is proven two ways:
  1. Sentinels on Trading212Adapter, KrakenAdapter, and
     create_trading212_provider_adapter (the provider factory) that raise
     if constructed -- StrategyRunner._get_broker() only reaches these on
     its non-mock branch, so a passing test proves that branch was never
     taken.
  2. The filled order's broker_response/broker_order_id fields: only
     ExecutionEngine.submit_order()'s dry-run branch sets
     broker_response == {"dry_run": True, "simulated": True}; the real
     broker-submission branch always overwrites broker_response with the
     broker's actual JSON reply and sets broker_order_id from it.

"Non-mock mode still does not become implicitly dry-run" is proven in
tests/unit/test_strategy_runner_provider_equivalence.py, whose
APP_MODE="demo" fixture now asserts is_dry_run=False in the captured
create_order_intent() kwargs at both call sites -- see
test_process_ticker_live_entry_routes_order_through_execution_engine_only
and test_check_exit_live_routes_sell_order_through_execution_engine_only.

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


def _live_adapter_sentinel(*_args: Any, **_kwargs: Any) -> Any:
    """Fails loudly if a live broker adapter/provider is ever constructed.

    StrategyRunner._get_broker() only reaches Trading212Adapter,
    KrakenAdapter, or the provider factory on its non-mock branch -- in
    APP_MODE=mock it returns MockBrokerAdapter() before any of these are
    touched. Patching all three here means a passing test is direct proof
    that branch was never taken."""
    raise AssertionError("live broker adapter/provider must not be constructed in APP_MODE=mock")


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
async def test_scheduled_live_strategy_signal_reaches_mock_paper_fill(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Proves the fix: an enabled, is_live=True strategy that reaches a real
    signal through the real scheduled entrypoint (StrategyRunner.run_all_enabled(),
    the same method app/workers/tasks.py:run_strategy_signals calls) now
    DOES end in a paper/mock fill in APP_MODE=mock, because
    strategy_runner.py's create_order_intent() call passes
    is_dry_run=(APP_MODE == "mock") the way every other order-creation call
    site in the codebase does.

    If this test starts failing because an Order is stuck at
    status="pending_intent" or a Signal ends status="error" again, that
    means the is_dry_run guard was removed from strategy_runner.py -- fix
    the production code, not this test.
    """
    assert settings.APP_MODE == "mock"
    monkeypatch.setattr("app.broker.trading212.Trading212Adapter", _live_adapter_sentinel)
    monkeypatch.setattr("app.broker.kraken.KrakenAdapter", _live_adapter_sentinel)
    monkeypatch.setattr(
        strategy_runner, "create_trading212_provider_adapter", _live_adapter_sentinel
    )

    strategy = await _seed_open_gates(db, is_live=True)
    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", _fake_market_context)

    summary = await service.run_all_enabled()
    await db.commit()

    assert summary["strategies_run"] == 1
    assert summary["signals_generated"] == 1
    assert summary["orders_submitted"] == 1, (
        "orders_submitted == 0 means the mock-mode paper-fill gap has "
        "regressed -- see this test's docstring."
    )
    assert summary["errors"] == []

    signal = (
        await db.execute(select(Signal).where(Signal.strategy_id == strategy.id))
    ).scalar_one()
    assert signal.status == "executed"
    assert signal.risk_rejection_reason is None

    order = (await db.execute(select(Order).where(Order.signal_id == signal.id))).scalar_one()
    assert order.is_dry_run is True
    assert order.status == "filled"  # no longer stuck at pending_intent
    assert order.filled_quantity == order.quantity
    assert order.avg_fill_price is not None
    # broker_response/broker_order_id are only ever set this way by
    # ExecutionEngine.submit_order()'s dry-run branch -- the real-submission
    # branch always overwrites broker_response with the broker's actual JSON
    # reply and sets broker_order_id from it. Their presence here, combined
    # with the Trading212Adapter/KrakenAdapter/provider-factory sentinels
    # above never firing, is direct proof no live broker call occurred.
    assert order.broker_response == {"dry_run": True, "simulated": True}
    assert not order.broker_order_id

    simulated_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "order_submitted"))
    ).scalar_one()
    assert simulated_audit.payload["decision"] == "simulated"
    assert simulated_audit.payload["is_dry_run"] is True
    assert simulated_audit.payload["no_broker_order_sent"] is True
    assert (
        simulated_audit.payload["reason"]
        == "Dry-run order simulated locally. No broker order sent."
    )

    placed_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "strategy_order_placed"))
    ).scalar_one()
    assert placed_audit.payload["ticker"] == "NVDA"
    assert placed_audit.payload["side"] == "buy"

    # The mock-broker block that used to fire (order_blocked_by_runtime_policy,
    # decision_code=mock_broker_block) is unreachable now: is_dry_run makes
    # require_order_submission_allowed() return before that check runs.
    blocked_audit = await db.execute(
        select(AuditLog).where(AuditLog.action == "order_blocked_by_runtime_policy")
    )
    assert blocked_audit.scalar_one_or_none() is None


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
    path still blocks -- with no live broker call -- when the kill switch is
    active.

    Order creation still passes is_dry_run=True (APP_MODE=mock) here, same
    as the success-path test above -- proving the kill switch check in
    require_order_submission_allowed() runs and blocks *before* the
    `if order.is_dry_run: return` early-exit, so is_dry_run never bypasses
    the kill switch.
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
    assert order.is_dry_run is True  # dry-run intent created, but never reached fill

    kill_switch_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "order_blocked_by_kill_switch"))
    ).scalar_one()
    assert kill_switch_audit.payload["decision"] == "blocked"

    # The kill switch short-circuits before the mock-broker check is reached.
    mock_broker_audit = await db.execute(
        select(AuditLog).where(AuditLog.action == "order_blocked_by_runtime_policy")
    )
    assert mock_broker_audit.scalar_one_or_none() is None
