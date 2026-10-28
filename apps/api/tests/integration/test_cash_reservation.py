"""PostgreSQL proof that an account's buy orders can never be admitted for more cash than the
account has, however the requests arrive.

Every order goes through the real ``create_order_intent`` and ``submit_order``. Separate
database sessions stand in for separate processes, and what was saved is read back through
another session, so only committed state counts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.broker.protocols import BrokerSubmissionNotTransmitted, BrokerSubmissionRejected
from app.db.models import Order
from app.execution.cash_reservation import (
    CLOCK_TOLERANCE,
    UNDATED_CASH_FIGURE_AGE,
    CashReservationRefused,
)
from app.execution.engine import ExecutionEngine, OrderCancellationFailed
from app.services.safety_policy import SafetyPolicyViolation
from tests.integration.cash_reservation_cases import (
    POSTGRES_TEST_DATABASE_URL,
    PRICE,
    WAIT,
    Account,
    SimulatedProcessDeath,
    World,
    filled,
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

LONG_AGO = timedelta(minutes=30)


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[World, None]:
    async with open_world(monkeypatch) as made:
        yield made


async def total_admitted(world: World) -> Decimal:
    return sum((order.cash_used for order in await world.orders()), Decimal("0"))


async def test_sessions_read_committed_data_as_the_admission_turn_assumes(world: World) -> None:
    """The count made under the turn must see what the previous holder committed. Under a
    stricter isolation level it would see the transaction's first snapshot instead."""
    async with world.sessions() as session:
        level = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()

    assert level == "read committed"


# ── Two orders, one cash figure ───────────────────────────────────────────────


async def test_a_second_order_that_would_overspend_the_account_is_refused(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)

    with pytest.raises(CashReservationRefused, match="already promised"):
        await world.admit(account, "BBB", 6)

    assert [order.ticker for order in await world.orders()] == ["AAA"]
    assert account.posts == [("AAA", Decimal("6"))], "the refused order reached the broker"
    assert await total_admitted(world) <= account.cash


async def test_orders_that_fit_together_exactly_are_both_admitted(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)
    await world.admit(account, "BBB", 4)

    assert await total_admitted(world) == Decimal("1000")
    assert len(account.posts) == 2


async def test_one_cent_over_is_refused(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", "4.0001")


async def test_the_same_instrument_twice_is_counted_like_any_other(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "AAA", 5)

    assert len(await world.orders()) == 1


async def test_each_account_has_its_own_cash(world: World) -> None:
    first, second = Account("one"), Account("two")

    await world.admit(first, "AAA", 6)
    await world.admit(second, "BBB", 6)
    with pytest.raises(CashReservationRefused):
        await world.admit(first, "CCC", 6)
    with pytest.raises(CashReservationRefused):
        await world.admit(second, "DDD", 6)

    assert sorted(order.ticker for order in await world.orders()) == ["AAA", "BBB"]


async def test_an_order_recorded_without_an_account_counts_against_every_account(
    world: World,
) -> None:
    unattributed = Account("none")
    unattributed.account_scope = None
    await world.admit(unattributed, "AAA", 6)

    with pytest.raises(CashReservationRefused):
        await world.admit(Account("one"), "BBB", 6)


async def test_a_caller_without_an_account_counts_every_order(world: World) -> None:
    await world.admit(Account("one"), "AAA", 6)
    unattributed = Account("none")
    unattributed.account_scope = None

    with pytest.raises(CashReservationRefused):
        await world.admit(unattributed, "BBB", 6)


async def test_orders_of_another_environment_or_venue_do_not_count(
    world: World,
) -> None:
    from sqlalchemy import update

    account = Account("one")
    await world.admit(account, "AAA", 6, venue="kraken")
    await world.admit(account, "BBB", 6, send=False)
    # Seed a stored foreign-environment reservation; do not authorize an unbound live transport.
    async with world.sessions() as fixture:
        await fixture.execute(
            update(Order).where(Order.ticker == "BBB").values(execution_environment="live")
        )
        await fixture.commit()

    await world.admit(account, "CCC", 6)

    assert {order.ticker: order.execution_environment for order in await world.orders()} == {
        "AAA": "demo",
        "BBB": "live",
        "CCC": "demo",
    }


async def test_dry_run_and_sell_orders_neither_reserve_nor_are_refused(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 9, is_dry_run=True, send=False)
    await world.admit(account, "BBB", 9, side="sell", send=False)
    await world.admit(account, "CCC", 9, cash=0, side="sell", send=False)
    await world.admit(account, "DDD", 50, cash=0, is_dry_run=True, send=False)

    await world.admit(account, "EEE", 10)

    assert len(await world.orders()) == 5


async def test_a_buy_whose_cost_cannot_be_estimated_is_refused(world: World) -> None:
    account = Account("one")

    with pytest.raises(CashReservationRefused, match="cannot be estimated"):
        await world.admit(account, "AAA", 1, estimated_price=None, order_type="market")

    assert await world.orders() == []
    assert account.posts == []


async def test_a_real_buy_without_a_cash_figure_is_not_checked_but_still_counts(
    world: World,
) -> None:
    """Known limit: the check needs a figure. The three callers that can buy always give
    one (see the caller inventory test); an order created without one still reserves."""
    account = Account("one")
    async with world.sessions() as session:
        await ExecutionEngine(session, account).create_order_intent(
            ticker="AAA",
            side="buy",
            order_type="limit",
            quantity=Decimal("6"),
            limit_price=PRICE,
            estimated_price=PRICE,
            is_dry_run=False,
        )
        await session.commit()

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


async def test_a_refusal_is_recorded_with_its_figures(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)
    read_at = datetime.now(UTC)

    async with world.sessions() as session:
        engine = ExecutionEngine(session, account)
        with pytest.raises(CashReservationRefused) as refusal:
            await engine.create_order_intent(
                ticker="BBB",
                side="buy",
                order_type="limit",
                quantity=Decimal("6"),
                limit_price=PRICE,
                estimated_price=PRICE,
                is_dry_run=False,
                available_cash=Decimal("1000"),
                cash_read_at=read_at,
            )
        await session.commit()

    assert refusal.value.event_type == "cash_reservation_block"
    assert "600.00" in refusal.value.reason
    assert "1000.00" in refusal.value.reason
    (event,) = await world.refusals()
    assert event.ticker == "BBB"
    assert event.payload == {
        "cost": "600",
        "promised_elsewhere": "600.00000000",
        "available": "1000",
        "cash_read_at": read_at.isoformat(),
        "account_scope": account.account_scope,
    }


# ── Requests that arrive together ─────────────────────────────────────────────


async def test_two_sessions_admitting_at_once_cannot_both_spend_the_same_cash(
    world: World,
) -> None:
    account = Account("one")
    first_holds_its_turn = asyncio.Event()
    let_first_finish = asyncio.Event()

    async def first() -> None:
        async with world.sessions() as session:
            engine = ExecutionEngine(session, account)
            order = await engine.create_order_intent(
                ticker="AAA",
                side="buy",
                order_type="limit",
                quantity=Decimal("6"),
                limit_price=PRICE,
                estimated_price=PRICE,
                is_dry_run=False,
                available_cash=account.cash,
                cash_read_at=datetime.now(UTC),
            )
            first_holds_its_turn.set()
            await asyncio.wait_for(let_first_finish.wait(), WAIT)
            await engine.submit_order(order)
            await session.commit()

    first_task = asyncio.ensure_future(first())
    await asyncio.wait_for(first_holds_its_turn.wait(), WAIT)
    second_task = asyncio.ensure_future(world.admit(account, "BBB", 6))
    try:
        assert await world.is_kept_waiting(second_task), "the second did not wait for the first"
        assert await world.orders() == [], "an uncommitted order is visible to other sessions"
        let_first_finish.set()
        await asyncio.wait_for(first_task, WAIT)
        with pytest.raises(CashReservationRefused):
            await asyncio.wait_for(second_task, WAIT)
    finally:
        let_first_finish.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert [order.ticker for order in await world.orders()] == ["AAA"]
    assert account.posts == [("AAA", Decimal("6"))]


async def test_the_same_order_asked_for_again_while_the_first_is_being_admitted_is_not_doubled(
    world: World,
) -> None:
    """An operator who retries while the first request is still in progress."""
    account = Account("one", cash="5000")
    async with world.sessions() as first:
        engine = ExecutionEngine(first, account)
        order = await _open_intent(first, account, "AAA", 6)
        again = asyncio.ensure_future(world.admit(account, "AAA", 6))
        try:
            assert await world.is_kept_waiting(again)
            await engine.submit_order(order)
            await first.commit()
            returned = await asyncio.wait_for(again, WAIT)
        finally:
            await asyncio.gather(again, return_exceptions=True)

    assert returned.id == order.id, "the retry created a second order"
    assert len(await world.orders()) == 1
    assert account.posts == [("AAA", Decimal("6"))]


async def test_many_sessions_at_once_never_admit_more_than_the_cash(world: World) -> None:
    account = Account("one")

    results = await asyncio.wait_for(
        asyncio.gather(
            *(world.admit(account, f"T{index:02d}", 3) for index in range(8)),
            return_exceptions=True,
        ),
        WAIT * 3,
    )

    admitted = [result for result in results if isinstance(result, Order)]
    refused = [result for result in results if isinstance(result, CashReservationRefused)]
    assert len(admitted) == 3, results
    assert len(refused) == 5, results
    assert await total_admitted(world) == Decimal("900")
    assert len(account.posts) == 3


async def test_a_waiting_admission_proceeds_when_the_first_is_rolled_back(world: World) -> None:
    account = Account("one")
    async with world.sessions() as abandoned:
        await ExecutionEngine(abandoned, account).create_order_intent(
            ticker="AAA",
            side="buy",
            order_type="limit",
            quantity=Decimal("6"),
            limit_price=PRICE,
            estimated_price=PRICE,
            is_dry_run=False,
            available_cash=account.cash,
            cash_read_at=datetime.now(UTC),
        )
        second_task = asyncio.ensure_future(world.admit(account, "BBB", 6))
        try:
            assert await world.is_kept_waiting(second_task)
            await abandoned.rollback()
            await asyncio.wait_for(second_task, WAIT)
        finally:
            await asyncio.gather(second_task, return_exceptions=True)

    assert [order.ticker for order in await world.orders()] == ["BBB"]


async def test_a_process_that_dies_before_saving_leaves_no_reservation_and_no_lock(
    world: World,
) -> None:
    account = Account("one")
    dying = world.sessions()
    await ExecutionEngine(dying, account).create_order_intent(
        ticker="AAA",
        side="buy",
        order_type="limit",
        quantity=Decimal("6"),
        limit_price=PRICE,
        estimated_price=PRICE,
        is_dry_run=False,
        available_cash=account.cash,
        cash_read_at=datetime.now(UTC),
    )
    # The connection goes away with its transaction open, as when the process is killed.
    await dying.invalidate()

    await asyncio.wait_for(world.admit(account, "BBB", 6), WAIT)

    assert [order.ticker for order in await world.orders()] == ["BBB"]


async def _open_intent(session: Any, account: Account, ticker: str, quantity: int) -> Order:
    """Create an intent and leave the transaction open, as a caller does before it sends."""
    return await ExecutionEngine(session, account).create_order_intent(
        ticker=ticker,
        side="buy",
        order_type="limit",
        quantity=Decimal(quantity),
        limit_price=PRICE,
        estimated_price=PRICE,
        is_dry_run=False,
        available_cash=account.cash,
        cash_read_at=datetime.now(UTC),
    )


async def test_a_refused_admission_gives_its_turn_back_without_ending_its_transaction(
    world: World,
) -> None:
    """The runner and the rebalancer carry on in the same transaction after a refusal."""
    account = Account("one")
    await world.admit(account, "AAA", 6)
    async with world.sessions() as refused_and_still_open:
        with pytest.raises(CashReservationRefused):
            await _open_intent(refused_and_still_open, account, "BBB", 6)

        await asyncio.wait_for(world.admit(account, "CCC", 4), 3)

        await refused_and_still_open.commit()
    assert sorted(order.ticker for order in await world.orders()) == ["AAA", "CCC"]
    assert [event.ticker for event in await world.refusals()] == ["BBB"]


async def test_an_admission_does_not_wait_for_ever_on_one_that_never_finishes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.execution.cash_reservation.ADMISSION_WAIT", timedelta(seconds=0.4))
    account = Account("one")
    async with world.sessions() as stuck:
        await _open_intent(stuck, account, "AAA", 1)

        async with world.sessions() as second:
            with pytest.raises(CashReservationRefused, match="did not finish in time"):
                await asyncio.wait_for(_open_intent(second, account, "BBB", 1), 5)
            await second.commit()

        await stuck.rollback()
    assert await world.orders() == []
    assert account.posts == []
    (event,) = await world.refusals()
    assert event.ticker == "BBB"
    assert event.payload["promised_elsewhere"] is None


# ── What becomes of the reservation ───────────────────────────────────────────


async def test_an_order_with_an_unknown_outcome_keeps_its_reservation(world: World) -> None:
    account = Account("one")
    account.answers["AAA"] = TimeoutError("no answer")
    await world.admit(account, "AAA", 6)
    assert (await world.order("AAA")).status == "submission_unknown"
    await world.make_older("AAA", LONG_AGO)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


async def test_a_process_killed_after_dispatch_leaves_the_reservation_for_the_next_process(
    world: World,
) -> None:
    account = Account("one")
    account.answers["AAA"] = SimulatedProcessDeath()
    with pytest.raises(SimulatedProcessDeath):
        await world.admit(account, "AAA", 6)
    saved = await world.order("AAA")
    assert saved.status == "submission_unknown", "the dispatch was not saved before the request"
    await world.make_older("AAA", LONG_AGO)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


@pytest.mark.parametrize(
    "refusal",
    [
        BrokerSubmissionRejected(error_type="T212APIError", http_status=400),
        BrokerSubmissionNotTransmitted(error_type="ConnectError"),
    ],
)
async def test_an_order_the_broker_never_took_gives_its_cash_back(
    world: World, refusal: Exception
) -> None:
    account = Account("one")
    account.answers["AAA"] = refusal
    read_before = datetime.now(UTC)
    await world.admit(account, "AAA", 6)
    assert (await world.order("AAA")).status in {"rejected", "error"}

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6, cash_read_at=read_before)
    await world.make_older("AAA", LONG_AGO)
    await world.admit(account, "BBB", 6)

    assert len(account.posts) == 2


async def test_a_filled_order_still_counts_against_a_cash_figure_read_before_the_fill(
    world: World,
) -> None:
    account = Account("one")
    account.answers["AAA"] = filled(6)
    read_before = datetime.now(UTC)
    await world.admit(account, "AAA", 6)
    assert (await world.order("AAA")).status == "filled"

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6, cash=1000, cash_read_at=read_before)


async def test_a_filled_order_stops_counting_once_a_later_cash_figure_shows_it(
    world: World,
) -> None:
    account = Account("one")
    account.answers["AAA"] = filled(6)
    await world.admit(account, "AAA", 6)
    await world.make_older("AAA", LONG_AGO)

    await world.admit(account, "BBB", 4, cash=400)
    with pytest.raises(CashReservationRefused):
        await world.admit(account, "CCC", 1, cash=400)


async def test_an_order_that_ended_within_the_clock_tolerance_still_counts(world: World) -> None:
    account = Account("one")
    account.answers["AAA"] = filled(6)
    await world.admit(account, "AAA", 6)
    await world.make_older("AAA", CLOCK_TOLERANCE - timedelta(seconds=20))

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


async def test_an_order_that_ended_before_the_tolerance_no_longer_counts(world: World) -> None:
    account = Account("one")
    account.answers["AAA"] = filled(6)
    await world.admit(account, "AAA", 6)
    await world.make_older("AAA", CLOCK_TOLERANCE + timedelta(seconds=20))

    await world.admit(account, "BBB", 6)


async def test_a_cash_figure_without_a_reading_time_is_treated_as_old(world: World) -> None:
    account = Account("one")
    account.answers["AAA"] = filled(6)
    await world.admit(account, "AAA", 6)
    await world.make_older("AAA", UNDATED_CASH_FIGURE_AGE - timedelta(minutes=2))

    async with world.sessions() as session:
        with pytest.raises(CashReservationRefused):
            await ExecutionEngine(session, account).create_order_intent(
                ticker="BBB",
                side="buy",
                order_type="limit",
                quantity=Decimal("6"),
                limit_price=PRICE,
                estimated_price=PRICE,
                is_dry_run=False,
                available_cash=account.cash,
            )
    await world.admit(account, "BBB", 6)


async def test_a_partly_filled_then_cancelled_order_counts_in_full_until_a_later_reading(
    world: World,
) -> None:
    account = Account("one")
    read_before = datetime.now(UTC)
    await world.admit(account, "AAA", 6)
    async with world.sessions() as session:
        order = (await session.execute(select(Order).where(Order.ticker == "AAA"))).scalar_one()
        order.status = "partially_filled"
        order.filled_quantity = Decimal("2")
        await session.commit()
    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)

    # The broker's record once the cancellation took effect: two of six had filled.
    account.by_id["B-AAA"] = {"status": "CANCELLED", "filledQuantity": "2", "filledPrice": "100"}
    async with world.sessions() as session:
        order = (await session.execute(select(Order).where(Order.ticker == "AAA"))).scalar_one()
        await ExecutionEngine(session, account).cancel_order(order)
        await session.commit()
    assert (await world.order("AAA")).status == "cancelled"
    assert (await world.order("AAA")).filled_quantity == Decimal("2")
    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6, cash_read_at=read_before)

    await world.make_older("AAA", LONG_AGO)
    await world.admit(account, "BBB", 6, cash=800)


async def test_an_order_whose_cancellation_failed_keeps_its_reservation(world: World) -> None:
    """The broker did not confirm the cancellation, so the order may still fill."""
    account = Account("one")
    account.cancel_failure = TimeoutError("no answer")
    await world.admit(account, "AAA", 6)
    async with world.sessions() as session:
        order = (await session.execute(select(Order).where(Order.ticker == "AAA"))).scalar_one()
        with pytest.raises(OrderCancellationFailed):
            await ExecutionEngine(session, account).cancel_order(order)
        await session.commit()
    saved = await world.order("AAA")
    assert saved.status == "accepted", "an order that may still be open was called finished"
    assert saved.cancelled_at is None
    assert saved.rejected_at is None
    assert account.cancels == ["B-AAA"]
    await world.make_older("AAA", LONG_AGO)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


async def _blocked(*_args: Any, **_kwargs: Any) -> None:
    raise SafetyPolicyViolation("Trading is halted.", decision_code="kill_switch_active")


async def test_a_buy_that_may_not_be_sent_is_closed_and_stops_reserving(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = Account("one")
    with monkeypatch.context() as halted:
        halted.setattr("app.execution.engine.require_order_submission_allowed", _blocked)
        with pytest.raises(SafetyPolicyViolation):
            async with world.sessions() as session:
                engine = ExecutionEngine(session, account)
                order = await _open_intent(session, account, "AAA", 6)
                try:
                    await engine.submit_order(order)
                finally:
                    # The scheduled callers keep their transaction; what they keep is checked.
                    await session.commit()

    saved = await world.order("AAA")
    assert saved.status == "rejected", "a buy that was never sent was left reserving cash"
    assert saved.rejected_at is not None
    assert saved.broker_order_id is None
    assert account.posts == []
    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6, cash_read_at=datetime.now(UTC) - timedelta(minutes=5))
    await world.make_older("AAA", LONG_AGO)
    await world.admit(account, "BBB", 6)


async def test_a_sell_that_may_not_be_sent_is_left_for_a_later_attempt(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = Account("one")
    monkeypatch.setattr("app.execution.engine.require_order_submission_allowed", _blocked)
    with pytest.raises(SafetyPolicyViolation):
        async with world.sessions() as session:
            engine = ExecutionEngine(session, account)
            order = await engine.create_order_intent(
                ticker="AAA",
                side="sell",
                order_type="market",
                quantity=Decimal("6"),
                estimated_price=PRICE,
                is_dry_run=False,
            )
            try:
                await engine.submit_order(order)
            finally:
                await session.commit()

    assert (await world.order("AAA")).status == "pending_intent"


async def test_asking_twice_for_the_same_operation_reserves_once(world: World) -> None:
    account = Account("one")
    first = await world.admit(account, "AAA", 4, stable_operation_identity="rebalance-1")
    again = await world.admit(account, "AAA", 4, stable_operation_identity="rebalance-1")

    assert again.id == first.id
    assert account.posts == [("AAA", Decimal("4"))]
    await world.admit(account, "BBB", 6)
    assert await total_admitted(world) == Decimal("1000")


async def test_a_repeat_of_an_already_admitted_operation_is_not_refused_for_lack_of_cash(
    world: World,
) -> None:
    account = Account("one")
    first = await world.admit(account, "AAA", 10, stable_operation_identity="rebalance-1")

    again = await world.admit(account, "AAA", 10, stable_operation_identity="rebalance-1")

    assert again.id == first.id
    assert await world.refusals() == []


# ── After a restart, and through reconciliation ───────────────────────────────


async def reconcile(world: World, account: Account, ticker: str) -> Order:
    async with world.sessions() as session:
        order = (await session.execute(select(Order).where(Order.ticker == ticker))).scalar_one()
        await ExecutionEngine(session, account).reconcile_order(order)
        await session.commit()
    return await world.order(ticker)


async def test_a_working_order_keeps_its_reservation_through_reconciliation(world: World) -> None:
    account = Account("one")
    await world.admit(account, "AAA", 6)
    account.by_id["B-AAA"] = {"status": "WORKING"}
    assert (await reconcile(world, account, "AAA")).status == "accepted"
    await world.make_older("AAA", LONG_AGO)

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6)


@pytest.mark.parametrize(
    ("reading", "status"),
    [
        (filled(6), "filled"),
        ({"status": "REJECTED", "filledQuantity": 0}, "rejected"),
        ({"status": "CANCELLED", "filledQuantity": 0}, "cancelled"),
    ],
)
async def test_reconciliation_ends_a_reservation_only_for_later_cash_figures(
    world: World, reading: dict[str, Any], status: str
) -> None:
    account = Account("one")
    read_before = datetime.now(UTC)
    await world.admit(account, "AAA", 6)
    account.by_id["B-AAA"] = reading
    assert (await reconcile(world, account, "AAA")).status == status

    with pytest.raises(CashReservationRefused):
        await world.admit(account, "BBB", 6, cash_read_at=read_before)
    await world.make_older("AAA", LONG_AGO)
    await world.admit(account, "BBB", 4, cash=400)
