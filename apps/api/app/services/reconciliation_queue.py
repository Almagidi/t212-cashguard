"""Which orders the scheduled reconciler may take, and in what order.

Everything here is a database question; nothing in this module talks to a broker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, false, func, or_, select

from app.db.models import BrokerConnection, Order
from app.execution.state_machine import ACTIVE_ORDER_STATUSES
from app.services.reconciliation_backoff import (
    BACKOFF_STEPS_SECONDS,
    MAX_BACKOFF_LEVEL,
    backoff_delay_seconds,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

log = structlog.get_logger()

RECONCILED_VENUE = "t212"
RECONCILED_BROKER = "trading212"
# How far ahead of this worker's clock a recorded attempt may be before it is disregarded.
CLOCK_TOLERANCE = timedelta(seconds=60)
ORPHAN_AGE = timedelta(minutes=5)
ORPHAN_REPORT_LIMIT = 50


async def active_connections(db: AsyncSession, environment: str) -> list[BrokerConnection]:
    """Every active Trading 212 connection for the environment, in a fixed order."""
    result = await db.execute(
        select(BrokerConnection)
        .where(
            BrokerConnection.is_active.is_(True),
            BrokerConnection.broker == RECONCILED_BROKER,
            BrokerConnection.environment == environment,
        )
        .order_by(BrokerConnection.created_at.asc(), BrokerConnection.id.asc())
    )
    return list(result.scalars().all())


def awaiting_reconciliation() -> ColumnElement[bool]:
    """An order the broker can still say something about.

    Either it is active and has a broker order id, or its submission outcome is unknown
    and it has none (those are looked up in broker history; they are never sent again).
    """
    return or_(
        and_(Order.status.in_(ACTIVE_ORDER_STATUSES), Order.broker_order_id.is_not(None)),
        and_(Order.status == "submission_unknown", Order.broker_order_id.is_(None)),
    )


def on_this_venue() -> ColumnElement[bool]:
    return and_(Order.venue == RECONCILED_VENUE, Order.is_dry_run.is_(False))


def owned_by(
    account_scope: str | None, *, unscoped_with_broker_id: bool, unscoped_unknown: bool
) -> ColumnElement[bool]:
    """The orders one account may be asked about.

    Its own scope always. Orders recorded without an account only when the caller has
    established that they can be attributed to it: those with a broker order id are looked
    up by that exact id; those without are matched against the account's history, which
    is a looser test and is allowed separately.
    """
    clauses: list[ColumnElement[bool]] = []
    if account_scope is not None:
        clauses.append(Order.broker_account_scope == account_scope)
    if unscoped_with_broker_id:
        clauses.append(
            and_(Order.broker_account_scope.is_(None), Order.broker_order_id.is_not(None))
        )
    if unscoped_unknown:
        clauses.append(and_(Order.broker_account_scope.is_(None), Order.broker_order_id.is_(None)))
    if not clauses:
        return false()
    return or_(*clauses)


def is_due(now: datetime) -> ColumnElement[bool]:
    """True once the wait that goes with the order's backoff level has passed."""
    level = func.coalesce(Order.retry_count, 0)
    waited: list[ColumnElement[bool]] = [
        and_(
            level == step,
            Order.last_reconciled_at <= now - timedelta(seconds=backoff_delay_seconds(step)),
        )
        for step in range(1, MAX_BACKOFF_LEVEL)
    ]
    waited.append(
        and_(
            level >= MAX_BACKOFF_LEVEL,
            Order.last_reconciled_at <= now - timedelta(seconds=BACKOFF_STEPS_SECONDS[-1]),
        )
    )
    # A last attempt dated in the future cannot be trusted to mean anything; without this
    # the order would wait until the clock caught up, however long that was.
    from_the_future = Order.last_reconciled_at > now + CLOCK_TOLERANCE
    return or_(level <= 0, Order.last_reconciled_at.is_(None), from_the_future, *waited)


async def count_awaiting_reconciliation(db: AsyncSession, environment: str) -> int:
    """How many orders of this environment the broker could still be asked about."""
    result = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(
            Order.execution_environment == environment,
            on_this_venue(),
            awaiting_reconciliation(),
        )
    )
    return int(result.scalar_one())


async def count_awaiting_without_environment(db: AsyncSession) -> int:
    """Orders the broker could be asked about, if only their environment had been recorded.

    They are never read: nothing says whether they belong to a demo or a live account.
    """
    result = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(
            Order.execution_environment.is_(None),
            on_this_venue(),
            awaiting_reconciliation(),
        )
    )
    return int(result.scalar_one())


async def report_orphaned_submissions(db: AsyncSession, now: datetime) -> int:
    """Log legacy orders stuck in 'submitted' with no broker order id, and return how many.

    They were written before submissions were committed as 'submission_unknown'. Nothing
    can be looked up without a broker order id, and they are never sent again, so they
    are kept visible for manual review.
    """
    stuck = and_(
        Order.status == "submitted",
        Order.broker_order_id.is_(None),
        Order.is_dry_run.is_(False),
        Order.created_at < now - ORPHAN_AGE,
    )
    total = int(
        (await db.execute(select(func.count()).select_from(Order).where(stuck))).scalar_one()
    )
    if not total:
        return 0
    result = await db.execute(
        select(Order)
        .where(stuck)
        .order_by(Order.created_at.asc(), Order.id.asc())
        .limit(ORPHAN_REPORT_LIMIT)
    )
    for orphan in result.scalars().all():
        log.error(
            "execution.orphaned_submitted_order",
            order_id=str(orphan.id),
            ticker=orphan.ticker,
            side=orphan.side,
            created_at=orphan.created_at.isoformat(),
        )
    return total
