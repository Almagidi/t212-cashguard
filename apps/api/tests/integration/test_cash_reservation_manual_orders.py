"""PostgreSQL proof, through the manual order route, that requests arriving together cannot
both be admitted against the same cash.

The route, the risk checks with the default risk limits, intent creation and submission are
the real code. Only the broker is replaced, by an in-memory double.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response

from app.api.schemas import OrderCreate
from app.api.v1.routes import orders as route
from app.db.models import Order, User
from tests.integration.cash_reservation_cases import (
    POSTGRES_TEST_DATABASE_URL,
    WAIT,
    Account,
    World,
    open_world,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the cash reservation proof",
    ),
    pytest.mark.asyncio,
]


class Desk:
    """The manual order route with one broker account per user."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.accounts: dict[uuid.UUID, Account] = {world.user_id: Account("one")}

        async def broker_of(*, current_user: User, db: Any) -> Account:
            return self.accounts[current_user.id]

        monkeypatch.setattr(route, "get_broker", broker_of)

    @property
    def account(self) -> Account:
        return self.accounts[self.world.user_id]

    async def second_user(self) -> uuid.UUID:
        async with self.world.sessions() as session:
            user = User(
                id=uuid.uuid4(),
                email=f"{uuid.uuid4().hex}@example.test",
                hashed_password="x",
                is_active=True,
                is_admin=True,
            )
            session.add(user)
            await session.commit()
        self.accounts[user.id] = Account("two")
        return user.id

    async def place(
        self, ticker: str, quantity: int, *, user_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """One request: its own session, committed on success and dropped on an error."""
        async with self.world.sessions() as session:
            user = await session.get(User, user_id or self.world.user_id)
            order = await route.place_order(
                OrderCreate(
                    ticker=ticker,
                    side="buy",
                    order_type="limit",
                    quantity=Decimal(quantity),
                    limit_price=Decimal("100"),
                    time_validity="DAY",
                ),
                Response(),
                current_user=user,
                db=session,
            )
            await session.commit()
            return order.id


@pytest_asyncio.fixture
async def desk(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Desk, None]:
    async with open_world(monkeypatch) as world:
        yield Desk(world, monkeypatch)


def refused_for_cash(result: object) -> bool:
    return (
        isinstance(result, HTTPException)
        and result.status_code == 422
        and "Cash reservation" in str(result.detail)
    )


async def test_one_affordable_order_is_admitted(desk: Desk) -> None:
    """Control: the setup admits an order, so a refusal below is the reservation's doing."""
    await desk.place("AAA", 6)

    (order,) = await desk.world.orders()
    assert order.status == "accepted"
    assert order.cash_used == Decimal("600")
    assert order.available_cash_at_submission == Decimal("1000")


async def test_the_route_notes_the_time_before_it_reads_the_account(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real = route.ExecutionEngine.create_order_intent

    async def recording(self: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await real(self, **kwargs)

    monkeypatch.setattr(route.ExecutionEngine, "create_order_intent", recording)
    started = datetime.now(UTC)

    await desk.place("AAA", 6)

    assert seen[0]["available_cash"] == Decimal("1000")
    assert started <= seen[0]["cash_read_at"] <= desk.account.summary_times[0]


async def test_one_unaffordable_order_is_stopped_by_the_plain_cash_check(desk: Desk) -> None:
    with pytest.raises(HTTPException, match="Cash guard"):
        await desk.place("AAA", 11)

    assert await desk.world.orders() == []


async def test_two_requests_reading_the_same_cash_together_are_not_both_admitted(
    desk: Desk,
) -> None:
    """Both requests read 1,000 free cash before either is admitted; each asks for 600."""
    desk.account.read_together = asyncio.Barrier(2)

    results = await asyncio.wait_for(
        asyncio.gather(desk.place("AAA", 6), desk.place("BBB", 6), return_exceptions=True),
        WAIT * 2,
    )

    assert desk.account.summaries == 2
    assert sum(isinstance(result, uuid.UUID) for result in results) == 1, results
    assert sum(refused_for_cash(result) for result in results) == 1, results
    orders = await desk.world.orders()
    assert [order.status for order in orders] == ["accepted"]
    assert sum(order.cash_used for order in orders) <= desk.account.cash
    assert len(desk.account.posts) == 1, "the refused order reached the broker"
    assert len(await desk.world.attempts()) == 1


async def test_a_second_request_is_refused_while_the_first_order_is_open(desk: Desk) -> None:
    await desk.place("AAA", 6)

    with pytest.raises(HTTPException) as refusal:
        await desk.place("BBB", 6)

    assert refused_for_cash(refusal.value)
    assert len(desk.account.posts) == 1
    assert [order.ticker for order in await desk.world.orders()] == ["AAA"]


async def test_an_order_filled_well_before_a_request_does_not_hold_it_back(desk: Desk) -> None:
    """The request's own reading is recent, so it already shows the earlier fill."""
    desk.account.answers["AAA"] = {"status": "FILLED", "filledQuantity": "6", "filledPrice": "100"}
    await desk.place("AAA", 6)
    assert (await desk.world.order("AAA")).status == "filled"
    await desk.world.make_older("AAA", timedelta(minutes=5))
    desk.account.cash = Decimal("400")

    await desk.place("BBB", 4)

    assert len(desk.account.posts) == 2


async def test_two_users_with_their_own_accounts_are_both_admitted(desk: Desk) -> None:
    other = await desk.second_user()
    for account in desk.accounts.values():
        account.read_together = None

    results = await asyncio.wait_for(
        asyncio.gather(
            desk.place("AAA", 6), desk.place("BBB", 6, user_id=other), return_exceptions=True
        ),
        WAIT * 2,
    )

    assert all(isinstance(result, uuid.UUID) for result in results), results
    orders = await desk.world.orders()
    assert {order.broker_account_scope for order in orders} == {
        account.account_scope for account in desk.accounts.values()
    }


async def test_the_kill_switch_stops_an_order_before_any_reservation(desk: Desk) -> None:
    await desk.world.set_kill_switch(True)

    with pytest.raises(HTTPException) as refusal:
        await desk.place("AAA", 6)

    assert not refused_for_cash(refusal.value)
    assert await desk.world.orders() == []
    assert desk.account.posts == []
    await desk.world.set_kill_switch(False)
    await desk.place("BBB", 10)
    assert [order.ticker for order in await desk.world.orders()] == ["BBB"]


async def test_the_reservation_does_not_replace_the_kill_switch(desk: Desk) -> None:
    await desk.place("AAA", 4)
    await desk.world.set_kill_switch(True)

    with pytest.raises(HTTPException) as refusal:
        await desk.place("BBB", 4)

    assert not refused_for_cash(refusal.value)
    assert len(desk.account.posts) == 1


async def test_order_rows_never_add_up_to_more_than_the_cash(desk: Desk) -> None:
    desk.account.read_together = asyncio.Barrier(4)

    results = await asyncio.wait_for(
        asyncio.gather(*(desk.place(f"T{index}", 4) for index in range(4)), return_exceptions=True),
        WAIT * 3,
    )

    assert sum(isinstance(result, uuid.UUID) for result in results) == 2, results
    assert sum(refused_for_cash(result) for result in results) == 2, results
    orders: list[Order] = await desk.world.orders()
    assert sum(order.cash_used for order in orders) == Decimal("800")
    assert len(desk.account.posts) == 2
