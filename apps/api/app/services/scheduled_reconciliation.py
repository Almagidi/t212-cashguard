"""The scheduled reconciler's work: whose orders it may read, in what order, and how often.

Three rules hold for every order it touches.

Scope. An order is read only at the account it belongs to: the same environment, the
Trading 212 venue, and the account scope recorded on the order when it was created. An
order with no recorded account is read only when exactly one connection is active, and
then only by its exact broker order id; an unknown submission with no recorded account is
matched against history only when that connection has no account identity either.
Everything else is counted and reported, never read.

Queue. Each account has its own queue of orders that are due, longest-waiting first
(never-read orders first, then by last look, creation time and id). The accounts take
turns, up to a fixed number of orders per run. An order that settled nothing waits before
it is asked about again, and orders that are waiting do not take places in a queue.

Ownership of the row. Each order is handled in its own transaction: the row is locked and
re-read, the broker is asked while the lock is held, and the result is committed before
the next order starts. A writer that already holds the row is left alone; a writer that
arrives meanwhile waits and then sees the committed result.

Reconciliation only reads from the broker. It never places, changes or cancels an order.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from sqlalchemy import and_, func, or_, select

from app.db.models import Order, OrderEvent, RiskEvent
from app.execution.broker_record import NO_BROKER_RECORD
from app.services.reconciliation_backoff import (
    MAX_BACKOFF_LEVEL,
    backoff_delay_seconds,
    level_after_unsettled_attempt,
    usable_retry_after_seconds,
)
from app.services.reconciliation_broker import AccountBroker, ReadOnlyBroker, failure_outcome
from app.services.reconciliation_queue import (
    awaiting_reconciliation,
    is_due,
    on_this_venue,
    owned_by,
)
from app.services.reconciliation_scope import unscoped_orders_are_readable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

log = structlog.get_logger()

# Orders handled in one run, shared between the accounts in turn. The run is also bounded
# by time, below.
BATCH_LIMIT = 50
# No new order is started after this long. The broker reads one order takes share one
# time limit (see ``reconciliation_broker``), so a run ends inside the task's 45 second
# soft time limit however many of its orders need the history as well.
RUN_BUDGET_SECONDS = 30.0
# After this many reads in a row that the broker did not answer, the account is left alone
# for the rest of the run.
MAX_FAILED_READS_IN_A_ROW = 3
UNSCOPED = "unscoped"
# Outcomes that are about one order, not about the broker being unreachable: its record is
# missing or refused, an unknown submission still has no single matching record, the
# broker refuses the read of it, or its settlement cannot be saved. An order that ends an
# attempt this way while its wait is already the longest will not settle by waiting, and
# is raised as a risk event.
ORDER_NOT_SETTLING_OUTCOMES = frozenset(
    {
        "broker_answer_refused",
        NO_BROKER_RECORD,
        "unresolved",
        "broker_rejected_read",
        "local_error",
    }
)
# An order that goes on not settling is raised again after this long, not on every attempt.
RAISE_AGAIN_AFTER = timedelta(hours=24)
ORDER_NOT_SETTLING_MESSAGE = (
    "The broker's record of this order cannot be used to settle it. "
    "The order stays open here and its cash stays reserved; check it at the broker."
)


class _Engine(Protocol):
    async def reconcile_order(self, order: Order) -> Any: ...

    async def reconcile_unknown_submission(self, order: Order) -> Any: ...


@dataclass
class _Account:
    """One broker account in this run: how it is reached, what it may be asked about, and
    its own queue of orders that are due."""

    broker: ReadOnlyBroker
    unscoped_with_broker_id: bool
    unscoped_unknown: bool
    queue: deque[Any] = field(default_factory=deque)

    def owns(self) -> ColumnElement[bool]:
        return owned_by(
            self.broker.account_scope,
            unscoped_with_broker_id=self.unscoped_with_broker_id,
            unscoped_unknown=self.unscoped_unknown,
        )


class ScheduledReconciler:
    """One bounded run over the orders that are due, for the accounts it was given."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        environment: str,
        engine_factory: Callable[[AsyncSession, Any], _Engine],
        fatal_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        self.db = db
        self.environment = environment
        self.engine_factory = engine_factory
        self.fatal_errors = fatal_errors
        self._started = time.monotonic()
        self._accounts: list[_Account] = []
        self._counts: dict[str, int] = {}

    def _elapsed(self) -> float:
        return time.monotonic() - self._started

    def _count(self, name: str, by: int = 1) -> None:
        if by:
            self._counts[name] = self._counts.get(name, 0) + by

    async def run(
        self, accounts: Sequence[AccountBroker], *, active_connection_count: int
    ) -> dict[str, Any]:
        self._started = time.monotonic()
        unidentified = self._bind(accounts, active_connection_count=active_connection_count)

        now = datetime.now(UTC)
        due_in_total = 0
        for account in self._accounts:
            due = self._due(account, now)
            # Each account has its own queue, so one account's trouble cannot hold the
            # places another account's orders need.
            account.queue = deque(
                (
                    await self.db.execute(
                        select(Order.id)
                        .where(due)
                        .order_by(
                            Order.last_reconciled_at.asc().nulls_first(),
                            Order.created_at.asc(),
                            Order.id.asc(),
                        )
                        .limit(BATCH_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            due_in_total += int(
                (
                    await self.db.execute(select(func.count()).select_from(Order).where(due))
                ).scalar_one()
            )
        out_of_scope = await self._out_of_scope()
        # Nothing read so far is needed again; do not hold a transaction open over the run.
        await self.db.rollback()

        attempted, out_of_time = await self._take_turns()
        # Selected but not attempted: the account was left alone, or time ran out. Nothing
        # about these orders is changed.
        deferred = sum(
            len(account.queue)
            for account in self._accounts
            if out_of_time or account.broker.stopped is not None
        )
        self._count("deferred", deferred)
        return self._summary(
            left_for_next_run=due_in_total - attempted - deferred,
            out_of_scope=out_of_scope,
            unidentified_connections=unidentified,
            out_of_time=out_of_time,
        )

    async def _take_turns(self) -> tuple[int, bool]:
        """Work through the accounts' queues one order each in turn. Returns how many
        orders were attempted and whether the time budget ended the run."""
        attempted = 0
        while attempted < BATCH_LIMIT:
            waiting = [a for a in self._accounts if a.queue and a.broker.stopped is None]
            if not waiting:
                return attempted, False
            for account in waiting:
                if attempted >= BATCH_LIMIT:
                    break
                if self._elapsed() >= RUN_BUDGET_SECONDS:
                    return attempted, True
                await self._reconcile_one(account.queue.popleft(), account)
                attempted += 1
        return attempted, False

    def _bind(self, accounts: Sequence[AccountBroker], *, active_connection_count: int) -> int:
        """Decide what each account may be asked about. Returns the connections left out
        because nothing identifies their account.

        An order recorded without an account can only be attributed when one connection
        exists. Even then, an unknown submission (matched loosely, against history) is
        left alone unless that connection is itself without an account identity: a
        connection that knows its account cannot show the order was placed there.
        """
        if len(accounts) > active_connection_count:
            raise ValueError("more broker accounts were supplied than connections are active")
        sole = unscoped_orders_are_readable(active_connection_count)
        self._accounts = []
        unidentified = 0
        for account in accounts:
            scope = account.account_scope
            if scope is None and not sole:
                unidentified += 1
                continue
            if any(bound.broker.account_scope == scope for bound in self._accounts):
                raise ValueError("two connections are bound to the same broker account")
            self._accounts.append(
                _Account(
                    broker=ReadOnlyBroker(account.broker, account_scope=scope),
                    unscoped_with_broker_id=sole,
                    unscoped_unknown=sole and scope is None,
                )
            )
        return unidentified

    async def _out_of_scope(self) -> dict[str, int]:
        """Count orders that would be reconciled if their account could be established."""
        awaiting = and_(on_this_venue(), awaiting_reconciliation())
        here = Order.execution_environment == self.environment
        scopes = [a.broker.account_scope for a in self._accounts if a.broker.account_scope]
        other_account = and_(here, Order.broker_account_scope.is_not(None))
        if scopes:
            other_account = and_(other_account, Order.broker_account_scope.not_in(scopes))
        # Spelled out rather than negated: a comparison with a missing scope is neither
        # true nor false in SQL, and negating it would drop the very rows counted here.
        unattributable: list[ColumnElement[bool]] = []
        if not any(a.unscoped_with_broker_id for a in self._accounts):
            unattributable.append(Order.broker_order_id.is_not(None))
        if not any(a.unscoped_unknown for a in self._accounts):
            unattributable.append(Order.broker_order_id.is_(None))
        questions: dict[str, ColumnElement[bool]] = {"other_account": other_account}
        if unattributable:
            questions["no_account_identity"] = and_(
                here, Order.broker_account_scope.is_(None), or_(*unattributable)
            )
        found: dict[str, int] = {}
        for name, clause in questions.items():
            total = int(
                (
                    await self.db.execute(
                        select(func.count()).select_from(Order).where(awaiting, clause)
                    )
                ).scalar_one()
            )
            if total:
                found[name] = total
        return found

    def _due(self, account: _Account, now: datetime) -> ColumnElement[bool]:
        """An order this account may be asked about now: this environment and venue, the
        account's own, still waiting for the broker, and past its wait."""
        return and_(
            Order.execution_environment == self.environment,
            on_this_venue(),
            awaiting_reconciliation(),
            account.owns(),
            is_due(now),
        )

    async def _reconcile_one(self, order_id: Any, account: _Account) -> None:
        broker = account.broker
        try:
            # Lock the row and read it again, replacing any copy this session still holds:
            # the order may have been settled, cancelled or picked up by someone else
            # since it was selected. A row another writer holds is skipped, not waited for.
            still_due = and_(Order.id == order_id, self._due(account, datetime.now(UTC)))
            order = (
                await self.db.execute(
                    select(Order)
                    .where(still_due)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if order is None:
                unlocked = await self.db.execute(select(Order.id).where(still_due))
                still_there = unlocked.scalar_one_or_none() is not None
                await self.db.rollback()
                self._count("skipped_locked" if still_there else "no_longer_due")
                return

            had_broker_order_id = bool(order.broker_order_id)
            status_before = order.status
            attempt_before = order.last_reconciled_at
            broker.begin_order()
            engine = self.engine_factory(self.db, broker)
            if had_broker_order_id:
                await engine.reconcile_order(order)
            else:
                await engine.reconcile_unknown_submission(order)
            if isinstance(broker.failure, self.fatal_errors):
                # The engine treats every failed read alike; a stop request is not one.
                raise broker.failure

            unsettled_reason = getattr(engine, "unsettled_reason", None)
            outcome = self._outcome(
                order,
                broker,
                had_broker_order_id=had_broker_order_id,
                status_before=status_before,
                attempt_before=attempt_before,
                unsettled_reason=unsettled_reason,
            )
            counted = await self._record(
                order,
                broker,
                outcome,
                had_broker_order_id=had_broker_order_id,
                unsettled_reason=unsettled_reason,
            )
            if outcome == "not_attempted":
                await self.db.rollback()
                return
            # Saved before the next order is read, and before the lock is let go.
            await self.db.commit()
            # Counted only once it is saved, so the run never reports work it lost.
            for name in counted:
                self._count(name)
        except self.fatal_errors:
            await self._discard_unsaved_work()
            raise
        except Exception as exc:
            await self._discard_unsaved_work()
            log.warning(
                "reconciliation.order_failed_locally",
                order_id=str(order_id),
                error_type=type(exc).__name__,
            )
            await self._record_local_failure(order_id, exc)
        except BaseException:
            await self._discard_unsaved_work()
            raise

    @staticmethod
    def _outcome(
        order: Order,
        broker: ReadOnlyBroker,
        *,
        had_broker_order_id: bool,
        status_before: str,
        attempt_before: datetime | None,
        unsettled_reason: str | None = None,
    ) -> str:
        if broker.failure is not None:
            return failure_outcome(broker.failure)
        attempt_recorded = order.last_reconciled_at != attempt_before
        if broker.reads == 0:
            # Nothing was asked. An unknown submission may be too recent to check, or
            # may have no dispatch time to search history with.
            return "unresolved" if attempt_recorded else "not_attempted"
        if order.status != status_before:
            return "settled"
        if had_broker_order_id:
            # The engine records the time of every answer it accepts. An answer it did
            # not record was one it refused to act on, or no record of the order at all.
            if attempt_recorded:
                return "unchanged"
            if unsettled_reason == NO_BROKER_RECORD:
                return NO_BROKER_RECORD
            return "broker_answer_refused"
        return "settled" if order.broker_order_id else "unresolved"

    async def _record(
        self,
        order: Order,
        broker: ReadOnlyBroker,
        outcome: str,
        *,
        had_broker_order_id: bool,
        unsettled_reason: str | None = None,
    ) -> tuple[str, ...]:
        """Apply the outcome to the order. Returns what to count once it has been saved."""
        if outcome == "not_attempted":
            return ()
        looked_at = () if had_broker_order_id else ("unknown_submissions",)
        if outcome in {"settled", "unchanged"}:
            broker.failed_reads_in_a_row = 0
            if order.retry_count:
                order.retry_count = 0
            if had_broker_order_id:
                return ("reconciled",)
            if order.status != "submission_unknown":
                return (*looked_at, "unknown_submissions_resolved")
            return looked_at

        retry_after = getattr(broker.failure, "retry_after", None)
        await self._wait_longer(
            order,
            outcome=outcome,
            error_type=type(broker.failure).__name__ if broker.failure is not None else None,
            retry_after=retry_after if outcome == "rate_limited" else None,
            reason=unsettled_reason,
        )
        if outcome == "unresolved":
            return looked_at
        if outcome == NO_BROKER_RECORD:
            # The broker answered every read; it is not unavailable, and nothing failed.
            broker.failed_reads_in_a_row = 0
            return (NO_BROKER_RECORD,)
        if outcome == "rate_limited":
            broker.stopped = "rate_limited"
            return (*looked_at, "rate_limited")
        if outcome == "broker_auth_failed":
            broker.stopped = "broker_auth_failed"
        elif outcome == "broker_read_failed":
            broker.failed_reads_in_a_row += 1
            if broker.failed_reads_in_a_row >= MAX_FAILED_READS_IN_A_ROW:
                broker.stopped = "broker_unavailable"
        else:
            # The broker did answer, about this one order; it is not unavailable.
            broker.failed_reads_in_a_row = 0
        return (*looked_at, "failed")

    async def _wait_longer(
        self,
        order: Order,
        *,
        outcome: str,
        error_type: str | None,
        retry_after: object,
        reason: str | None = None,
    ) -> None:
        """Raise the order's backoff level and keep a record of why it is waiting.

        The wait runs from now, after the attempt, not from when the attempt began.
        """
        now = datetime.now(UTC)
        level = level_after_unsettled_attempt(order.retry_count, retry_after)
        order.retry_count = level
        order.last_reconciled_at = now
        payload: dict[str, Any] = {
            "outcome": outcome,
            "error_type": error_type,
            "attempts_in_a_row": level,
            "wait_seconds": backoff_delay_seconds(level),
        }
        if reason is not None:
            payload["reason"] = reason
        if (
            level >= MAX_BACKOFF_LEVEL
            and outcome in ORDER_NOT_SETTLING_OUTCOMES
            and not await self._raised_lately(order, now)
        ):
            # Waiting has not helped and will not: say so where an operator looks. It does
            # not matter how the wait came to be the longest (an outage may have got it
            # there); what matters is that the order still does not settle now that the
            # broker answers. Reconciliation itself goes on asking.
            log.error(
                "reconciliation.order_not_settling",
                order_id=str(order.id),
                broker_order_id=order.broker_order_id,
                outcome=outcome,
                reason=reason,
            )
            self.db.add(
                RiskEvent(
                    id=uuid.uuid4(),
                    event_type="order_not_settling",
                    ticker=order.ticker,
                    signal_id=order.signal_id,
                    order_id=order.id,
                    message=ORDER_NOT_SETTLING_MESSAGE,
                    payload={
                        "severity": "critical",
                        "outcome": outcome,
                        "reason": reason,
                        "error_type": error_type,
                        "attempts_in_a_row": level,
                        "broker_order_id": order.broker_order_id,
                        "status": order.status,
                        "side": order.side,
                        "quantity": str(order.quantity),
                    },
                    occurred_at=now,
                )
            )
        asked_for = usable_retry_after_seconds(retry_after)
        if asked_for is not None:
            payload["retry_after_seconds"] = asked_for
        self.db.add(
            OrderEvent(
                order_id=order.id,
                event_type="reconciliation_backoff",
                from_status=order.status,
                to_status=order.status,
                payload=payload,
                occurred_at=now,
            )
        )

    async def _raised_lately(self, order: Order, now: datetime) -> bool:
        """True when this order was already raised as not settling within the last day."""
        raised = await self.db.execute(
            select(RiskEvent.id)
            .where(
                RiskEvent.order_id == order.id,
                RiskEvent.event_type == "order_not_settling",
                RiskEvent.occurred_at > now - RAISE_AGAIN_AFTER,
            )
            .limit(1)
        )
        return raised.scalar_one_or_none() is not None

    async def _record_local_failure(self, order_id: Any, failure: Exception) -> None:
        """After a rollback, note in a transaction of its own that this order failed here.

        If even that cannot be saved the database is not accepting writes, and the run stops.
        """
        try:
            order = (
                await self.db.execute(
                    select(Order)
                    .where(Order.id == order_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if order is None:
                await self.db.rollback()
                self._count("skipped_locked")
                return
            await self._wait_longer(
                order,
                outcome="local_error",
                error_type=type(failure).__name__,
                retry_after=None,
            )
            await self.db.commit()
            self._count("failed")
        except BaseException:
            await self._discard_unsaved_work()
            raise

    async def _discard_unsaved_work(self) -> None:
        """Roll back, finishing whatever interrupts this run meanwhile.

        The rollback is what releases the row lock, so it is completed first. A
        cancellation or a stop request that arrived during it is then passed on. A
        rollback that fails is raised: the session can no longer be trusted and the run
        must stop.
        """
        rollback = asyncio.ensure_future(self.db.rollback())
        interruption: BaseException | None = None
        while not rollback.done():
            try:
                await asyncio.shield(rollback)
            except asyncio.CancelledError:
                interruption = asyncio.CancelledError()
            except Exception as exc:
                if not rollback.done() and interruption is None:
                    # Not the rollback's own failure: something interrupted the wait.
                    # A cancellation that came first is not replaced by it.
                    interruption = exc
        if isinstance(interruption, asyncio.CancelledError):
            raise interruption
        rollback.result()
        if interruption is not None:
            raise interruption

    def _summary(
        self,
        *,
        left_for_next_run: int,
        out_of_scope: dict[str, int],
        unidentified_connections: int,
        out_of_time: bool,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"reconciled": self._counts.get("reconciled", 0)}
        if "unknown_submissions" in self._counts:
            summary["unknown_submissions"] = self._counts["unknown_submissions"]
            summary["unknown_submissions_resolved"] = self._counts.get(
                "unknown_submissions_resolved", 0
            )
        for name in (
            "failed",
            NO_BROKER_RECORD,
            "rate_limited",
            "skipped_locked",
            "no_longer_due",
            "deferred",
        ):
            if self._counts.get(name):
                summary[name] = self._counts[name]
        if left_for_next_run > 0:
            summary["left_for_next_run"] = left_for_next_run
        stopped = {
            account.broker.account_scope or UNSCOPED: account.broker.stopped
            for account in self._accounts
            if account.broker.stopped is not None
        }
        if stopped:
            summary["stopped"] = stopped
        if out_of_time:
            summary["out_of_time"] = True
        if out_of_scope:
            summary["out_of_scope"] = out_of_scope
        if unidentified_connections:
            summary["connections_without_account_identity"] = unidentified_connections
        return summary
