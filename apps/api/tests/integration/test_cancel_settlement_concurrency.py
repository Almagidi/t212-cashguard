"""PostgreSQL proof that a cancellation and the reconciler settle a partial fill once.

Separate sessions stand in for separate processes, against a real server: the row lock,
what each side sees after the other commits, and what survives a process that dies or a
commit that fails after the broker accepted the cancel request.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.broker.trading212 import T212APIError
from app.db.models import Order, OrderEvent
from app.execution.engine import (
    ExecutionEngine,
    OrderCancellationFailed,
    OrderCancellationUnconfirmed,
)
from tests.integration.scheduled_reconciliation_cases import stored
from tests.integration.test_scheduled_reconciliation_concurrency import (
    _OPEN_GATES,
    POSTGRES_TEST_DATABASE_URL,
    WAIT,
    Gate,
    SimulatedProcessDeath,
    World,
    a_world,
    in_background,
    one_run,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the row-lock proof",
    ),
    pytest.mark.asyncio,
]

NOT_FOUND = T212APIError(404, "not found")


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    async with a_world(monkeypatch) as made:
        yield made


def _cancelled_after(world: World, name: str, filled: int, price: str = "10.5") -> dict[str, Any]:
    return {
        "id": world.broker_id(name),
        "ticker": world.ticker,
        "type": "LIMIT",
        "quantity": 10,
        "status": "CANCELLED",
        "filledQuantity": filled,
        "filledPrice": price,
    }


class CancelBroker:
    """The write-capable broker a cancellation uses. Its cancel request can be held open."""

    environment = "demo"

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.open_answer: Any = NOT_FOUND
        self.history: dict[str, Any] = {"items": [], "nextPagePath": None}
        self.refuse_cancel: Exception | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        _OPEN_GATES.append(self.release)

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_calls.append(broker_order_id)
        self.entered.set()
        await self.release.wait()
        if self.refuse_cancel is not None:
            raise self.refuse_cancel

    async def get_order_by_id(self, _broker_order_id: str) -> Any:
        if isinstance(self.open_answer, BaseException):
            raise self.open_answer
        return dict(self.open_answer)

    async def get_historical_orders(self, **_kwargs: Any) -> Any:
        return self.history


async def _cancel(world: World, order: Order, broker: CancelBroker) -> Order:
    async with world.sessions() as session:
        mine = await stored(session, order.id)
        result = await ExecutionEngine(session, broker).cancel_order(mine)
        await session.commit()
        return result


async def _event_count(world: World, order: Order, event_type: str) -> int:
    async with world.sessions() as reader:
        return int(
            await reader.scalar(
                select(func.count())
                .select_from(OrderEvent)
                .where(OrderEvent.order_id == order.id, OrderEvent.event_type == event_type)
            )
        )


async def test_a_cancel_holding_the_row_settles_the_partial_fill_and_the_reconciler_skips_it(
    world: World,
) -> None:
    order = await world.order("x", quantity="10")
    cancelling = CancelBroker()
    cancelling.history = {"items": [_cancelled_after(world, "x", 4)], "nextPagePath": None}
    cancelling.release.clear()

    cancellation = in_background(_cancel(world, order, cancelling))
    async with asyncio.timeout(WAIT):
        await cancelling.entered.wait()
    assert not await world.lockable(order)
    summary = await one_run()
    assert summary["skipped_locked"] == 1
    assert world.reads_of("x") == 0

    cancelling.release.set()
    async with asyncio.timeout(WAIT):
        await cancellation
    after_cancel = await world.current(order)
    assert after_cancel.status == "cancelled"
    assert after_cancel.filled_quantity == Decimal("4")
    assert after_cancel.avg_fill_price == Decimal("10.5")

    # The reconciler does not settle it again.
    await one_run()
    assert world.reads_of("x") == 0
    assert (await world.current(order)).filled_quantity == Decimal("4")
    assert await _event_count(world, order, "reconciled_status") == 1


async def test_the_reconciler_settling_first_leaves_the_cancel_nothing_to_send(
    world: World,
) -> None:
    order = await world.order("x", quantity="10")
    gate = Gate(NOT_FOUND)
    world.broker.responses[world.broker_id("x")] = gate
    world.broker.history = {"items": [_cancelled_after(world, "x", 4)]}
    cancelling = CancelBroker()

    run = in_background(one_run())
    async with asyncio.timeout(WAIT):
        await gate.entered.wait()
    cancellation = in_background(_cancel(world, order, cancelling))
    await world.wait_for_a_lock_waiter()
    assert not cancellation.done()

    gate.release.set()
    async with asyncio.timeout(WAIT):
        await run
        result = await cancellation

    # The cancel waited for the lock, then read the committed row: already cancelled.
    assert result.status == "cancelled"
    assert cancelling.cancel_calls == []
    saved = await world.current(order)
    assert saved.filled_quantity == Decimal("4")
    assert await _event_count(world, order, "reconciled_status") == 1
    assert await _event_count(world, order, "cancel_request_accepted") == 0


async def test_two_cancels_at_once_send_one_request_and_invent_nothing(world: World) -> None:
    order = await world.order("x", quantity="10")
    cancelling = CancelBroker()
    cancelling.release.clear()

    async def attempt() -> str:
        # As every caller does: the outcome is caught, and what was written is committed.
        async with world.sessions() as session:
            mine = await stored(session, order.id)
            try:
                await ExecutionEngine(session, cancelling).cancel_order(mine)
            except OrderCancellationUnconfirmed as unconfirmed:
                await session.commit()
                return unconfirmed.reason
            await session.commit()
            return "settled"

    first = in_background(attempt())
    async with asyncio.timeout(WAIT):
        await cancelling.entered.wait()
    second = in_background(attempt())
    await world.wait_for_a_lock_waiter()
    cancelling.release.set()
    async with asyncio.timeout(WAIT):
        reasons = [await first, await second]

    assert reasons == ["no_broker_record", "no_broker_record"]
    assert cancelling.cancel_calls == [world.broker_id("x")], "the second cancel asked again"
    saved = await world.current(order)
    assert saved.status == "accepted"
    assert saved.filled_quantity is None
    assert await _event_count(world, order, "cancel_request_accepted") == 1


async def test_a_process_that_dies_after_the_broker_took_the_cancel_leaves_it_to_reconciliation(
    world: World,
) -> None:
    order = await world.order("x", quantity="10")
    cancelling = CancelBroker()
    cancelling.open_answer = SimulatedProcessDeath()

    with pytest.raises(SimulatedProcessDeath):
        await _cancel(world, order, cancelling)

    # Nothing of the attempt was committed: the order is as it was, and unlocked.
    saved = await world.current(order)
    assert saved.status == "accepted"
    assert saved.filled_quantity is None
    assert await _event_count(world, order, "cancel_request_accepted") == 0
    assert await world.lockable(order)

    world.broker.responses[world.broker_id("x")] = NOT_FOUND
    world.broker.history = {"items": [_cancelled_after(world, "x", 4)]}
    await one_run()
    await one_run()

    saved = await world.current(order)
    assert saved.status == "cancelled"
    assert saved.filled_quantity == Decimal("4")
    assert await _event_count(world, order, "reconciled_status") == 1


async def test_a_commit_that_fails_after_the_final_record_settles_nothing_twice(
    world: World,
) -> None:
    order = await world.order("x", quantity="10")
    cancelling = CancelBroker()
    cancelling.history = {"items": [_cancelled_after(world, "x", 4)], "nextPagePath": None}

    async with world.sessions() as session:
        mine = await stored(session, order.id)
        await ExecutionEngine(session, cancelling).cancel_order(mine)
        await session.rollback()  # the commit did not happen

    assert (await world.current(order)).status == "accepted"
    # The broker has already cancelled it, so a second request is refused.
    cancelling.refuse_cancel = T212APIError(400, {"code": "OrderCannotBeCancelled"})
    with pytest.raises(OrderCancellationFailed) as refused:
        await _cancel(world, order, cancelling)
    assert not isinstance(refused.value, OrderCancellationUnconfirmed)
    assert (await world.current(order)).status == "accepted"

    world.broker.responses[world.broker_id("x")] = NOT_FOUND
    world.broker.history = {"items": [_cancelled_after(world, "x", 4)]}
    await one_run()

    saved = await world.current(order)
    assert saved.status == "cancelled"
    assert saved.filled_quantity == Decimal("4")
    assert await _event_count(world, order, "reconciled_status") == 1


async def test_a_local_fault_on_one_order_keeps_the_record_of_the_others_on_postgresql(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    faulty = await world.order("x", quantity="10")
    fine = await world.order("y", quantity="10", seconds=1)
    cancelling = CancelBroker()
    cancelling.history = {
        "items": [_cancelled_after(world, "x", 4), _cancelled_after(world, "y", 3)],
        "nextPagePath": None,
    }
    real = ExecutionEngine._record_final_state

    async def fails_after_changing_x(self, order, record, found):
        await real(self, order, record, found)
        if order.broker_order_id == world.broker_id("x"):
            await self.db.flush()
            raise RuntimeError("local fault")

    monkeypatch.setattr(ExecutionEngine, "_record_final_state", fails_after_changing_x)

    async with world.sessions() as session:
        mine = [await stored(session, order.id) for order in (faulty, fine)]
        batch = await ExecutionEngine(session, cancelling).cancel_many(mine)
        reported = [(o.broker_order_id, o.status, r) for o, r in batch.unconfirmed]
        await session.commit()

    assert reported == [(world.broker_id("x"), "accepted", "local_error")]
    assert (await world.current(faulty)).status == "accepted"
    assert (await world.current(faulty)).filled_quantity is None
    assert (await world.current(fine)).filled_quantity == Decimal("3")
    assert await _event_count(world, faulty, "cancel_request_accepted") == 1
