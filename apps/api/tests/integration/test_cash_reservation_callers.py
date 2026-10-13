"""PostgreSQL proof that the scheduled strategy runner and the portfolio rebalancer hand the
execution engine a dated cash figure and stand down when the cash is already claimed.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

import app
from app.db.models import AuditLog, Order, RiskProfile, Signal, Strategy
from app.execution.cash_reservation import CashReservationRefused
from app.services import portfolio_execution_service as portfolio_module
from app.services import strategy_runner
from app.services.portfolio_execution_service import PortfolioExecutionService
from app.services.safety_policy import SafetyPolicyViolation
from app.services.strategy_runner import StrategyRunner
from tests.integration import test_scheduled_signal_paper_fill_gap as gap_cases
from tests.integration.cash_reservation_cases import (
    POSTGRES_TEST_DATABASE_URL,
    PRICE,
    Account,
    World,
    open_world,
)
from tests.unit.test_portfolio_execution_service import StaticPortfolioProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

needs_postgres = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the cash reservation proof",
)
APP_ROOT = Path(app.__file__).parent
# Callers that only ever create sell orders; a sell reserves no cash.
SELL_ONLY_CALLERS = {
    "services/system_control.py",
    "services/position_monitor.py",
    "services/eod_flatten.py",
}


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    async with open_world(monkeypatch) as made:
        yield made


# ── Scheduled strategy runner ────────────────────────────────────────────────


async def runner_buy(runner: StrategyRunner, account: Account, ticker: str, quantity: int) -> Any:
    return await runner._submit_strategy_order(
        broker=account,
        strategy=SimpleNamespace(name="strategy", venue="t212"),
        signal_id=None,
        ticker=ticker,
        side="buy",
        quantity=Decimal(quantity),
        estimated_price=PRICE,
        order_type="limit",
        available_cash=account.cash,
        limit_price=PRICE,
    )


@needs_postgres
@pytest.mark.asyncio
async def test_one_run_of_the_runner_cannot_spend_its_cash_reading_twice(world: World) -> None:
    """The runner reads the account once per run and uses that figure for every signal."""
    account = Account("one")
    async with world.sessions() as session:
        runner = StrategyRunner(session)
        runner._cash_read_at = datetime.now(UTC)
        first = await runner_buy(runner, account, "AAA", 6)
        with pytest.raises(CashReservationRefused):
            await runner_buy(runner, account, "BBB", 6)
        await session.commit()

    assert first.status == "accepted"
    assert account.posts == [("AAA", Decimal("6"))]
    assert [order.ticker for order in await world.orders()] == ["AAA"]


@needs_postgres
@pytest.mark.asyncio
async def test_the_runner_hands_over_the_time_it_read_the_cash(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = Account("one")
    seen: list[dict[str, Any]] = []

    async def recording_intent(_self: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(status="accepted")

    monkeypatch.setattr(
        "app.services.strategy_runner.ExecutionEngine.create_order_intent", recording_intent
    )
    async with world.sessions() as session:
        runner = StrategyRunner(session)
        runner._cash_read_at = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
        await runner_buy(runner, account, "AAA", 6)

    assert seen[0]["available_cash"] == account.cash
    assert seen[0]["cash_read_at"] == datetime(2026, 10, 12, 14, 30, tzinfo=UTC)


async def _blocked(*_args: Any, **_kwargs: Any) -> None:
    raise SafetyPolicyViolation("Trading is halted.", decision_code="kill_switch_active")


@needs_postgres
@pytest.mark.asyncio
async def test_the_runner_gives_the_admission_turn_back_when_its_order_may_not_be_sent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run goes on to other tickers in the same session; nobody may be kept waiting."""
    account = Account("one")
    async with world.sessions() as session:
        runner = StrategyRunner(session)
        runner._cash_read_at = datetime.now(UTC)
        with monkeypatch.context() as halted:
            halted.setattr("app.execution.engine.require_order_submission_allowed", _blocked)
            with pytest.raises(SafetyPolicyViolation):
                await runner_buy(runner, account, "AAA", 6)

        saved = await world.order("AAA")
        assert saved.status == "rejected", "the unsent buy was not closed and saved"
        await asyncio.wait_for(world.admit(account, "BBB", 4), 3)
    assert account.posts == [("BBB", Decimal("4"))]


@needs_postgres
@pytest.mark.asyncio
async def test_the_runner_notes_the_time_before_it_reads_the_account(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = FlatSummaryAccount("one")
    noted: list[datetime | None] = []
    monkeypatch.setattr(
        strategy_runner, "MarketIntelligenceMonitor", gap_cases._NoOpMarketIntelligenceMonitor
    )
    monkeypatch.setattr(strategy_runner, "alert_daily_summary", lambda *_a, **_kw: None)
    async with world.sessions() as session:
        session.add(
            Strategy(
                id=uuid.uuid4(),
                name="Runner Test Strategy",
                type="orb",
                is_enabled=True,
                is_live=True,
                params={},
                allowed_tickers=["AAA"],
                session_start="09:30",
                session_end="16:00",
                eod_flatten=False,
            )
        )
        await session.commit()
        runner = StrategyRunner(session)

        async def broker() -> Account:
            return account

        async def one_strategy(**_kwargs: Any) -> tuple[int, int, int]:
            noted.append(runner._cash_read_at)
            return 0, 0, 0

        monkeypatch.setattr(runner, "_get_broker", broker)
        monkeypatch.setattr(runner, "_run_strategy", one_strategy)
        started = datetime.now(UTC)
        summary = await runner.run_all_enabled()

    assert summary["strategies_run"] == 1, summary
    assert len(account.summary_times) == 1
    assert noted[0] is not None
    assert started <= noted[0] <= account.summary_times[0]


# ── Portfolio rebalancer ─────────────────────────────────────────────────────


class FlatSummaryAccount(Account):
    """The summary shape the rebalancer reads: free cash and total at the top level."""

    async def get_account_summary(self) -> dict[str, Any]:
        self.summaries += 1
        self.summary_times.append(datetime.now(UTC))
        return {"free": str(self.cash), "total": "10000", "currency": "USD"}


def record_intents(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    """Let the module's order intents through unchanged, keeping what each was given."""
    seen: list[dict[str, Any]] = []
    real = module.ExecutionEngine.create_order_intent

    async def recording(self: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await real(self, **kwargs)

    monkeypatch.setattr(module.ExecutionEngine, "create_order_intent", recording)
    return seen


async def rebalance(
    world: World, account: Account, monkeypatch: pytest.MonkeyPatch, *, again: bool = False
) -> dict:
    """One rebalance run. ``again`` runs the strategy created by an earlier call once more."""
    monkeypatch.setattr(
        portfolio_module,
        "get_live_provider",
        lambda: StaticPortfolioProvider(tickers=["SPY", "QQQ", "IWM"]),
    )

    async def promoted(_self: Any, _strategy: Any) -> tuple[bool, None]:
        return True, None

    monkeypatch.setattr(portfolio_module.StrategyPromotionService, "execution_gate", promoted)
    # The allocator scores candidates against the wall clock; it is not what is tested here.
    monkeypatch.setattr(portfolio_module, "SignalAllocator", gap_cases._AllowingSignalAllocator)
    async with world.sessions() as session:
        if not again:
            profile = RiskProfile(
                id=uuid.uuid4(),
                name="Rebalance limits",
                max_risk_per_trade_pct=Decimal("10.0"),
                max_daily_loss_pct=Decimal("10.0"),
                max_open_positions=20,
                max_position_size_pct=Decimal("40.0"),
                max_trades_per_day=50,
                stop_after_consecutive_losses=0,
                symbol_cooldown_seconds=0,
                force_flat_eod=False,
            )
            session.add(profile)
            session.add(
                Strategy(
                    id=uuid.uuid4(),
                    name="Portfolio Test Strategy",
                    type="buy_hold_core",
                    is_enabled=True,
                    is_live=True,
                    params={
                        "capital_fraction": 0.3,
                        "min_trade_value": 25,
                        "min_weight_delta_pct": 0.5,
                    },
                    allowed_tickers=["SPY", "QQQ", "IWM"],
                    session_start="09:30",
                    session_end="16:00",
                    eod_flatten=False,
                    risk_profile_id=profile.id,
                )
            )
            await session.commit()
        service = PortfolioExecutionService(session)

        async def broker() -> Account:
            return account

        monkeypatch.setattr(service, "_get_broker", broker)
        monkeypatch.setattr(
            service, "_decision_now", lambda: datetime(2026, 4, 10, 15, 0, tzinfo=UTC)
        )
        summary: dict = await service.run_all_enabled(force=True)
        await session.commit()
        return summary


async def _strategy(world: World) -> Strategy:
    async with world.sessions() as session:
        return (
            await session.execute(select(Strategy).where(Strategy.type == "buy_hold_core"))
        ).scalar_one()


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_with_free_cash_places_its_buys(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the setup really sends rebalance buys, so the refusal below is not vacuous."""
    account = FlatSummaryAccount("one", cash="5000")
    intents = record_intents(monkeypatch, portfolio_module)

    summary = await rebalance(world, account, monkeypatch)

    assert summary["errors"] == []
    assert summary["orders_submitted"] >= 1, summary
    buys = [intent for intent in intents if intent["side"] == "buy"]
    assert len(buys) == summary["orders_submitted"]
    for intent in buys:
        # The broker's own figure and the time before it was read, for every order,
        # whatever the run's own running estimate has become by then.
        assert intent["available_cash"] == Decimal("5000")
        assert intent["cash_read_at"] <= account.summary_times[0]
    assert summary["strategies_rebalanced"] == 1
    state = (await _strategy(world)).params["portfolio_execution"]
    assert state["last_status"] == "rebalanced"
    assert state["last_rebalance_signal_at"]
    assert len(account.posts) == summary["orders_submitted"]
    orders = await world.orders()
    assert sum(order.cash_used for order in orders) <= account.cash
    assert {order.available_cash_at_submission for order in orders} == {Decimal("5000")}


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_does_not_spend_cash_an_open_order_already_claims(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = FlatSummaryAccount("one", cash="5000")
    await world.admit(account, "OPEN", 45)
    assert len(account.posts) == 1

    summary = await rebalance(world, account, monkeypatch)

    assert summary["errors"] == []
    orders = await world.orders()
    assert sum(order.cash_used for order in orders) <= account.cash
    async with world.sessions() as session:
        refused = (
            (await session.execute(select(Signal).where(Signal.risk_rejected.is_(True))))
            .scalars()
            .all()
        )
        blocked = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "portfolio_rebalance_blocked")
                )
            )
            .scalars()
            .all()
        )
    assert refused, "no rebalance buy was refused although the cash was already claimed"
    assert all("Cash reservation" in signal.risk_rejection_reason for signal in refused)
    assert len(blocked) == len(refused)
    assert len(await world.refusals()) == len(refused)
    # The period is not recorded as rebalanced: a later run reads the cash again and
    # finishes the job, where recording it would leave the sleeve in cash until next period.
    assert summary["strategies_rebalanced"] == 0
    assert summary["strategies_due"] == 1
    state = (await _strategy(world)).params["portfolio_execution"]
    assert state["last_status"] == "cash_blocked"
    assert "last_rebalance_signal_at" not in state
    assert state["cash_blocked"]["attempts"] == 1


@needs_postgres
@pytest.mark.asyncio
async def test_a_cash_blocked_rebalance_is_tried_a_few_times_and_then_left_for_next_period(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = FlatSummaryAccount("one", cash="5000")
    await world.admit(account, "OPEN", 45)

    first = await rebalance(world, account, monkeypatch)
    second = await rebalance(world, account, monkeypatch, again=True)
    third = await rebalance(world, account, monkeypatch, again=True)

    assert [run["strategies_due"] for run in (first, second, third)] == [1, 1, 0]
    assert [run["strategies_rebalanced"] for run in (first, second, third)] == [0, 0, 0]
    state = (await _strategy(world)).params["portfolio_execution"]
    assert state["last_status"] == "cash_blocked"
    assert state["cash_blocked"]["attempts"] == portfolio_module.MAX_CASH_BLOCKED_ATTEMPTS == 3
    assert state["last_rebalance_signal_at"], "the period was never closed"
    assert account.posts == [("OPEN", Decimal("45"))], "a refused buy reached the broker"


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_is_not_planned_again_while_its_orders_are_still_open(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker positions do not show open orders, so a second plan would repeat them."""
    account = FlatSummaryAccount("one", cash="5000")
    first = await rebalance(world, account, monkeypatch)
    sent = list(account.posts)
    assert first["orders_submitted"] == len(sent) >= 1

    second = await rebalance(world, account, monkeypatch, again=True)

    assert second["orders_submitted"] == 0
    assert second["signals_created"] == 0
    assert account.posts == sent, "the same rebalance was sent a second time"
    assert (await _strategy(world)).params["portfolio_execution"]["last_reason"] == (
        "orders_in_flight"
    )


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_waits_for_open_orders_however_long_they_have_been_open(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = FlatSummaryAccount("one", cash="5000")
    await rebalance(world, account, monkeypatch)
    sent = list(account.posts)
    async with world.sessions() as session:
        await session.execute(
            update(Order).values(updated_at=datetime.now(UTC) - timedelta(hours=6))
        )
        await session.commit()
    assert {order.status for order in await world.orders()} == {"accepted"}

    again = await rebalance(world, account, monkeypatch, again=True)

    assert again["orders_submitted"] == 0
    assert again["signals_created"] == 0
    assert account.posts == sent, "a rebalance with orders still open was planned again"


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_waits_for_orders_that_ended_a_moment_ago(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker's positions and cash may not show a fill yet."""
    account = FlatSummaryAccount("one", cash="5000")
    await rebalance(world, account, monkeypatch)
    sent = list(account.posts)
    async with world.sessions() as session:
        await session.execute(
            update(Order).values(
                status="filled", filled_at=datetime.now(UTC), updated_at=datetime.now(UTC)
            )
        )
        await session.commit()

    again = await rebalance(world, account, monkeypatch, again=True)

    assert again["orders_submitted"] == 0
    assert again["signals_created"] == 0
    assert account.posts == sent, "a rebalance was planned again before its fills had settled"


@needs_postgres
@pytest.mark.asyncio
async def test_an_intent_that_was_never_sent_does_not_hold_the_rebalance_up_for_ever(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sell blocked before dispatch stays a pending intent; nothing sends it later."""
    account = FlatSummaryAccount("one", cash="5000")
    await rebalance(world, account, monkeypatch)
    async with world.sessions() as session:
        await session.execute(
            update(Order).values(
                status="pending_intent",
                broker_order_id=None,
                updated_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        await session.commit()

    again = await rebalance(world, account, monkeypatch, again=True)

    assert again["skipped"] == []
    assert again["strategies_due"] == 1


@needs_postgres
@pytest.mark.asyncio
async def test_a_rebalance_is_planned_again_once_its_orders_have_ended_and_settled(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = FlatSummaryAccount("one", cash="5000")
    await rebalance(world, account, monkeypatch)
    async with world.sessions() as session:
        await session.execute(
            update(Order).values(
                status="filled",
                filled_at=datetime.now(UTC) - timedelta(minutes=30),
                updated_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        await session.commit()

    again = await rebalance(world, account, monkeypatch, again=True)

    assert again["skipped"] == []
    assert again["strategies_due"] == 1


# ── Every caller ──────────────────────────────────────────────────────────────


def _intent_calls(path: Path) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_order_intent"
    ]


def test_every_caller_that_can_buy_hands_over_a_dated_cash_figure() -> None:
    callers = {
        str(path.relative_to(APP_ROOT)): _intent_calls(path)
        for path in sorted(APP_ROOT.rglob("*.py"))
        if _intent_calls(path)
    }

    assert set(callers) == {
        "api/v1/routes/orders.py",
        "services/portfolio_execution_service.py",
        "services/strategy_runner.py",
        *SELL_ONLY_CALLERS,
    }, "a new caller creates order intents: decide whether it can buy"
    for name, calls in callers.items():
        for call in calls:
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            if name in SELL_ONLY_CALLERS:
                side = keywords["side"]
                assert isinstance(side, ast.Constant), name
                assert side.value == "sell", name
            elif name == "services/strategy_runner.py":
                assert None in keywords, "the runner passes its keywords as one mapping"
            else:
                assert {"available_cash", "cash_read_at"} <= set(keywords), name
    runner_source = (APP_ROOT / "services/strategy_runner.py").read_text()
    assert 'intent_kwargs["cash_read_at"] = self._cash_read_at' in runner_source
