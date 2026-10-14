"""Cash that other buy orders of the same account have already been promised.

A broker's free-cash figure is read before an order is created. Until the broker's own books
show them, orders admitted, sent or filled after that reading are invisible in it. This module
counts those orders, and lets one admission per environment and venue decide at a time, so
that two orders are never both admitted against the same cash.

Nothing is stored here. The reservation of an order is its own row: it holds its estimated
cost while its status is active, and for one clock tolerance after it ends, because a cash
figure read before the end cannot show the outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from app.db.models import Order
from app.execution.state_machine import ACTIVE_ORDER_STATUSES
from app.risk.engine import RiskViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

# How far two clocks that stamp orders and cash readings may disagree.
CLOCK_TOLERANCE = timedelta(seconds=60)
# A cash figure handed over without its reading time is treated as this old.
UNDATED_CASH_FIGURE_AGE = timedelta(minutes=15)
# How long an admission waits for another one to finish before it is refused.
ADMISSION_WAIT = timedelta(seconds=10)
_ASK_AGAIN_AFTER_SECONDS = 0.05
_ZERO = Decimal("0")


class CashReservationRefused(RiskViolation):
    """The order's cost, added to what the account has already promised, exceeds its cash."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, "cash_reservation_block")


def reading_time_or_assumed(cash_read_at: datetime | None, now: datetime | None = None) -> datetime:
    """When the cash figure was read; a figure whose time cannot be relied on is assumed old.

    That is a figure with no time, with a time that names no time zone, or with a time
    later than this clock allows for: counting from a time that is too late would leave
    out orders that ended before it.
    """
    present = now or datetime.now(UTC)
    if (
        cash_read_at is not None
        and cash_read_at.tzinfo is not None
        and cash_read_at <= present + CLOCK_TOLERANCE
    ):
        return cash_read_at
    return present - UNDATED_CASH_FIGURE_AGE


async def take_admission_turn(
    db: AsyncSession,
    *,
    environment: str | None,
    venue: str,
) -> bool:
    """Take the right to admit a buy order; False if it did not come free in time.

    The lock belongs to the transaction, or to the savepoint it was taken in: a commit, a
    rollback, a rollback of that savepoint or a lost connection lets it go. It is asked for
    repeatedly and never waited on, so a holder that hangs cannot hang every other admission.
    Databases without advisory locks (the single-writer test database) need none.
    """
    if db.get_bind().dialect.name != "postgresql":
        return True
    digest = hashlib.sha256(f"cash-admission:{environment}:{venue}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    give_up_at = time.monotonic() + ADMISSION_WAIT.total_seconds()
    while True:
        taken = (await db.execute(select(func.pg_try_advisory_xact_lock(key)))).scalar_one()
        if taken:
            return True
        if time.monotonic() >= give_up_at:
            return False
        await asyncio.sleep(_ASK_AGAIN_AFTER_SECONDS)


def _same_account(account_scope: str | None) -> ColumnElement[bool]:
    """Orders that may draw on the same cash.

    An order recorded without an account could belong to any account, so it counts against
    every one; a caller without an account identity counts every order.
    """
    if account_scope is None:
        return Order.id.is_not(None)
    return or_(Order.broker_account_scope == account_scope, Order.broker_account_scope.is_(None))


def _same_environment(environment: str | None) -> ColumnElement[bool]:
    """Orders of this environment, and older rows that never recorded one."""
    if environment is None:
        return Order.id.is_not(None)
    return or_(Order.execution_environment == environment, Order.execution_environment.is_(None))


def _not_shown_in_a_reading_from(cash_read_at: datetime) -> ColumnElement[bool]:
    """Still open, or ended too recently for a figure read at that time to show it."""
    earliest = cash_read_at - CLOCK_TOLERANCE
    return or_(
        Order.status.in_(ACTIVE_ORDER_STATUSES),
        Order.updated_at >= earliest,
        Order.filled_at >= earliest,
        Order.cancelled_at >= earliest,
        Order.rejected_at >= earliest,
    )


async def cash_promised_elsewhere(
    db: AsyncSession,
    *,
    environment: str | None,
    venue: str,
    account_scope: str | None,
    cash_read_at: datetime,
) -> Decimal:
    """The estimated cost of the account's other buy orders that the cash figure cannot show."""
    result = await db.execute(
        select(func.coalesce(func.sum(Order.cash_used), 0)).where(
            and_(
                Order.side == "buy",
                Order.is_dry_run.is_not(True),
                Order.venue == venue,
                Order.cash_used.is_not(None),
                _same_environment(environment),
                _same_account(account_scope),
                _not_shown_in_a_reading_from(cash_read_at),
            )
        )
    )
    return Decimal(str(result.scalar_one() or _ZERO))
