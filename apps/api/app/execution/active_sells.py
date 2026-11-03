"""Which sells count as "in flight" for a protective exit, and when one has been stuck too long.

A protective exit is judged against the sells already on their way for the SAME position:
the same ticker, execution environment and broker account. A leftover sell from another
environment (demo vs live), from another account, or one that has been stuck for longer than
``PROTECTIVE_SELL_STALE_AFTER_SECONDS`` must not leave a held long without its stop.

A stale sell (still ``submission_unknown`` / ``submitted`` / ``accepted`` after the threshold)
stops blocking but is never forgotten: the position monitor alerts and triggers a
reconciliation for it. Overselling stays impossible because the held quantity is read fresh
from the broker and every sell that is genuinely active still counts against it.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, not_, or_

from app.core.config import settings
from app.db.models import Order
from app.execution.state_machine import ACTIVE_ORDER_STATUSES

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

# Statuses whose age says the order is stuck rather than simply working.
STALE_CANDIDATE_STATUSES = frozenset({"submission_unknown", "submitted", "accepted"})
_DISPATCHED_STATUSES = ACTIVE_ORDER_STATUSES - {"pending_intent"}


def broker_account_scope(broker: Any) -> str | None:
    value = inspect.getattr_static(broker, "account_scope", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def execution_environment_of(*, is_dry_run: bool) -> str:
    return "dry_run" if is_dry_run else settings.APP_MODE


def stale_cutoff(now: datetime | None = None) -> datetime | None:
    """Dispatches older than this are stale; None when escalation is disabled (<= 0)."""
    seconds = settings.PROTECTIVE_SELL_STALE_AFTER_SECONDS
    if seconds <= 0:
        return None
    return (now or datetime.now(UTC)) - timedelta(seconds=seconds)


def _scope(
    ticker: str, *, is_dry_run: bool, environment: str | None, account_scope: str | None
) -> list[ColumnElement[bool]]:
    env = environment or execution_environment_of(is_dry_run=is_dry_run)
    conditions: list[ColumnElement[bool]] = [
        Order.ticker == ticker,
        Order.side == "sell",
        Order.is_dry_run.is_(is_dry_run),
        # An order recorded without an environment/account (legacy) still counts: the
        # unknown is treated as the same position, never silently ignored.
        or_(Order.execution_environment == env, Order.execution_environment.is_(None)),
    ]
    if account_scope is not None:
        conditions.append(
            or_(Order.broker_account_scope == account_scope, Order.broker_account_scope.is_(None))
        )
    return conditions


def _dispatch_clock() -> Any:
    return func.coalesce(Order.submitted_at, Order.created_at)


def _is_stale(cutoff: datetime) -> ColumnElement[bool]:
    return and_(
        Order.status.in_(STALE_CANDIDATE_STATUSES),
        _dispatch_clock() < cutoff,
    )


def blocking_sell_conditions(
    ticker: str,
    *,
    is_dry_run: bool,
    environment: str | None = None,
    account_scope: str | None = None,
) -> list[ColumnElement[bool]]:
    """Sells for this position that are dispatched, unfinished and not stuck."""
    conditions = _scope(
        ticker, is_dry_run=is_dry_run, environment=environment, account_scope=account_scope
    )
    conditions.append(Order.status.in_(_DISPATCHED_STATUSES))
    cutoff = stale_cutoff()
    if cutoff is not None:
        conditions.append(not_(_is_stale(cutoff)))
    return conditions


def stale_sell_conditions(
    ticker: str,
    *,
    is_dry_run: bool,
    environment: str | None = None,
    account_scope: str | None = None,
) -> list[ColumnElement[bool]] | None:
    """Sells for this position stuck beyond the threshold; None when disabled."""
    cutoff = stale_cutoff()
    if cutoff is None:
        return None
    conditions = _scope(
        ticker, is_dry_run=is_dry_run, environment=environment, account_scope=account_scope
    )
    conditions.append(_is_stale(cutoff))
    return conditions
