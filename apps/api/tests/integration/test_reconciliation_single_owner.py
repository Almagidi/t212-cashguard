"""Order reconciliation has one owner at a time, whichever entry point starts it.

Entry points: the scheduled Celery task, the demo reconciliation worker (run by the
in-process scheduler and by two operator routes).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.api.deps import get_broker
from app.core.config import settings
from app.db.models import AppSettings, AuditLog
from app.main import app
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.workers import tasks
from tests.integration import test_demo_reconciliation_worker as worker_cases

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SCHEDULED_SKIP = {"skipped": True, "reason": "already_running"}
FILLED = {"items": [{"id": "48850886521", "status": "FILLED"}]}


class Locks:
    """In-memory stand-in for the Redis task lock: one holder per name."""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.requests: list[tuple[str, int]] = []

    @asynccontextmanager
    async def __call__(self, name: str, *, ttl_seconds: int) -> Any:
        self.requests.append((name, ttl_seconds))
        acquired = name not in self.held
        if acquired:
            self.held.add(name)
        try:
            yield acquired
        finally:
            if acquired:
                self.held.discard(name)


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> Locks:
    registry = Locks()
    monkeypatch.setattr("app.core.redis.task_lock", registry)
    monkeypatch.setattr("app.core.redis.exclusive_task_lock", registry)
    return registry


@pytest.fixture(autouse=True)
def _demo_worker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_ENABLED", True)


def _owner_lock() -> str:
    from app.services.reconciliation_owner import RECONCILIATION_OWNER_LOCK

    return RECONCILIATION_OWNER_LOCK


async def _settings_row(db: AsyncSession) -> None:
    """The row the scheduler keeps its latest result in."""
    if await db.get(AppSettings, 1) is None:
        db.add(AppSettings(id=1, extra={}))
        await db.flush()


async def _held_order(db: AsyncSession) -> Any:
    order = worker_cases._demo_order(broker_order_id="48850886521")
    db.add(order)
    await db.flush()
    return order


def test_scheduled_reconciler_takes_the_shared_owner_lock(
    monkeypatch: pytest.MonkeyPatch, locks: Locks
) -> None:
    monkeypatch.setattr(tasks, "_LOOP", None)
    locks.held.add(_owner_lock())

    def no_session(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a reconciler that is not the owner must not open a session")

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", no_session)

    result = tasks.reconcile_pending_orders.run()

    assert result == SCHEDULED_SKIP
    assert [name for name, _ttl in locks.requests] == [_owner_lock()]


@pytest.mark.asyncio
async def test_demo_worker_does_nothing_while_another_owner_is_reconciling(
    db: AsyncSession, locks: Locks
) -> None:
    order = await _held_order(db)
    status_before = order.status
    locks.held.add(_owner_lock())
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    summary = await DemoReconciliationWorker(db, broker).run_once()

    assert broker.history_calls == []
    assert summary.outcome == "skipped_locked"
    assert (summary.candidates_found, summary.attempted, summary.succeeded) == (0, 0, 0)
    assert order.status == status_before
    assert [name for name, _ttl in locks.requests] == [_owner_lock()]


@pytest.mark.asyncio
async def test_demo_worker_owns_the_lock_while_it_reads_the_broker_and_releases_it(
    db: AsyncSession, locks: Locks
) -> None:
    await _held_order(db)
    held_during_read: list[bool] = []

    class RecordingBroker(worker_cases.WorkerBroker):
        async def get_historical_orders(self, **kwargs: Any) -> Any:
            held_during_read.append(_owner_lock() in locks.held)
            return await super().get_historical_orders(**kwargs)

    summary = await DemoReconciliationWorker(db, RecordingBroker(responses=[FILLED])).run_once()

    assert summary.attempted == 1
    assert held_during_read == [True]
    assert locks.held == set()
    (request,) = locks.requests
    assert request[0] == _owner_lock()
    assert request[1] >= settings.DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS


@pytest.mark.asyncio
async def test_demo_worker_releases_the_lock_when_the_broker_fails(
    db: AsyncSession, locks: Locks
) -> None:
    await _held_order(db)
    broker = worker_cases.WorkerBroker(exc=RuntimeError("broker unavailable"))

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await DemoReconciliationWorker(db, broker).run_once()

    assert broker.history_calls
    assert locks.held == set()


@pytest.mark.asyncio
async def test_a_skipped_run_is_audited_and_does_not_replace_the_last_real_summary(
    db: AsyncSession, locks: Locks
) -> None:
    db.add(AppSettings(id=1, extra={}))
    await _held_order(db)
    worker = DemoReconciliationWorker(db, worker_cases.WorkerBroker(responses=[FILLED]))
    completed = await worker.run_once()
    locks.held.add(_owner_lock())

    skipped = await worker.run_once()

    actions = [
        audit.action
        for audit in (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
    ]
    assert "demo_reconciliation_worker_skipped_locked" in actions
    status = await worker.get_worker_status()
    assert skipped.outcome == "skipped_locked"
    assert status.last_run_summary is not None
    assert status.last_run_summary["run_id"] == str(completed.run_id)


RUN_ONCE = "/v1/broker/trading212/reconciliation/run-once"
SCHEDULER_RUN_ONCE = "/v1/broker/trading212/reconciliation/scheduler/run-once"


async def _post(client: Any, auth_headers: Any, path: str, broker: Any) -> Any:
    app.dependency_overrides[get_broker] = lambda: broker
    try:
        return await client.post(path, headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_broker, None)


@pytest.mark.asyncio
async def test_operator_run_once_reports_a_skipped_run_while_another_owner_is_reconciling(
    client: Any, auth_headers: Any, db: AsyncSession, locks: Locks
) -> None:
    order = await _held_order(db)
    status_before = order.status
    await db.commit()
    locks.held.add(_owner_lock())
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    response = await _post(client, auth_headers, RUN_ONCE, broker)

    assert response.status_code == 200
    assert response.json()["outcome"] == "skipped_locked"
    assert [name for name, _ttl in locks.requests] == [_owner_lock()]
    assert broker.history_calls == []
    await db.refresh(order)
    assert order.status == status_before


@pytest.mark.asyncio
async def test_scheduler_run_once_reports_a_skipped_tick_not_a_success(
    client: Any, auth_headers: Any, db: AsyncSession, locks: Locks
) -> None:
    await _settings_row(db)
    order = await _held_order(db)
    status_before = order.status
    await db.commit()
    locks.held.add(_owner_lock())
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    response = await _post(client, auth_headers, SCHEDULER_RUN_ONCE, broker)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "skipped"
    assert body["skip_reason"] == "reconciliation_owner_busy"
    assert not body.get("total_successful_runs")
    assert [name for name, _ttl in locks.requests] == [_owner_lock()]
    assert broker.history_calls == []
    await db.refresh(order)
    assert order.status == status_before


# ── The owner commits before it stops being the owner ────────────────────────


@pytest.mark.asyncio
async def test_demo_worker_commits_its_changes_before_it_releases_the_lock(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _held_order(db)
    held_at_commit: list[bool] = []
    commit = db.commit

    async def recording_commit() -> None:
        held_at_commit.append(_owner_lock() in locks.held)
        await commit()

    monkeypatch.setattr(db, "commit", recording_commit)

    summary = await DemoReconciliationWorker(
        db, worker_cases.WorkerBroker(responses=[FILLED])
    ).run_once()

    assert summary.succeeded == 1
    assert held_at_commit == [True]
    assert locks.held == set()


@pytest.mark.asyncio
async def test_a_run_that_is_not_the_owner_commits_nothing_itself(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _held_order(db)
    locks.held.add(_owner_lock())
    commits: list[int] = []

    async def recording_commit() -> None:
        commits.append(1)

    monkeypatch.setattr(db, "commit", recording_commit)

    await DemoReconciliationWorker(db, worker_cases.WorkerBroker(responses=[FILLED])).run_once()

    assert commits == []


# ── A lock that cannot be asked for is not the same as a lock that is held ───


class _RedisDown:
    async def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis is down")

    async def eval(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a lock that was never taken must not be released")


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import redis as redis_module

    monkeypatch.setattr(redis_module, "_get_pool", lambda: object())
    monkeypatch.setattr(redis_module.aioredis, "Redis", lambda **_kwargs: _RedisDown())


@pytest.mark.asyncio
async def test_demo_worker_refuses_to_run_when_the_lock_cannot_be_requested(
    db: AsyncSession, redis_down: None
) -> None:
    from app.core.redis import TaskLockUnavailable

    await _held_order(db)
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    with pytest.raises(TaskLockUnavailable, match="Redis is unavailable"):
        await DemoReconciliationWorker(db, broker).run_once()

    assert broker.history_calls == []


@pytest.mark.asyncio
async def test_operator_run_once_says_so_when_the_lock_cannot_be_requested(
    client: Any, auth_headers: Any, db: AsyncSession, redis_down: None
) -> None:
    await _held_order(db)
    await db.commit()
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    response = await _post(client, auth_headers, RUN_ONCE, broker)

    assert response.status_code == 503
    assert "Redis is unavailable" in response.json()["detail"]
    assert broker.history_calls == []


@pytest.mark.asyncio
async def test_scheduler_counts_a_lock_outage_as_a_failed_run_not_a_success(
    client: Any, auth_headers: Any, db: AsyncSession, redis_down: None
) -> None:
    await _settings_row(db)
    await _held_order(db)
    await db.commit()
    broker = worker_cases.WorkerBroker(responses=[FILLED])

    response = await _post(client, auth_headers, SCHEDULER_RUN_ONCE, broker)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "failed"
    assert body["consecutive_failures"] == 1
    assert not body.get("total_successful_runs")
    assert broker.history_calls == []


# ── A run is stopped before the owner's lease can end ────────────────────────


class _SlowBroker(worker_cases.WorkerBroker):
    async def get_historical_orders(self, **kwargs: Any) -> Any:
        self.history_calls.append(kwargs)
        await asyncio.sleep(30)
        return FILLED


@pytest.mark.asyncio
async def test_a_run_that_overruns_is_stopped_and_releases_the_lock_without_saving(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = await _held_order(db)
    status_before = order.status
    await db.commit()
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 1)
    commits: list[int] = []
    commit = db.commit

    async def recording_commit() -> None:
        commits.append(1)
        await commit()

    monkeypatch.setattr(db, "commit", recording_commit)

    with pytest.raises(TimeoutError):
        await DemoReconciliationWorker(db, _SlowBroker()).run_once()

    assert locks.held == set()
    assert commits == []
    await db.refresh(order)
    assert order.status == status_before


@pytest.mark.asyncio
async def test_operator_run_once_reports_a_stopped_run(
    client: Any, auth_headers: Any, db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _held_order(db)
    await db.commit()
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 1)

    response = await _post(client, auth_headers, RUN_ONCE, _SlowBroker())

    assert response.status_code == 504
    assert locks.held == set()


class _SecondCallSlowBroker(worker_cases.WorkerBroker):
    """Answers for the first order at once, then never answers for the second."""

    async def get_historical_orders(self, **kwargs: Any) -> Any:
        self.history_calls.append(kwargs)
        if len(self.history_calls) > 1:
            await asyncio.sleep(30)
        return {"items": [{"id": "11111111111", "status": "FILLED"}]}


async def _two_orders(db: AsyncSession) -> tuple[Any, Any]:
    earlier = datetime.now(UTC) - timedelta(minutes=10)
    first = worker_cases._demo_order(broker_order_id="11111111111", created_at=earlier)
    second = worker_cases._demo_order(broker_order_id="22222222222")
    db.add_all([first, second])
    await db.commit()
    return first, second


@pytest.mark.asyncio
async def test_a_stopped_run_discards_the_orders_it_had_already_processed(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _second = await _two_orders(db)
    status_before = first.status
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 1)
    held_at_rollback: list[bool] = []
    rollback = db.rollback

    async def recording_rollback() -> None:
        held_at_rollback.append(_owner_lock() in locks.held)
        await rollback()

    monkeypatch.setattr(db, "rollback", recording_rollback)
    broker = _SecondCallSlowBroker()

    with pytest.raises(TimeoutError):
        await DemoReconciliationWorker(db, broker).run_once()

    assert len(broker.history_calls) == 2
    assert held_at_rollback == [True]
    assert locks.held == set()
    assert not db.dirty
    assert not db.new
    await db.commit()  # what a caller would do next
    await db.refresh(first)
    assert first.status == status_before


@pytest.mark.asyncio
async def test_a_failed_run_discards_its_unsaved_work_while_still_the_owner(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _second = await _two_orders(db)
    status_before = first.status
    held_at_rollback: list[bool] = []
    rollback = db.rollback

    async def recording_rollback() -> None:
        held_at_rollback.append(_owner_lock() in locks.held)
        await rollback()

    monkeypatch.setattr(db, "rollback", recording_rollback)

    class SecondCallFails(worker_cases.WorkerBroker):
        async def get_historical_orders(self, **kwargs: Any) -> Any:
            self.history_calls.append(kwargs)
            if len(self.history_calls) > 1:
                raise RuntimeError("broker unavailable")
            return {"items": [{"id": "11111111111", "status": "FILLED"}]}

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await DemoReconciliationWorker(db, SecondCallFails()).run_once()

    assert held_at_rollback == [True]
    await db.commit()
    await db.refresh(first)
    assert first.status == status_before


@pytest.mark.asyncio
async def test_rollback_finishes_even_when_a_second_time_limit_cancels_it(
    db: AsyncSession, locks: Locks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler's limit fires first; the worker's own fires during the rollback."""
    from app.services import demo_reconciliation_worker as worker_module

    first, _second = await _two_orders(db)
    status_before = first.status
    monkeypatch.setattr(worker_module, "reconciliation_run_limit_seconds", lambda: 1.3)
    progress: list[str] = []
    rollback = db.rollback

    async def slow_rollback() -> None:
        progress.append("started")
        await asyncio.sleep(0.6)
        await rollback()
        progress.append("finished while owner" if _owner_lock() in locks.held else "finished late")

    monkeypatch.setattr(db, "rollback", slow_rollback)

    with pytest.raises(TimeoutError):  # an ordinary error, so the scheduler records the failure
        await asyncio.wait_for(
            DemoReconciliationWorker(db, _SecondCallSlowBroker()).run_once(), timeout=1.0
        )

    assert progress == ["started", "finished while owner"]
    assert locks.held == set()
    await db.commit()
    await db.refresh(first)
    assert first.status == status_before


@pytest.mark.asyncio
async def test_scheduler_failure_record_points_at_no_discarded_audit_rows(
    client: Any,
    auth_headers: Any,
    db: AsyncSession,
    locks: Locks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _settings_row(db)
    await _two_orders(db)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 1)

    response = await _post(client, auth_headers, SCHEDULER_RUN_ONCE, _SecondCallSlowBroker())

    body = response.json()
    assert body["outcome"] == "failed"
    stored = {str(audit.id) for audit in (await db.execute(select(AuditLog))).scalars()}
    assert set(body["audit_event_ids"]) <= stored


@pytest.mark.asyncio
async def test_scheduler_does_not_save_a_stopped_runs_partial_work(
    client: Any,
    auth_headers: Any,
    db: AsyncSession,
    locks: Locks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _settings_row(db)
    first, _second = await _two_orders(db)
    status_before = first.status
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 1)

    response = await _post(client, auth_headers, SCHEDULER_RUN_ONCE, _SecondCallSlowBroker())

    assert response.status_code == 200
    assert response.json()["outcome"] == "failed"
    assert locks.held == set()
    await db.refresh(first)
    assert first.status == status_before


# ── The lock's name and lease ────────────────────────────────────────────────


def test_owner_lock_keeps_the_name_running_workers_already_use() -> None:
    assert _owner_lock() == "reconcile_pending_orders"


@pytest.mark.parametrize(
    ("ttl_setting", "run_limit", "expected"),
    [(90, 60, 90), (10, 60, 90), (90, 200, 230), (100_000, 60, 300), (0, 0, 31), (10, 400, 430)],
)
def test_owner_lease_outlasts_one_run_and_is_capped(
    monkeypatch: pytest.MonkeyPatch, ttl_setting: int, run_limit: int, expected: int
) -> None:
    from app.services.reconciliation_owner import (
        reconciliation_owner_lease_seconds,
        reconciliation_run_limit_seconds,
    )

    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_LOCK_TTL_SECONDS", ttl_setting)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", run_limit)

    assert reconciliation_owner_lease_seconds() == expected
    assert reconciliation_owner_lease_seconds() > reconciliation_run_limit_seconds()
