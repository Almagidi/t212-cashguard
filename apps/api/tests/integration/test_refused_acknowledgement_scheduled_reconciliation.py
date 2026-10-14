"""PostgreSQL proof that an order left unresolved by a refused immediate answer is settled by
the scheduled reconciler, by reading only, at the order's own account.

The order is created and sent by the real execution engine; the reconciler is the real
``reconcile_pending_orders`` task body. Both brokers are in-memory doubles.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    Alert,
    AppSettings,
    AuditLog,
    BrokerConnection,
    Order,
    OrderEvent,
    OrderSubmissionAttempt,
    RiskEvent,
    User,
)
from app.execution.engine import ExecutionEngine
from tests.integration.scheduled_reconciliation_cases import (
    Brokers,
    add_connection,
    install_task_environment,
    run_task,
    scope_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the reconciliation proof",
    ),
    pytest.mark.asyncio,
]

QUANTITY = Decimal("2")


class SubmittingBroker:
    """The write-capable broker of one account. Answers the order request once."""

    environment = "demo"

    def __init__(self, account_scope: str | None, acknowledgement: Any) -> None:
        self.account_scope = account_scope
        self.acknowledgement = acknowledgement
        self.posts: list[tuple[str, Decimal]] = []

    async def place_market_order(self, ticker: str, quantity: Decimal, time_validity: str = "DAY"):
        self.posts.append((ticker, quantity))
        if isinstance(self.acknowledgement, BaseException):
            raise self.acknowledgement
        return self.acknowledgement


class World:
    def __init__(self, sessions: Any, brokers: Brokers) -> None:
        self.sessions = sessions
        self.brokers = brokers
        self.ticker = f"SEAM{uuid.uuid4().hex[:8].upper()}"
        self.broker_order_id = f"{self.ticker}-BRK"
        self.connection: BrokerConnection
        self.other_connection: BrokerConnection

    @property
    def reader(self) -> Any:
        return self.brokers.for_user(self.connection.user_id)

    @property
    def other_reader(self) -> Any:
        return self.brokers.for_user(self.other_connection.user_id)

    async def submit(self, acknowledgement: Any, *, side: str = "buy") -> SubmittingBroker:
        broker = SubmittingBroker(scope_of(self.connection), acknowledgement)
        async with self.sessions() as session:
            engine = ExecutionEngine(session, broker)
            order = await engine.create_order_intent(
                ticker=self.ticker,
                side=side,
                order_type="market",
                quantity=QUANTITY,
                is_dry_run=False,
                estimated_price=Decimal("50"),
            )
            await engine.submit_order(order)
        return broker

    async def order(self) -> Order:
        async with self.sessions() as session:
            return (
                await session.execute(select(Order).where(Order.ticker == self.ticker))
            ).scalar_one()

    async def attempts(self) -> list[OrderSubmissionAttempt]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(OrderSubmissionAttempt)
                        .join(Order, Order.id == OrderSubmissionAttempt.order_id)
                        .where(Order.ticker == self.ticker)
                    )
                )
                .scalars()
                .all()
            )

    async def events(self, event_type: str) -> list[OrderEvent]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(OrderEvent)
                        .join(Order, Order.id == OrderEvent.order_id)
                        .where(Order.ticker == self.ticker, OrderEvent.event_type == event_type)
                    )
                )
                .scalars()
                .all()
            )


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    assert POSTGRES_TEST_DATABASE_URL is not None
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    brokers = Brokers()
    install_task_environment(monkeypatch, brokers, sessions)
    made = World(sessions, brokers)

    async with sessions() as setup:
        app_settings = await setup.get(AppSettings, 1)
        created_settings = app_settings is None
        if app_settings is None:
            app_settings = AppSettings(id=1)
            setup.add(app_settings)
        previous = (app_settings.auto_trading_enabled, app_settings.kill_switch_active)
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
        await setup.commit()
        made.connection = await add_connection(setup, account_id=f"{made.ticker}-A")
        made.other_connection = await add_connection(setup, account_id=f"{made.ticker}-B")

    try:
        yield made
    finally:
        async with sessions() as cleanup:
            order_ids = (
                (await cleanup.execute(select(Order.id).where(Order.ticker == made.ticker)))
                .scalars()
                .all()
            )
            if order_ids:
                text_ids = [str(order_id) for order_id in order_ids]
                await cleanup.execute(
                    delete(OrderSubmissionAttempt).where(
                        OrderSubmissionAttempt.order_id.in_(order_ids)
                    )
                )
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(RiskEvent).where(RiskEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id.in_(text_ids)))
                await cleanup.execute(delete(Order).where(Order.id.in_(order_ids)))
            await cleanup.execute(
                delete(Alert).where(Alert.title == f"Order outcome unknown: {made.ticker}")
            )
            users = [made.connection.user_id, made.other_connection.user_id]
            await cleanup.execute(
                delete(BrokerConnection).where(BrokerConnection.user_id.in_(users))
            )
            await cleanup.execute(delete(User).where(User.id.in_(users)))
            if created_settings:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            else:
                restored = await cleanup.get(AppSettings, 1)
                restored.auto_trading_enabled, restored.kill_switch_active = previous
            await cleanup.commit()
        await engine.dispose()


def overfilled(world: World) -> dict[str, Any]:
    """FILLED for 3 when 2 were ordered: the engine must refuse it."""
    return {
        "id": world.broker_order_id,
        "status": "FILLED",
        "filledQuantity": 3,
        "filledPrice": 50.25,
    }


async def left_unresolved(world: World) -> SubmittingBroker:
    submitter = await world.submit(overfilled(world))
    order = await world.order()
    assert order.status == "submission_unknown"
    assert order.broker_order_id == world.broker_order_id
    assert order.filled_quantity is None
    assert [attempt.outcome for attempt in await world.attempts()] == ["ambiguous"]
    return submitter


def assert_read_only(world: World, submitter: SubmittingBroker) -> None:
    assert submitter.posts == [(world.ticker, QUANTITY)], "the order was sent a second time"
    assert world.brokers.all_write_calls == []
    assert world.other_reader.order_reads == [], "another account was asked about this order"
    assert world.other_reader.history_reads == []


async def test_a_valid_later_reading_settles_the_refused_order_as_filled(world: World) -> None:
    submitter = await left_unresolved(world)
    world.reader.responses[world.broker_order_id] = {
        "status": "FILLED",
        "filledQuantity": "2",
        "filledPrice": "50.5",
    }

    summary = await run_task()

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == QUANTITY
    assert order.avg_fill_price == Decimal("50.5")
    assert order.filled_at is not None
    assert world.reader.order_reads == [world.broker_order_id]
    assert summary["reconciled"] == 1
    assert_read_only(world, submitter)


async def test_a_working_reading_makes_the_refused_order_accepted(world: World) -> None:
    submitter = await left_unresolved(world)
    world.reader.responses[world.broker_order_id] = {"status": "WORKING"}

    await run_task()

    order = await world.order()
    assert order.status == "accepted"
    assert order.filled_quantity is None
    assert_read_only(world, submitter)


@pytest.mark.parametrize("quantity", ["3", "1", "0"])
async def test_a_later_reading_with_the_wrong_quantity_leaves_it_unresolved(
    world: World, quantity: str
) -> None:
    submitter = await left_unresolved(world)
    world.reader.responses[world.broker_order_id] = {
        "status": "FILLED",
        "filledQuantity": quantity,
        "filledPrice": "50.5",
    }

    summary = await run_task()

    order = await world.order()
    assert order.status == "submission_unknown", "a wrong quantity was accepted on a later read"
    assert order.filled_quantity is None
    assert order.avg_fill_price is None
    assert order.retry_count == 1, "the order was not made to wait after an unusable answer"
    assert len(await world.events("reconciliation_backoff")) == 1
    assert summary["reconciled"] == 0
    assert_read_only(world, submitter)


async def test_a_failed_read_leaves_it_unresolved_and_waiting(world: World) -> None:
    submitter = await left_unresolved(world)
    world.reader.responses[world.broker_order_id] = TimeoutError("no answer")

    await run_task()

    order = await world.order()
    assert order.status == "submission_unknown"
    assert order.broker_order_id == world.broker_order_id
    assert order.retry_count == 1
    assert_read_only(world, submitter)


async def test_without_its_own_account_connected_the_order_is_not_read(world: World) -> None:
    submitter = await left_unresolved(world)
    async with world.sessions() as session:
        connection = await session.get(BrokerConnection, world.connection.id)
        connection.is_active = False
        await session.commit()

    await run_task()

    order = await world.order()
    assert order.status == "submission_unknown"
    assert world.reader.order_reads == []
    assert_read_only(world, submitter)


async def test_a_rejected_reading_closes_the_refused_order_without_a_fill(world: World) -> None:
    submitter = await left_unresolved(world)
    world.reader.responses[world.broker_order_id] = {"status": "REJECTED"}

    await run_task()

    order = await world.order()
    assert order.status == "rejected"
    assert not order.filled_quantity, "a rejection was recorded with a fill"
    assert order.avg_fill_price is None
    assert_read_only(world, submitter)
