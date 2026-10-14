"""PostgreSQL proof that the scheduled reconciler and other order writers cannot overwrite
each other.

Separate database sessions stand in for separate processes. Every claim about a row lock is
checked from a second session against a real PostgreSQL server; SQLite cannot show it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import NullPool

from app.db.models import AuditLog, BrokerConnection, Order, OrderEvent, RiskEvent, User
from app.execution.engine import ExecutionEngine
from app.execution.state_machine import InvalidOrderTransition
from tests.integration.scheduled_reconciliation_cases import (
    T0,
    Brokers,
    add_connection,
    add_order,
    filled,
    install_task_environment,
    run_task,
    scope_of,
    stored,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the row-lock proof",
    ),
    pytest.mark.asyncio,
]

WAIT = 10.0
# Everything a test may leave waiting. Teardown lets all of it go, so a failing test fails
# instead of hanging on a row lock.
_OPEN_GATES: list[asyncio.Event] = []
_BACKGROUND: list[asyncio.Task[Any]] = []


def in_background(work: Any) -> asyncio.Task[Any]:
    task: asyncio.Task[Any] = asyncio.ensure_future(work)
    _BACKGROUND.append(task)
    return task


async def one_run() -> dict[str, Any]:
    """One run of the scheduled reconciler that fails, rather than hangs, if it is blocked."""
    async with asyncio.timeout(WAIT):
        return await run_task()


class SimulatedProcessDeath(BaseException):
    """Stands in for the worker being killed: nothing may catch it and carry on."""


class Gate:
    """Holds a broker call open until the test lets it go."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        _OPEN_GATES.append(self.release)

    async def __call__(self, _broker_order_id: str) -> Any:
        self.calls += 1
        if self.calls > 1:
            return self.answer
        self.entered.set()
        await self.release.wait()
        return self.answer


class CancellingBroker:
    """The write-capable broker a cancellation would use. Counts what it is asked to cancel."""

    environment = "demo"

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        _OPEN_GATES.append(self.release)

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_calls.append(broker_order_id)
        self.entered.set()
        await self.release.wait()


class World:
    """One test's rows: two identified accounts, so unscoped rows are never in play."""

    def __init__(self, sessions: Any, brokers: Brokers) -> None:
        self.sessions = sessions
        self.brokers = brokers
        self.ticker = f"U2B{uuid.uuid4().hex[:8].upper()}"
        self.connection: BrokerConnection
        self.other_connection: BrokerConnection
        self.scope: str

    async def order(self, broker_order_id: str, *, seconds: int = 0, **kwargs: Any) -> Order:
        async with self.sessions() as setup:
            return await add_order(
                setup,
                account_scope=self.scope,
                broker_order_id=f"{self.ticker}-{broker_order_id}",
                ticker=self.ticker,
                created_at=T0 + timedelta(seconds=seconds),
                **kwargs,
            )

    def broker_id(self, name: str) -> str:
        return f"{self.ticker}-{name}"

    @property
    def broker(self) -> Any:
        return self.brokers.for_user(self.connection.user_id)

    def reads_of(self, name: str) -> int:
        return self.broker.order_reads.count(self.broker_id(name))

    async def current(self, order: Order) -> Order:
        async with self.sessions() as reader:
            return await stored(reader, order.id)

    async def lockable(self, order: Order) -> bool:
        """True when another session can take the row lock at once."""
        async with self.sessions() as probe:
            try:
                await probe.execute(
                    text("SELECT id FROM orders WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": order.id},
                )
            except DBAPIError:
                return False
            finally:
                await probe.rollback()
        return True

    async def lock_waiters(self) -> int:
        async with self.sessions() as probe:
            result = await probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            return int(result.scalar_one())

    async def wait_for_a_lock_waiter(self) -> None:
        async with asyncio.timeout(WAIT):
            while await self.lock_waiters() == 0:
                await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    assert POSTGRES_TEST_DATABASE_URL is not None
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    brokers = Brokers()
    install_task_environment(monkeypatch, brokers, sessions)
    made = World(sessions, brokers)
    async with sessions() as setup:
        made.connection = await add_connection(setup, account_id=f"A-{made.ticker}")
        made.other_connection = await add_connection(setup, account_id=f"B-{made.ticker}")
    made.scope = str(scope_of(made.connection))
    try:
        yield made
    finally:
        for release in _OPEN_GATES:
            release.set()
        for task in _BACKGROUND:
            task.cancel()
        await asyncio.gather(*_BACKGROUND, return_exceptions=True)
        _OPEN_GATES.clear()
        _BACKGROUND.clear()
        async with sessions() as cleanup:
            order_ids = (
                (await cleanup.execute(select(Order.id).where(Order.ticker == made.ticker)))
                .scalars()
                .all()
            )
            if order_ids:
                text_ids = [str(order_id) for order_id in order_ids]
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(RiskEvent).where(RiskEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id.in_(text_ids)))
                await cleanup.execute(delete(Order).where(Order.id.in_(order_ids)))
            user_ids = [made.connection.user_id, made.other_connection.user_id]
            await cleanup.execute(
                delete(BrokerConnection).where(BrokerConnection.user_id.in_(user_ids))
            )
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()
        await engine.dispose()


async def _cancel(world: World, order: Order, broker: CancellingBroker) -> None:
    async with world.sessions() as session:
        mine = await stored(session, order.id)
        await ExecutionEngine(session, broker).cancel_order(mine)
        await session.commit()


async def test_the_order_row_is_locked_while_the_broker_is_being_asked(world: World) -> None:
    order = await world.order("x")
    gate = Gate(filled())
    world.broker.responses[world.broker_id("x")] = gate

    run = in_background(one_run())
    async with asyncio.timeout(WAIT):
        await gate.entered.wait()
    locked_during_the_read = not await world.lockable(order)
    gate.release.set()
    async with asyncio.timeout(WAIT):
        await run

    assert locked_during_the_read
    assert await world.lockable(order)
    assert (await world.current(order)).status == "filled"


async def test_a_cancellation_during_reconciliation_waits_and_then_sees_the_fill(
    world: World,
) -> None:
    order = await world.order("x")
    gate = Gate(filled())
    world.broker.responses[world.broker_id("x")] = gate
    cancelling = CancellingBroker()

    run = in_background(one_run())
    async with asyncio.timeout(WAIT):
        await gate.entered.wait()
    cancellation = in_background(_cancel(world, order, cancelling))
    await world.wait_for_a_lock_waiter()
    assert not cancellation.done()
    assert cancelling.cancel_calls == []

    gate.release.set()
    async with asyncio.timeout(WAIT):
        await run
    with pytest.raises(InvalidOrderTransition):
        async with asyncio.timeout(WAIT):
            await cancellation

    assert cancelling.cancel_calls == [], "a filled order was sent to the broker for cancelling"
    assert (await world.current(order)).status == "filled"


async def test_an_order_being_cancelled_is_skipped_unread_and_other_orders_still_run(
    world: World,
) -> None:
    being_cancelled = await world.order("x", seconds=0)
    other = await world.order("y", seconds=1)
    cancelling = CancellingBroker()
    cancelling.release.clear()
    cancellation = in_background(_cancel(world, being_cancelled, cancelling))
    async with asyncio.timeout(WAIT):
        await cancelling.entered.wait()

    summary = await one_run()

    assert world.reads_of("x") == 0
    assert world.reads_of("y") == 1
    assert summary["skipped_locked"] == 1
    assert (await world.current(other)).last_reconciled_at is not None

    cancelling.release.set()
    async with asyncio.timeout(WAIT):
        await cancellation
    assert (await world.current(being_cancelled)).status == "cancelled"


async def test_a_stale_owner_cannot_touch_an_order_a_newer_owner_already_settled(
    world: World,
) -> None:
    """Two runs both believe they own reconciliation, as after a lease has expired."""
    slow = await world.order("w", seconds=0)
    settled = await world.order("x", seconds=1)
    gate = Gate({"status": "WORKING"})
    world.broker.responses[world.broker_id("w")] = gate
    world.broker.responses[world.broker_id("x")] = filled()

    stale_owner = in_background(one_run())
    async with asyncio.timeout(WAIT):
        await gate.entered.wait()
    newer_owner_summary = await one_run()
    assert (await world.current(settled)).status == "filled"

    gate.release.set()
    async with asyncio.timeout(WAIT):
        stale_owner_summary = await stale_owner

    assert world.reads_of("x") == 1, "the stale owner asked the broker about a settled order"
    assert world.reads_of("w") == 1, "the newer owner read an order the stale owner had locked"
    assert newer_owner_summary["skipped_locked"] == 1
    assert stale_owner_summary["no_longer_due"] == 1
    async with world.sessions() as reader:
        fills = (
            (
                await reader.execute(
                    select(OrderEvent).where(
                        OrderEvent.order_id == settled.id,
                        OrderEvent.event_type == "reconciled_fill",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(fills) == 1
    assert (await world.current(slow)).last_reconciled_at is not None


async def test_a_writer_holding_stale_state_is_refused_rather_than_overwriting_the_fill(
    world: World,
) -> None:
    order = await world.order("x")
    world.broker.responses[world.broker_id("x")] = filled()

    async with world.sessions() as stale_writer:
        remembered = await stored(stale_writer, order.id)
        # End the read without forgetting what was read, as a long-lived writer would.
        await stale_writer.commit()
        await one_run()
        remembered.error_message = "written from a stale copy"
        with pytest.raises(StaleDataError):
            await stale_writer.flush()
        await stale_writer.rollback()

    after = await world.current(order)
    assert after.status == "filled"
    assert after.error_message is None


async def test_saved_orders_survive_a_crash_and_the_crashed_orders_lock_is_released(
    world: World,
) -> None:
    first = await world.order("first", seconds=0)
    second = await world.order("second", seconds=1)
    world.broker.responses[world.broker_id("first")] = filled()
    world.broker.responses[world.broker_id("second")] = SimulatedProcessDeath()

    with pytest.raises(SimulatedProcessDeath):
        await one_run()

    assert (await world.current(first)).status == "filled"
    interrupted = await world.current(second)
    assert interrupted.status == "accepted"
    assert interrupted.retry_count == 0
    assert await world.lockable(second)


async def test_a_run_cancelled_twice_releases_the_row_and_writes_nothing(world: World) -> None:
    order = await world.order("x")
    gate = Gate(filled())
    world.broker.responses[world.broker_id("x")] = gate

    run = in_background(one_run())
    async with asyncio.timeout(WAIT):
        await gate.entered.wait()
    run.cancel()
    await asyncio.sleep(0)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert await world.lockable(order)
    after = await world.current(order)
    assert after.status == "accepted"
    assert after.last_reconciled_at is None
    assert after.retry_count == 0


async def test_the_other_account_is_reconciled_while_one_account_is_held_up(
    world: World,
) -> None:
    held = await world.order("x")
    async with world.sessions() as setup:
        other = await add_order(
            setup,
            account_scope=scope_of(world.other_connection),
            broker_order_id=world.broker_id("other"),
            ticker=world.ticker,
            created_at=T0 + timedelta(seconds=5),
        )
    cancelling = CancellingBroker()
    cancelling.release.clear()
    cancellation = in_background(_cancel(world, held, cancelling))
    async with asyncio.timeout(WAIT):
        await cancelling.entered.wait()

    await one_run()

    other_broker = world.brokers.for_user(world.other_connection.user_id)
    assert other_broker.order_reads == [world.broker_id("other")]
    assert world.broker.order_reads == []
    assert (await world.current(other)).last_reconciled_at is not None

    cancelling.release.set()
    async with asyncio.timeout(WAIT):
        await cancellation
