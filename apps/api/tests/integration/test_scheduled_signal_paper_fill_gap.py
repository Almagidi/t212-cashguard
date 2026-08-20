"""Scheduled StrategyRunner-to-PaperExecutionEngine regression coverage.

These focused tests keep market intelligence, allocation, and the outer
StrategyRunner risk object deterministic so they can isolate submission
semantics. In APP_MODE=mock, scheduled entries/exits use the local paper
ledger and realistic paper policy; demo/live retain ExecutionEngine. The
separate real_worker_paper_smoke harness proves the unstubbed Celery,
Redis, PostgreSQL, market-intelligence, allocator, RiskEngine, and paper
chain. Broker constructors remain fail-fast tripwires in both surfaces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.api.schemas import PaperOrderCreate
from app.core.config import settings
from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    Order,
    OrderEvent,
    PositionSnapshot,
    Signal,
    Strategy,
    User,
    VenueConfig,
)
from app.execution.paper_engine import PaperExecutionEngine
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
    db.add(
        User(
            id=uuid.uuid4(),
            email=settings.ADMIN_EMAIL,
            hashed_password="test-only-not-a-real-credential",
            is_active=True,
            is_admin=True,
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
    assert order.status == "filled"
    assert order.filled_quantity == order.quantity
    assert order.execution_environment == "paper_mock"
    assert order.avg_fill_price != order.expected_fill_price
    assert order.slippage_value is not None and order.slippage_value > 0
    assert order.fee_amount is not None and order.fee_amount > 0
    assert order.broker_response["status"] == "PAPER_FILLED"
    assert order.broker_response["no_broker_order_sent"] is True
    assert not order.broker_order_id

    paper_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "paper_fill_simulated"))
    ).scalar_one()
    assert paper_audit.payload["paper_only"] is True
    assert paper_audit.payload["no_broker_order_sent"] is True

    fill_event = (
        await db.execute(
            select(OrderEvent).where(
                OrderEvent.order_id == order.id,
                OrderEvent.event_type == "paper_fill_simulated",
            )
        )
    ).scalar_one()
    assert fill_event.payload["fee_amount"] == str(order.fee_amount)

    account = (await db.execute(select(BrokerAccountSnapshot))).scalar_one()
    position = (await db.execute(select(PositionSnapshot))).scalar_one()
    assert account.cash < Decimal("100000")
    assert position.ticker == "NVDA"
    assert position.quantity == order.filled_quantity

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
async def test_repeated_scheduler_tick_creates_one_signal_and_one_order(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = await _seed_open_gates(db, is_live=True)
    service = StrategyRunner(db)
    monkeypatch.setattr(service, "_fetch_market_context", _fake_market_context)

    first = await service.run_all_enabled()
    await db.commit()
    second = await service.run_all_enabled()
    await db.commit()

    signals = (
        (await db.execute(select(Signal).where(Signal.strategy_id == strategy.id))).scalars().all()
    )
    orders = (
        (
            await db.execute(
                select(Order).where(Order.signal_id.in_([signal.id for signal in signals]))
            )
        )
        .scalars()
        .all()
    )
    assert first["signals_generated"] == 1
    assert first["orders_submitted"] == 1
    assert second["signals_generated"] == 0
    assert second["orders_submitted"] == 0
    assert len(signals) == 1
    assert len(orders) == 1
    assert signals[0].decision_key is not None


@pytest.mark.asyncio
async def test_exit_decisions_dedupe_same_bar_but_allow_later_bar(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy = await _seed_open_gates(db, is_live=True)
    entry_signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="NVDA",
        side="buy",
        signal_type="entry",
        status="executed",
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        generated_at=datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
    )
    db.add(entry_signal)
    await db.commit()
    paper_user = (
        await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
    ).scalar_one()
    await PaperExecutionEngine(db).execute(
        PaperOrderCreate(
            ticker="NVDA",
            side="buy",
            quantity=Decimal("2"),
            estimated_price=Decimal("100"),
            source="scheduled_exit_test",
            strategy=strategy.name,
        ),
        user=paper_user,
        signal_id=entry_signal.id,
    )
    await db.commit()

    class _DeterministicExitEngine:
        def __init__(self, _params: dict[str, Any]) -> None:
            pass

        def check_exit_conditions(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                signal_type="partial_exit",
                suggested_quantity=Decimal("-1"),
                stop_price=Decimal("101"),
                take_profit_price=Decimal("112"),
                confidence=Decimal("0.8"),
                reason="deterministic partial exit",
            )

    monkeypatch.setattr(strategy_runner, "OpeningRangeBreakoutStrategy", _DeterministicExitEngine)
    service = StrategyRunner(db)
    broker = await service._get_broker()
    assert broker is not None
    first_bar = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    later_bar = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    call = {
        "ticker": "NVDA",
        "strategy": strategy,
        "bars": _deterministic_breakout_bars(),
        "pos_qty": Decimal("2"),
        "avg_price": Decimal("100"),
        "max_sell": Decimal("1"),
        "broker": broker,
        "risk": _AllowingRiskEngine(),
    }

    first = await service._check_exit(**call, bar_time=first_bar)
    await db.commit()
    repeated = await service._check_exit(**call, bar_time=first_bar)
    await db.commit()
    later = await service._check_exit(**call, bar_time=later_bar)
    await db.commit()

    exit_signals = (
        (
            await db.execute(
                select(Signal).where(
                    Signal.strategy_id == strategy.id,
                    Signal.side == "sell",
                )
            )
        )
        .scalars()
        .all()
    )
    exit_orders = (
        (
            await db.execute(
                select(Order).where(Order.signal_id.in_([signal.id for signal in exit_signals]))
            )
        )
        .scalars()
        .all()
    )
    assert (first, repeated, later) == (1, None, 1)
    assert len(exit_signals) == 2
    assert len(exit_orders) == 2
    assert len({signal.decision_key for signal in exit_signals}) == 2


@pytest.mark.asyncio
async def test_kill_switch_blocks_the_real_submission_path_independent_of_the_top_level_gate(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run kill-switch change blocks before any paper Order or effect."""
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

    order = await db.execute(select(Order).where(Order.signal_id == signal.id))
    assert order.scalar_one_or_none() is None

    kill_switch_audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "paper_signal_rejected"))
    ).scalar_one()
    assert kill_switch_audit.payload["decision_code"] == "kill_switch_block"
    assert kill_switch_audit.payload["no_broker_order_sent"] is True
    assert (await db.execute(select(BrokerAccountSnapshot))).scalar_one_or_none() is None
    assert (await db.execute(select(PositionSnapshot))).scalar_one_or_none() is None
