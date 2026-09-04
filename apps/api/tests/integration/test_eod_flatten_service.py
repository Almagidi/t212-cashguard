"""Safe EOD attribution, replay, and recovery integration proofs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.broker.provider import BrokerProviderRequest, trading212_account_scope
from app.core.config import settings
from app.db.models import (
    Alert,
    AppSettings,
    EodFlattenOperation,
    Order,
    RiskEvent,
    Signal,
    Strategy,
)
from app.execution.engine import ExecutionEngine
from app.services.eod_flatten import EodFlattenService
from app.services.position_monitor import PositionMonitor
from app.services.strategy_runner import StrategyRunner

DUE_AT = datetime(2026, 7, 6, 20, 5, tzinfo=UTC)


class SnapshotOnlyBroker:
    """Broker tripwire: EOD tests may read a snapshot but never write directly."""

    environment = "demo"
    account_scope = "trading212:demo:user:test-admin"

    def __init__(self, positions: list[dict[str, object]]) -> None:
        self.positions = positions
        self.read_calls = 0
        self.write_calls = 0

    async def get_positions(self) -> list[dict[str, object]]:
        self.read_calls += 1
        return self.positions

    async def __aenter__(self) -> SnapshotOnlyBroker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def place_market_order(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.write_calls += 1
        ticker = str(args[0])
        position = next(row for row in self.positions if row["ticker"] == ticker)
        return {
            "id": f"fake-eod-{self.write_calls}",
            "status": "FILLED",
            "filledQuantity": str(abs(Decimal(str(args[1])))),
            "filledPrice": str(position["currentPrice"]),
        }


@pytest.fixture(autouse=True)
def _demo_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")


async def _ready_settings(db, *, kill_switch_active: bool = False) -> None:
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=kill_switch_active,
            live_trading_unlocked=False,
        )
    )
    await db.flush()


async def _strategy(db, name: str, *, venue: str = "t212") -> Strategy:
    strategy = Strategy(
        id=uuid.uuid4(),
        name=name,
        type="orb",
        params={},
        venue=venue,
        session_end="16:00",
        eod_flatten=True,
        is_enabled=True,
        is_live=True,
    )
    db.add(strategy)
    await db.flush()
    return strategy


async def _filled_strategy_order(
    db,
    strategy: Strategy,
    *,
    ticker: str,
    side: str,
    quantity: str,
) -> Order:
    signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker=ticker,
        side=side,
        signal_type="entry" if side == "buy" else "exit",
        status="executed",
    )
    order = Order(
        id=uuid.uuid4(),
        signal=signal,
        client_order_key=uuid.uuid4().hex,
        ticker=ticker,
        side=side,
        order_type="market",
        quantity=Decimal(quantity),
        filled_quantity=Decimal(quantity),
        status="filled",
        venue=strategy.venue,
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        is_dry_run=False,
    )
    db.add_all([signal, order])
    await db.flush()
    return order


@pytest.mark.asyncio
async def test_flattens_only_strategy_attributable_quantity_and_leaves_manual_holding(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Scoped EOD")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    broker = SnapshotOnlyBroker(
        [
            {
                "ticker": "AAPL",
                "quantity": "5",
                "maxSell": "5",
                "currentPrice": "190",
            },
            {"ticker": "MANUAL", "quantity": "9", "maxSell": "9", "currentPrice": "10"},
        ]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    eod_order = (
        await db.execute(
            select(Order).join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
        )
    ).scalar_one()
    assert summary["flattened"] == 1
    assert eod_order.ticker == "AAPL"
    assert eod_order.quantity == Decimal("2")
    assert broker.read_calls == 1
    assert broker.write_calls == 1


@pytest.mark.asyncio
async def test_shared_ticker_strategies_get_distinct_scoped_orders_without_over_liquidation(
    db,
) -> None:
    await _ready_settings(db)
    first = await _strategy(db, "Shared one")
    second = await _strategy(db, "Shared two")
    await _filled_strategy_order(db, first, ticker="MSFT", side="buy", quantity="2")
    await _filled_strategy_order(db, second, ticker="MSFT", side="buy", quantity="3")
    broker = SnapshotOnlyBroker(
        [{"ticker": "MSFT", "quantity": "8", "maxSell": "8", "currentPrice": "410"}]
    )

    summary = await EodFlattenService(db, broker).run([first, second], now_utc=DUE_AT)

    operations = (
        (await db.execute(select(EodFlattenOperation).order_by(EodFlattenOperation.strategy_id)))
        .scalars()
        .all()
    )
    orders = (
        (
            await db.execute(
                select(Order)
                .join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
                .order_by(Order.quantity)
            )
        )
        .scalars()
        .all()
    )
    assert summary["flattened"] == 2
    assert len(operations) == 2
    assert [order.quantity for order in orders] == [Decimal("2"), Decimal("3")]
    assert sum((order.quantity for order in orders), Decimal("0")) == Decimal("5")
    assert broker.write_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "positions",
    [
        [],
        [{"ticker": "NVDA", "quantity": "1", "maxSell": "1", "currentPrice": "180"}],
        [{"ticker": "NVDA", "quantity": "NaN", "maxSell": "2", "currentPrice": "180"}],
        [
            {"ticker": "NVDA", "quantity": "2", "maxSell": "2", "currentPrice": "180"},
            {"ticker": "NVDA", "quantity": "2", "maxSell": "2", "currentPrice": "180"},
        ],
    ],
    ids=["missing", "insufficient", "nonfinite", "duplicate"],
)
async def test_ambiguous_broker_attribution_fails_closed_with_manual_evidence(
    db,
    positions: list[dict[str, object]],
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Ambiguous EOD")
    await _filled_strategy_order(db, strategy, ticker="NVDA", side="buy", quantity="2")
    broker = SnapshotOnlyBroker(positions)

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["flattened"] == 0
    assert summary["manual_reconciliation_required"] == 1
    assert operation.order_id is None
    assert operation.status == "manual_reconciliation_required"
    assert operation.requires_manual_reconciliation is True
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_broker_snapshot_failure_fails_closed_with_manual_evidence(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Snapshot failure")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")

    class FailingSnapshotBroker(SnapshotOnlyBroker):
        async def get_positions(self) -> list[dict[str, object]]:
            self.read_calls += 1
            raise RuntimeError("synthetic snapshot outage")

    broker = FailingSnapshotBroker([])

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["flattened"] == 0
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert operation.details["reason"] == "Broker position snapshot failed with RuntimeError."
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.read_calls == 1
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_active_sell_for_ticker_blocks_flatten_as_ambiguous(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Active sell")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    db.add(
        Order(
            id=uuid.uuid4(),
            client_order_key=uuid.uuid4().hex,
            ticker="aapl",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            status="accepted",
            venue="t212",
            execution_environment="demo",
            broker_account_scope=SnapshotOnlyBroker.account_scope,
            is_dry_run=False,
        )
    )
    await db.flush()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert summary["flattened"] == 0
    assert summary["manual_reconciliation_required"] == 1
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_negative_local_attribution_fails_closed_before_broker_snapshot(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Negative ledger")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="1")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="sell", quantity="2")
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "1", "maxSell": "1", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["flattened"] == 0
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert operation.requires_manual_reconciliation is True
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_ten_ticks_terminal_order_and_settlement_lag_create_one_operation_and_order(
    db,
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Replay safe")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )
    service = EodFlattenService(db, broker)

    summaries = [await service.run([strategy], now_utc=DUE_AT) for _ in range(10)]

    assert summaries[0]["flattened"] == 1
    assert all(summary["flattened"] == 0 for summary in summaries[1:])
    assert await db.scalar(select(func.count()).select_from(EodFlattenOperation)) == 1
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Order)
            .join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
        )
        == 1
    )
    assert broker.write_calls == 1


@pytest.mark.asyncio
async def test_redelivery_after_intent_commit_requires_manual_reconciliation_without_resubmit(
    db,
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Restart recovery")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    pending_order = Order(
        id=uuid.uuid4(),
        client_order_key="restart-safe-eod-key",
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        status="pending_intent",
        venue="t212",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        is_dry_run=False,
    )
    db.add(pending_order)
    db.add(
        EodFlattenOperation(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            venue="t212",
            exchange="XNYS",
            execution_environment="demo",
            broker_account_scope=SnapshotOnlyBroker.account_scope,
            exchange_session_date=DUE_AT.date(),
            ticker="AAPL",
            attributable_quantity=Decimal("2"),
            order=pending_order,
            status="intent_persisted",
        )
    )
    await db.commit()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["flattened"] == 0
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert operation.requires_manual_reconciliation is True
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_same_session_reentry_alerts_and_never_submits_a_second_sell(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Re-entry")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )
    service = EodFlattenService(db, broker)
    await service.run([strategy], now_utc=DUE_AT)
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="1")
    broker.positions = [{"ticker": "AAPL", "quantity": "1", "maxSell": "1", "currentPrice": "191"}]

    replay = await service.run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert replay["flattened"] == 0
    assert replay["reentries_blocked"] == 1
    assert operation.status == "reentry_blocked"
    assert operation.requires_manual_reconciliation is True
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.write_calls == 1


@pytest.mark.asyncio
async def test_submission_failure_is_claimed_before_send_and_never_retried(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Failure recovery")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    monkeypatch.setattr(settings, "APP_MODE", "demo")

    original_commit = db.commit
    commit_count = 0

    async def counting_commit() -> None:
        nonlocal commit_count
        await original_commit()
        commit_count += 1

    monkeypatch.setattr(db, "commit", counting_commit)

    class FailingDemoBroker(SnapshotOnlyBroker):
        environment = "demo"

        async def place_market_order(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            self.write_calls += 1
            assert commit_count >= 1, "operation and intent must commit before broker send"
            operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
            assert operation.status == "intent_persisted"
            assert operation.order_id is not None
            raise RuntimeError("synthetic ambiguous transport failure")

    broker = FailingDemoBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )
    service = EodFlattenService(db, broker)

    first = await service.run([strategy], now_utc=DUE_AT)
    replay = await service.run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert first["manual_reconciliation_required"] == 1
    assert replay["flattened"] == 0
    assert operation.status == "manual_reconciliation_required"
    assert operation.requires_manual_reconciliation is True
    assert broker.write_calls == 1
    assert await db.scalar(select(func.count()).select_from(EodFlattenOperation)) == 1


@pytest.mark.asyncio
async def test_attribution_excludes_paper_and_other_account_fills(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Scoped history")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    for environment, account_scope, venue, is_dry_run, quantity in (
        ("paper_mock", "paper:mock:user:someone", "paper", True, "100"),
        ("demo", "trading212:demo:user:other", "t212", False, "50"),
    ):
        signal = Signal(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            ticker="AAPL",
            side="buy",
            signal_type="entry",
            status="executed",
        )
        db.add_all(
            [
                signal,
                Order(
                    id=uuid.uuid4(),
                    signal=signal,
                    client_order_key=uuid.uuid4().hex,
                    ticker="AAPL",
                    side="buy",
                    order_type="market",
                    quantity=Decimal(quantity),
                    filled_quantity=Decimal(quantity),
                    status="filled",
                    venue=venue,
                    execution_environment=environment,
                    broker_account_scope=account_scope,
                    is_dry_run=is_dry_run,
                ),
            ]
        )
    await db.flush()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    eod_order = (
        await db.execute(
            select(Order).join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
        )
    ).scalar_one()
    assert summary["flattened"] == 1
    assert eod_order.quantity == Decimal("2")
    assert eod_order.execution_environment == "demo"
    assert eod_order.broker_account_scope == SnapshotOnlyBroker.account_scope


@pytest.mark.asyncio
async def test_real_strategy_runner_order_flattens_through_position_monitor(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Real producer path")
    signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="AAPL",
        side="buy",
        signal_type="entry",
        status="executed",
    )
    db.add(signal)
    await db.flush()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    entry = await StrategyRunner(db)._submit_strategy_order(
        broker=broker,
        strategy=strategy,
        signal_id=signal.id,
        ticker="AAPL",
        side="buy",
        quantity=Decimal("2"),
        estimated_price=Decimal("190"),
        order_type="market",
    )
    monitor = PositionMonitor(db)
    monkeypatch.setattr(monitor, "_get_broker", AsyncMock(return_value=broker))

    summary = await monitor.eod_flatten([strategy], now_utc=DUE_AT)

    assert entry.broker_account_scope == SnapshotOnlyBroker.account_scope
    assert summary["flattened"] == 1
    assert broker.write_calls == 2


@pytest.mark.asyncio
async def test_unscoped_fill_requires_manual_reconciliation_before_snapshot(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Legacy unscoped")
    order = await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    order.broker_account_scope = None
    await db.flush()
    broker = SnapshotOnlyBroker([])

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.read_calls == 0


@pytest.mark.asyncio
async def test_missing_active_broker_account_scope_creates_manual_evidence(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Missing active account scope")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    broker = SnapshotOnlyBroker([])
    broker.account_scope = None

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert operation.broker_account_scope is None
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_reconnected_different_account_cannot_authorize_sell_from_old_fills(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Account rotation")
    user_id = uuid.uuid4()
    account_a_scope = trading212_account_scope(
        BrokerProviderRequest(
            broker_id="trading212",
            environment="demo",
            purpose="worker_strategy_runner",
            user_id=user_id,
            account_id="ACCOUNT-A",
        )
    )
    account_b_scope = trading212_account_scope(
        BrokerProviderRequest(
            broker_id="trading212",
            environment="demo",
            purpose="worker_position_monitor",
            user_id=user_id,
            account_id="ACCOUNT-B",
        )
    )
    assert account_a_scope is not None
    assert account_b_scope is not None
    assert account_a_scope != account_b_scope
    old_order = await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    old_order.broker_account_scope = account_a_scope
    await db.flush()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )
    broker.account_scope = account_b_scope

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert summary["flattened"] == 0
    assert await db.scalar(select(func.count()).select_from(EodFlattenOperation)) == 0
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_prior_session_unresolved_operation_blocks_new_session_sell(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Prior unresolved")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    pending = Order(
        id=uuid.uuid4(),
        client_order_key=uuid.uuid4().hex,
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        status="accepted",
        venue="t212",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        is_dry_run=False,
    )
    db.add_all(
        [
            pending,
            EodFlattenOperation(
                id=uuid.uuid4(),
                strategy_id=strategy.id,
                venue="t212",
                exchange="XNYS",
                execution_environment="demo",
                broker_account_scope=SnapshotOnlyBroker.account_scope,
                exchange_session_date=datetime(2026, 7, 3, tzinfo=UTC).date(),
                ticker="AAPL",
                attributable_quantity=Decimal("2"),
                order=pending,
                status="submission_pending",
            ),
        ]
    )
    await db.commit()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert summary["flattened"] == 0
    assert await db.scalar(select(func.count()).select_from(EodFlattenOperation)) == 1
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_prior_unresolved_operations_in_other_scopes_do_not_block_current_account(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Unrelated prior scopes")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    for days_ago, environment, account_scope in (
        (2, "paper_mock", "paper:mock:user:test-admin"),
        (1, "demo", "trading212:demo:user:other"),
    ):
        db.add(
            EodFlattenOperation(
                id=uuid.uuid4(),
                strategy_id=strategy.id,
                venue="t212",
                exchange="XNYS",
                execution_environment=environment,
                broker_account_scope=account_scope,
                exchange_session_date=DUE_AT.date() - timedelta(days=days_ago),
                ticker="AAPL",
                attributable_quantity=Decimal("2"),
                status="manual_reconciliation_required",
                requires_manual_reconciliation=True,
            )
        )
    await db.commit()
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert summary["flattened"] == 1
    assert broker.write_calls == 1


@pytest.mark.asyncio
async def test_pending_operation_reconciles_filled_then_blocks_same_session_reentry(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Late fill")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    eod_order = Order(
        id=uuid.uuid4(),
        client_order_key=uuid.uuid4().hex,
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        filled_quantity=Decimal("2"),
        status="filled",
        venue="t212",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        is_dry_run=False,
    )
    operation = EodFlattenOperation(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        venue="t212",
        exchange="XNYS",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        exchange_session_date=DUE_AT.date(),
        ticker="AAPL",
        attributable_quantity=Decimal("2"),
        order=eod_order,
        status="submission_pending",
    )
    db.add_all([eod_order, operation])
    await db.commit()
    broker = SnapshotOnlyBroker([])
    service = EodFlattenService(db, broker)

    await service.run([strategy], now_utc=DUE_AT)
    assert operation.status == "completed"
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="1")
    replay = await service.run([strategy], now_utc=DUE_AT)

    assert replay["reentries_blocked"] == 1
    assert operation.status == "reentry_blocked"
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_pending_operation_terminal_failure_becomes_manual_with_evidence(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Late rejection")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    eod_order = Order(
        id=uuid.uuid4(),
        client_order_key=uuid.uuid4().hex,
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        status="rejected",
        venue="t212",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        is_dry_run=False,
    )
    operation = EodFlattenOperation(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        venue="t212",
        exchange="XNYS",
        execution_environment="demo",
        broker_account_scope=SnapshotOnlyBroker.account_scope,
        exchange_session_date=DUE_AT.date(),
        ticker="AAPL",
        attributable_quantity=Decimal("2"),
        order=eod_order,
        status="submission_pending",
    )
    db.add_all([eod_order, operation])
    await db.commit()
    broker = SnapshotOnlyBroker([])

    await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert operation.status == "manual_reconciliation_required"
    assert operation.requires_manual_reconciliation is True
    assert await db.scalar(select(func.count()).select_from(RiskEvent)) == 1
    assert await db.scalar(select(func.count()).select_from(Alert)) == 1
    assert broker.read_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "filled_quantity"),
    [("2", "-1"), ("2", "3"), ("2", "0"), ("2", None), ("-2", "1")],
)
async def test_invalid_persisted_fill_quantities_fail_closed(
    db, quantity: str, filled_quantity: str | None
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, f"Bad fill {quantity} {filled_quantity}")
    order = await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    order.quantity = Decimal(quantity)
    order.filled_quantity = Decimal(filled_quantity) if filled_quantity is not None else None
    await db.flush()
    broker = SnapshotOnlyBroker([])

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_kill_switch_activation_after_claim_blocks_broker_send(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Late kill switch")
    await _filled_strategy_order(db, strategy, ticker="AAPL", side="buy", quantity="2")
    broker = SnapshotOnlyBroker(
        [{"ticker": "AAPL", "quantity": "2", "maxSell": "2", "currentPrice": "190"}]
    )
    original_submit = ExecutionEngine.submit_order

    async def activate_then_submit(engine: ExecutionEngine, order: Order) -> Order:
        await db.execute(
            update(AppSettings)
            .where(AppSettings.id == 1)
            .values(kill_switch_active=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await original_submit(engine, order)

    monkeypatch.setattr(ExecutionEngine, "submit_order", activate_then_submit)

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["manual_reconciliation_required"] == 1
    assert operation.status == "manual_reconciliation_required"
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_mock_paper_path_fails_closed_instead_of_reporting_false_flatten(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    await _ready_settings(db)
    strategy = await _strategy(db, "Paper EOD")
    signal = Signal(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        ticker="AAPL",
        side="buy",
        signal_type="entry",
        status="executed",
    )
    db.add_all(
        [
            signal,
            Order(
                id=uuid.uuid4(),
                signal=signal,
                client_order_key=uuid.uuid4().hex,
                ticker="AAPL",
                side="buy",
                order_type="market",
                quantity=Decimal("2"),
                filled_quantity=Decimal("2"),
                status="filled",
                venue="paper",
                execution_environment="paper_mock",
                broker_account_scope="paper:mock:user:test-admin",
                is_dry_run=True,
            ),
        ]
    )
    await db.flush()
    broker = SnapshotOnlyBroker([])
    broker.environment = "mock"
    broker.account_scope = "paper:mock:user:test-admin"

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    operation = (await db.execute(select(EodFlattenOperation))).scalar_one()
    assert summary["reason"] == "paper_eod_unsupported"
    assert summary["flattened"] == 0
    assert operation.status == "manual_reconciliation_required"
    assert broker.read_calls == 0
    assert broker.write_calls == 0


@pytest.mark.asyncio
async def test_unsupported_venue_fails_closed_before_broker_snapshot(db) -> None:
    await _ready_settings(db)
    strategy = await _strategy(db, "Deferred Kraken", venue="kraken")
    await _filled_strategy_order(db, strategy, ticker="BTC/USD", side="buy", quantity="1")
    broker = SnapshotOnlyBroker(
        [{"ticker": "BTC/USD", "quantity": "1", "maxSell": "1", "currentPrice": "1"}]
    )

    summary = await EodFlattenService(db, broker).run([strategy], now_utc=DUE_AT)

    assert summary == {
        "flattened": 0,
        "operations_created": 0,
        "manual_reconciliation_required": 0,
        "reentries_blocked": 0,
        "reason": "not_due",
    }
    assert broker.read_calls == 0
    assert broker.write_calls == 0
