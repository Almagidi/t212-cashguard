"""Which orders count against an account's cash figure, row by row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from app.db.models import Order
from app.execution.cash_reservation import (
    CLOCK_TOLERANCE,
    UNDATED_CASH_FIGURE_AGE,
    CashReservationRefused,
    cash_promised_elsewhere,
    reading_time_or_assumed,
    take_admission_turn,
)
from app.execution.state_machine import ACTIVE_ORDER_STATUSES, TERMINAL_ORDER_STATUSES
from app.risk.engine import RiskViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

READ_AT = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
WELL_BEFORE = READ_AT - timedelta(hours=1)
SCOPE = "trading212:demo:account:one"


def order(**overrides: Any) -> Order:
    identifier = uuid.uuid4()
    values: dict[str, Any] = {
        "id": identifier,
        "client_order_key": f"key-{identifier.hex}",
        "ticker": "AAA",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("6"),
        "limit_price": Decimal("100"),
        "status": "accepted",
        "venue": "t212",
        "is_dry_run": False,
        "execution_environment": "demo",
        "broker_account_scope": SCOPE,
        "cash_used": Decimal("600"),
        "created_at": WELL_BEFORE,
        "updated_at": WELL_BEFORE,
    }
    values.update(overrides)
    return Order(**values)


async def promised(db: AsyncSession, *rows: Order, scope: str | None = SCOPE) -> Decimal:
    db.add_all(rows)
    await db.flush()
    return await cash_promised_elsewhere(
        db, environment="demo", venue="t212", account_scope=scope, cash_read_at=READ_AT
    )


@pytest.mark.asyncio
async def test_nothing_is_promised_when_there_are_no_orders(db: AsyncSession) -> None:
    assert await promised(db) == Decimal("0")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(ACTIVE_ORDER_STATUSES))
async def test_an_open_order_counts_however_old_it_is(db: AsyncSession, status: str) -> None:
    assert await promised(db, order(status=status)) == Decimal("600")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(TERMINAL_ORDER_STATUSES))
async def test_an_order_that_ended_long_before_the_reading_does_not_count(
    db: AsyncSession, status: str
) -> None:
    ended = order(
        status=status, filled_at=WELL_BEFORE, cancelled_at=WELL_BEFORE, rejected_at=WELL_BEFORE
    )

    assert await promised(db, ended) == Decimal("0")


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["updated_at", "filled_at", "cancelled_at", "rejected_at"])
@pytest.mark.parametrize(
    ("offset", "counts"),
    [
        (timedelta(seconds=1), True),
        (timedelta(0), True),
        (-CLOCK_TOLERANCE, True),
        (-CLOCK_TOLERANCE - timedelta(seconds=1), False),
    ],
)
async def test_an_ended_order_counts_when_any_of_its_times_is_too_close_to_the_reading(
    db: AsyncSession, column: str, offset: timedelta, counts: bool
) -> None:
    ended = order(status="filled", **{column: READ_AT + offset})

    assert await promised(db, ended) == (Decimal("600") if counts else Decimal("0"))


@pytest.mark.asyncio
async def test_costs_of_several_orders_are_added(db: AsyncSession) -> None:
    rows = [
        order(cash_used=Decimal("100.5")),
        order(cash_used=Decimal("200.25")),
        order(status="submission_unknown", cash_used=Decimal("0.00000001")),
    ]

    assert await promised(db, *rows) == Decimal("300.75000001")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "not_counted",
    [
        {"side": "sell"},
        {"is_dry_run": True},
        {"venue": "kraken"},
        {"execution_environment": "live"},
        {"broker_account_scope": "trading212:demo:account:two"},
        {"cash_used": None},
    ],
)
async def test_orders_that_draw_on_other_cash_are_left_out(
    db: AsyncSession, not_counted: dict[str, Any]
) -> None:
    assert await promised(db, order(), order(**not_counted)) == Decimal("600")


@pytest.mark.asyncio
async def test_an_order_without_an_environment_counts_in_every_environment(
    db: AsyncSession,
) -> None:
    assert await promised(db, order(execution_environment=None)) == Decimal("600")


@pytest.mark.asyncio
async def test_an_order_without_an_account_counts_against_a_named_account(
    db: AsyncSession,
) -> None:
    assert await promised(db, order(broker_account_scope=None)) == Decimal("600")


@pytest.mark.asyncio
async def test_a_caller_without_an_account_counts_the_orders_of_every_account(
    db: AsyncSession,
) -> None:
    rows = [
        order(),
        order(broker_account_scope="trading212:demo:account:two"),
        order(broker_account_scope=None),
    ]

    assert await promised(db, *rows, scope=None) == Decimal("1800")


def test_a_dated_cash_figure_keeps_its_reading_time() -> None:
    assert reading_time_or_assumed(READ_AT, now=READ_AT + timedelta(hours=3)) == READ_AT


def test_a_time_without_a_time_zone_is_not_relied_on() -> None:
    naive = READ_AT.replace(tzinfo=None)

    assert reading_time_or_assumed(naive, now=READ_AT) == READ_AT - UNDATED_CASH_FIGURE_AGE


@pytest.mark.parametrize(
    ("ahead", "relied_on"),
    [
        (timedelta(0), True),
        (CLOCK_TOLERANCE, True),
        (CLOCK_TOLERANCE + timedelta(seconds=1), False),
        (timedelta(days=1), False),
    ],
)
def test_a_time_later_than_this_clock_allows_is_not_relied_on(
    ahead: timedelta, relied_on: bool
) -> None:
    claimed = READ_AT + ahead

    assumed = reading_time_or_assumed(claimed, now=READ_AT)

    assert assumed == (claimed if relied_on else READ_AT - UNDATED_CASH_FIGURE_AGE)


def test_an_undated_cash_figure_is_assumed_to_be_old() -> None:
    assert reading_time_or_assumed(None, now=READ_AT) == READ_AT - UNDATED_CASH_FIGURE_AGE
    assert timedelta(minutes=10) <= UNDATED_CASH_FIGURE_AGE


def test_an_undated_figure_is_measured_from_the_present_by_default() -> None:
    before = datetime.now(UTC)
    assumed = reading_time_or_assumed(None)
    after = datetime.now(UTC)

    assert before - UNDATED_CASH_FIGURE_AGE <= assumed <= after - UNDATED_CASH_FIGURE_AGE


@pytest.mark.asyncio
async def test_a_database_without_advisory_locks_needs_no_turn(db: AsyncSession) -> None:
    await take_admission_turn(db, environment="demo", venue="t212")


def test_a_refusal_is_a_risk_violation_with_its_own_event_type() -> None:
    refusal = CashReservationRefused("no cash left")

    assert isinstance(refusal, RiskViolation)
    assert refusal.event_type == "cash_reservation_block"
    assert refusal.reason == "no cash left"
