"""What the scheduled reconciler does when the database, the broker or its own run fails."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, update

from app.core import redis as redis_module
from app.db.models import AppSettings, Order, OrderEvent
from app.execution.engine import ExecutionEngine
from app.services import reconciliation_broker, scheduled_reconciliation
from app.services.reconciliation_broker import AccountBroker
from app.services.scheduled_reconciliation import ScheduledReconciler
from app.workers import tasks
from tests.integration.scheduled_reconciliation_cases import (
    T0,
    Brokers,
    add_connection,
    add_order,
    filled,
    install_borrowed_session,
    run_task,
    scope_of,
    stored,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import BrokerConnection


class DatabaseDown(Exception):
    """A database error that is not specific to one row."""


@pytest.fixture
def brokers(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> Brokers:
    registry = Brokers()
    install_borrowed_session(monkeypatch, registry, db)
    return registry


async def _two_orders(db: AsyncSession) -> tuple[BrokerConnection, Order, Order]:
    connection = await add_connection(db, account_id="ACC-1")
    scope = scope_of(connection)
    first = await add_order(db, account_scope=scope, broker_order_id="first", created_at=T0)
    second = await add_order(
        db, account_scope=scope, broker_order_id="second", created_at=T0 + timedelta(seconds=1)
    )
    return connection, first, second


def _fail_commits(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession, failing: set[int]
) -> list[str]:
    """Make the numbered commits (1 is the next one) fail. Returns the log of what happened."""
    real_commit = db.commit
    log: list[str] = []

    async def commit() -> None:
        number = len(log) + 1
        if number in failing:
            log.append("failed")
            raise DatabaseDown(f"commit {number} failed")
        await real_commit()
        log.append("saved")

    monkeypatch.setattr(db, "commit", commit)
    return log


async def _events(db: AsyncSession, order: Order) -> list[dict[str, Any]]:
    result = await db.execute(
        select(OrderEvent).where(
            OrderEvent.order_id == order.id, OrderEvent.event_type == "reconciliation_backoff"
        )
    )
    return [dict(event.payload or {}) for event in result.scalars().all()]


async def test_a_failed_save_leaves_nothing_of_that_order_behind_and_the_next_still_runs(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first, second = await _two_orders(db)
    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = filled()
    broker.responses["second"] = filled()
    _fail_commits(monkeypatch, db, failing={1})

    summary = await run_task()

    unsaved = await stored(db, first.id)
    assert unsaved.status == "accepted", "a fill whose save failed must not be half-recorded"
    assert unsaved.filled_quantity is None
    assert unsaved.retry_count == 1
    assert await _events(db, first) == [
        {
            "outcome": "local_error",
            "error_type": "DatabaseDown",
            "attempts_in_a_row": 1,
            "wait_seconds": 30,
        }
    ]
    assert (await stored(db, second.id)).status == "filled"
    assert summary["failed"] == 1
    assert summary["reconciled"] == 1


async def test_the_run_stops_when_not_even_the_failure_can_be_saved(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first, second = await _two_orders(db)
    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = filled()
    _fail_commits(monkeypatch, db, failing={1, 2})

    with pytest.raises(DatabaseDown, match="commit 2"):
        await run_task()

    assert broker.order_reads == ["first"], "the run carried on reading without a database"
    untouched = await stored(db, first.id)
    assert untouched.status == "accepted"
    assert untouched.retry_count == 0
    assert (await stored(db, second.id)).last_reconciled_at is None


async def test_the_run_stops_when_the_rollback_itself_fails(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first, _second = await _two_orders(db)
    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = filled()
    commits = _fail_commits(monkeypatch, db, failing={1})
    real_rollback = db.rollback
    failed_once: list[bool] = []

    async def rollback() -> None:
        if commits == ["failed"] and not failed_once:
            failed_once.append(True)
            raise DatabaseDown("rollback failed")
        await real_rollback()

    monkeypatch.setattr(db, "rollback", rollback)

    with pytest.raises(DatabaseDown, match="rollback failed"):
        await run_task()

    assert broker.order_reads == ["first"]
    assert (await stored(db, first.id)).status == "accepted"


async def test_a_second_cancellation_cannot_cut_the_rollback_short(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="x")
    entered = asyncio.Event()

    async def never_answers(_broker_order_id: str) -> Any:
        entered.set()
        await asyncio.Event().wait()

    brokers.for_user(connection.user_id).responses["x"] = never_answers
    real_rollback = db.rollback
    rollbacks: list[str] = []

    async def slow_rollback() -> None:
        rollbacks.append("started")
        await asyncio.sleep(0.2)
        await real_rollback()
        rollbacks.append("finished")

    run = asyncio.ensure_future(run_task())
    async with asyncio.timeout(5):
        await entered.wait()
    monkeypatch.setattr(db, "rollback", slow_rollback)
    run.cancel()
    await asyncio.sleep(0.05)
    assert rollbacks == ["started"]
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert rollbacks[:2] == ["started", "finished"]
    after = await stored(db, order.id)
    assert after.status == "accepted"
    assert after.retry_count == 0
    assert after.last_reconciled_at is None


@pytest.mark.parametrize(
    ("interruptions", "passed_on"),
    [
        ([SoftTimeLimitExceeded], SoftTimeLimitExceeded),
        ([asyncio.CancelledError, SoftTimeLimitExceeded], asyncio.CancelledError),
        ([SoftTimeLimitExceeded, asyncio.CancelledError], asyncio.CancelledError),
    ],
)
async def test_whatever_interrupts_the_rollback_is_passed_on_only_after_it_finishes(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    interruptions: list[type[BaseException]],
    passed_on: type[BaseException],
) -> None:
    reconciler = ScheduledReconciler(db, environment="demo", engine_factory=ExecutionEngine)
    real_rollback = db.rollback
    real_shield = asyncio.shield
    rollbacks: list[str] = []
    pending = list(interruptions)

    async def slow_rollback() -> None:
        rollbacks.append("started")
        await asyncio.sleep(0.05)
        await real_rollback()
        rollbacks.append("finished")

    def shield_interrupted(awaitable: Any) -> Any:
        if not pending:
            return real_shield(awaitable)
        interruption = pending.pop(0)

        async def interrupt() -> None:
            await asyncio.sleep(0)
            raise interruption()

        return interrupt()

    monkeypatch.setattr(db, "rollback", slow_rollback)
    monkeypatch.setattr(scheduled_reconciliation.asyncio, "shield", shield_interrupted)

    with pytest.raises(passed_on):
        await reconciler._discard_unsaved_work()

    assert rollbacks == ["started", "finished"]
    assert pending == []


async def test_a_copy_of_an_order_loaded_earlier_in_the_run_is_replaced_under_the_lock(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first, second = await _two_orders(db)
    broker = brokers.for_user(connection.user_id)
    broker.responses["second"] = filled()
    kept: list[Any] = []

    class LoadsAnotherOrder(ExecutionEngine):
        async def reconcile_order(self, order: Any) -> Any:
            if order.id == first.id:
                # The engine looks at other orders while it works; this copy stays in the
                # session. Meanwhile another writer changes that row.
                kept.append(await self.db.get(Order, second.id))
                await self.db.execute(
                    update(Order)
                    .where(Order.id == second.id)
                    .values(version=Order.version + 1, error_message="changed elsewhere")
                    .execution_options(synchronize_session=False)
                )
            return await super().reconcile_order(order)

    monkeypatch.setattr("app.execution.engine.ExecutionEngine", LoadsAnotherOrder)

    summary = await run_task()

    assert kept, "the scenario did not run"
    assert "failed" not in summary, "the second order was written from the older copy"
    after = await stored(db, second.id)
    assert after.status == "filled"
    assert after.retry_count == 0


@pytest.mark.parametrize(
    ("scopes", "connections", "message"),
    [
        (["a", "b"], 1, "more broker accounts"),
        (["a", "a"], 2, "same broker account"),
    ],
)
async def test_the_reconciler_refuses_an_inconsistent_set_of_accounts(
    db: AsyncSession, scopes: list[str], connections: int, message: str
) -> None:
    reconciler = ScheduledReconciler(db, environment="demo", engine_factory=ExecutionEngine)
    accounts = [AccountBroker(broker=object(), account_scope=scope) for scope in scopes]

    with pytest.raises(ValueError, match=message):
        await reconciler.run(accounts, active_connection_count=connections)


async def test_a_run_out_of_time_starts_nothing_and_says_so(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _connection, first, second = await _two_orders(db)
    monkeypatch.setattr(scheduled_reconciliation, "RUN_BUDGET_SECONDS", 0.0)

    summary = await run_task()

    assert brokers.all_order_reads == []
    assert summary == {"reconciled": 0, "deferred": 2, "out_of_time": True}
    for order in (first, second):
        after = await stored(db, order.id)
        assert after.retry_count == 0
        assert after.last_reconciled_at is None


async def test_a_run_out_of_time_defers_every_accounts_selected_orders(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await add_connection(db, account_id="ACC-1", created_at=T0)
    second = await add_connection(db, account_id="ACC-2", created_at=T0 + timedelta(minutes=1))
    for index in range(3):
        await add_order(db, account_scope=scope_of(first), broker_order_id=f"a-{index}")
    for index in range(2):
        await add_order(db, account_scope=scope_of(second), broker_order_id=f"b-{index}")
    monkeypatch.setattr(scheduled_reconciliation, "RUN_BUDGET_SECONDS", 0.0)

    summary = await run_task()

    assert brokers.all_order_reads == []
    assert summary == {"reconciled": 0, "deferred": 5, "out_of_time": True}


async def test_a_broker_that_does_not_answer_in_time_is_a_failed_read_and_the_run_goes_on(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first, second = await _two_orders(db)
    monkeypatch.setattr(reconciliation_broker, "BROKER_READ_TIMEOUT_SECONDS", 0.05)

    async def never_answers(_broker_order_id: str) -> Any:
        await asyncio.Event().wait()

    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = never_answers

    async with asyncio.timeout(5):
        summary = await run_task()

    assert broker.order_reads == ["first", "second"]
    assert (await _events(db, first))[0]["error_type"] == "TimeoutError"
    assert (await stored(db, first.id)).retry_count == 1
    assert (await stored(db, second.id)).last_reconciled_at is not None
    assert summary["failed"] == 1
    assert summary["reconciled"] == 1


async def test_an_answer_the_engine_refuses_changes_nothing_and_makes_the_order_wait(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    order = await add_order(db, account_scope=scope_of(connection), broker_order_id="odd")
    broker = brokers.for_user(connection.user_id)
    broker.responses["odd"] = filled(quantity="7")

    summary = await run_task()

    after = await stored(db, order.id)
    assert after.status == "accepted"
    assert after.filled_quantity is None
    assert after.retry_count == 1
    assert (await _events(db, order))[0]["outcome"] == "broker_answer_refused"
    assert summary["failed"] == 1
    assert "stopped" not in summary, "the broker did answer; the account is not at fault"


async def test_a_stop_request_during_a_read_stops_the_run_instead_of_counting_as_a_failure(
    db: AsyncSession, brokers: Brokers
) -> None:
    connection, first, second = await _two_orders(db)
    broker = brokers.for_user(connection.user_id)
    broker.responses["first"] = SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        await run_task()

    assert broker.order_reads == ["first"]
    for order in (first, second):
        after = await stored(db, order.id)
        assert after.retry_count == 0
        assert after.last_reconciled_at is None


async def test_the_run_is_reported_in_the_heartbeat_after_the_orders_are_saved(
    db: AsyncSession, brokers: Brokers
) -> None:
    db.add(AppSettings(id=1))
    await db.commit()
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="x")
    await add_order(db, account_scope="trading212:demo:account:someone-else")

    summary = await run_task()

    row = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    await db.refresh(row)
    recorded = row.extra["worker_heartbeats"]["reconcile_pending_orders"]["payload"]
    assert recorded == summary
    assert recorded["out_of_scope"] == {"other_account": 1}


class _LockRecorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        self.held = False

    @asynccontextmanager
    async def __call__(self, name: str, *, ttl_seconds: int) -> Any:
        self.requests.append((name, ttl_seconds))
        self.held = True
        try:
            yield True
        finally:
            self.held = False


async def test_the_owner_lock_is_held_for_every_read_and_save_and_released_afterwards(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await add_connection(db, account_id="ACC-1")
    await add_order(db, account_scope=scope_of(connection), broker_order_id="x")
    lock = _LockRecorder()
    monkeypatch.setattr("app.core.redis.exclusive_task_lock", lock)
    held_at: list[tuple[str, bool]] = []

    async def answer(_broker_order_id: str) -> Any:
        held_at.append(("read", lock.held))
        return filled()

    brokers.for_user(connection.user_id).responses["x"] = answer
    real_commit = db.commit

    async def commit() -> None:
        held_at.append(("save", lock.held))
        await real_commit()

    monkeypatch.setattr(db, "commit", commit)

    await run_task()

    assert lock.requests == [("reconcile_pending_orders", 90)]
    assert held_at == [("read", True), ("save", True), ("save", True)]
    assert lock.held is False


def _no_session(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a run that does not own the lock must not open a database session")


@asynccontextmanager
async def _held_elsewhere(*_args: Any, **_kwargs: Any) -> Any:
    yield False


class _RedisDown:
    async def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis is down")

    async def eval(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis is down")


def test_a_run_that_finds_the_lock_held_is_skipped_as_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "_LOOP", None)
    monkeypatch.setattr("app.core.redis.exclusive_task_lock", _held_elsewhere)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _no_session)

    assert tasks.reconcile_pending_orders.run() == {"skipped": True, "reason": "already_running"}


def test_a_redis_outage_is_reported_as_an_outage_not_as_another_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "_LOOP", None)
    monkeypatch.setattr(redis_module, "_get_pool", lambda: object())
    monkeypatch.setattr(redis_module.aioredis, "Redis", lambda **_kwargs: _RedisDown())
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _no_session)

    assert tasks.reconcile_pending_orders.run() == {
        "skipped": True,
        "reason": "owner_lock_unavailable",
    }
