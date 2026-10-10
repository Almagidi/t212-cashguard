"""Every scheduled task in ``app.workers.tasks`` runs under a single-owner lock."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.workers import tasks
from app.workers.celery_app import celery_app

TASKS_PATH = Path(tasks.__file__)
TASK_MODULE_PREFIX = "app.workers.tasks."
SKIPPED = {"skipped": True, "reason": "already_running"}
# name -> lock lease in seconds, for the tasks that take the lock through run_monitored_task.
EXCLUSIVE_LEASES = {
    "sync_account_snapshot": 45,
    "daily_reset": 120,
    "morning_scan": 150,
    "purge_old_records": 330,
    "track_cfd_funding": 90,
}


class _LockRecorder:
    def __init__(self, *, acquired: bool) -> None:
        self.acquired = acquired
        self.requests: list[tuple[str, int]] = []
        self.held = False
        self.released = 0

    @asynccontextmanager
    async def __call__(self, name: str, *, ttl_seconds: int) -> Any:
        self.requests.append((name, ttl_seconds))
        self.held = self.acquired
        try:
            yield self.acquired
        finally:
            if self.held:
                self.released += 1
            self.held = False


@pytest.fixture(autouse=True)
def _fresh_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "_LOOP", None)


def _install_lock(monkeypatch: pytest.MonkeyPatch, *, acquired: bool) -> _LockRecorder:
    recorder = _LockRecorder(acquired=acquired)
    monkeypatch.setattr("app.core.redis.task_lock", recorder)
    return recorder


def _session_must_not_open(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a task that does not own the lock must not open a database session")


def test_exclusive_task_runs_its_body_while_holding_the_named_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _install_lock(monkeypatch, acquired=True)
    held_during_body: list[bool] = []

    async def body() -> dict[str, Any]:
        held_during_body.append(lock.held)
        return {"done": True}

    result = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45)

    assert result == {"done": True}
    assert lock.requests == [("example_task", 45)]
    assert held_during_body == [True]
    assert lock.released == 1


def test_exclusive_task_skips_its_body_when_another_owner_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _install_lock(monkeypatch, acquired=False)
    calls: list[str] = []

    async def body() -> dict[str, Any]:
        calls.append("ran")
        return {"done": True}

    result = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45)

    assert result == SKIPPED
    assert calls == []
    assert lock.requests == [("example_task", 45)]


def test_exclusive_task_releases_the_lock_and_reports_when_its_body_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _install_lock(monkeypatch, acquired=True)
    recorded: list[tuple[str, str]] = []

    async def record_failure(task_name: str, exc: Exception) -> None:
        recorded.append((task_name, str(exc)))

    async def body() -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "_record_task_failure", record_failure)

    with pytest.raises(RuntimeError, match="boom"):
        tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45)

    assert lock.released == 1
    assert recorded == [("example_task", "boom")]


def test_task_without_an_exclusive_lease_takes_no_lock_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _install_lock(monkeypatch, acquired=False)

    async def body() -> dict[str, Any]:
        return {"done": True}

    assert tasks.run_monitored_task("example_task", body) == {"done": True}
    assert lock.requests == []


@pytest.mark.parametrize(("task_name", "lease"), sorted(EXCLUSIVE_LEASES.items()))
def test_scheduled_task_skips_without_touching_the_database_when_it_is_not_the_owner(
    monkeypatch: pytest.MonkeyPatch, task_name: str, lease: int
) -> None:
    lock = _install_lock(monkeypatch, acquired=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _session_must_not_open)

    result = getattr(tasks, task_name).run()

    assert result == SKIPPED
    assert lock.requests == [(task_name, lease)]


class _RetryRequested(Exception):
    pass


class _FakeTask:
    """Stands in for a bound Celery task: a request and a recording ``retry``."""

    def __init__(self, *, redelivered: bool | None) -> None:
        delivery_info = {} if redelivered is None else {"redelivered": redelivered}
        self.request = type("Request", (), {"delivery_info": delivery_info})()
        self.retries: list[dict[str, Any]] = []

    def retry(self, **options: Any) -> Exception:
        self.retries.append(options)
        return _RetryRequested()


def test_redelivered_run_that_finds_the_lock_held_is_retried_after_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_lock(monkeypatch, acquired=False)
    task = _FakeTask(redelivered=True)
    failures: list[str] = []
    calls: list[str] = []

    async def record_failure(task_name: str, _exc: Exception) -> None:
        failures.append(task_name)

    async def body() -> dict[str, Any]:
        calls.append("ran")
        return {"done": True}

    monkeypatch.setattr(tasks, "_record_task_failure", record_failure)

    with pytest.raises(_RetryRequested):
        tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45, task=task)

    assert task.retries == [{"countdown": 45 + tasks.REDELIVERY_RETRY_MARGIN_SECONDS}]
    assert calls == []
    assert failures == []


@pytest.mark.parametrize("redelivered", [False, None])
def test_ordinary_delivery_that_finds_the_lock_held_is_skipped_not_retried(
    monkeypatch: pytest.MonkeyPatch, redelivered: bool | None
) -> None:
    _install_lock(monkeypatch, acquired=False)
    task = _FakeTask(redelivered=redelivered)

    async def body() -> dict[str, Any]:
        raise AssertionError("the body must not run without the lock")

    result = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45, task=task)

    assert result == SKIPPED
    assert task.retries == []


def test_redelivered_run_that_gets_the_lock_runs_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _install_lock(monkeypatch, acquired=True)
    task = _FakeTask(redelivered=True)

    async def body() -> dict[str, Any]:
        return {"done": True}

    result = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45, task=task)

    assert result == {"done": True}
    assert task.retries == []
    assert lock.released == 1


def test_skip_result_cannot_be_changed_by_a_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_lock(monkeypatch, acquired=False)

    async def body() -> dict[str, Any]:
        return {"done": True}

    first = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45)
    first["reason"] = "changed"
    second = tasks.run_monitored_task("example_task", body, exclusive_ttl_seconds=45)

    assert second == SKIPPED


@pytest.mark.parametrize("task_name", sorted(EXCLUSIVE_LEASES))
def test_real_task_retries_a_redelivered_run_that_finds_the_lock_held(
    monkeypatch: pytest.MonkeyPatch, task_name: str
) -> None:
    from celery.exceptions import Retry

    _install_lock(monkeypatch, acquired=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", _session_must_not_open)
    task = getattr(tasks, task_name)
    task.push_request(delivery_info={"redelivered": True})
    try:
        with pytest.raises(Retry):
            task.run()
    finally:
        task.pop_request()


@pytest.mark.parametrize("task_name", sorted(EXCLUSIVE_LEASES))
def test_redelivery_retries_are_bounded_by_the_task_retry_limit(task_name: str) -> None:
    limit = getattr(tasks, task_name).max_retries

    assert isinstance(limit, int)
    assert 1 <= limit <= 3


def _beat_interval_seconds(task_name: str) -> float | None:
    for entry in celery_app.conf.beat_schedule.values():
        if entry["task"] == f"{TASK_MODULE_PREFIX}{task_name}" and isinstance(
            entry["schedule"], int | float
        ):
            return float(entry["schedule"])
    return None


def test_lease_ends_before_the_next_run_of_every_interval_scheduled_exclusive_task() -> None:
    intervals = {name: _beat_interval_seconds(name) for name in EXCLUSIVE_LEASES}
    interval_tasks = {name: value for name, value in intervals.items() if value is not None}

    assert set(interval_tasks) == {"sync_account_snapshot"}
    for name, interval in interval_tasks.items():
        assert EXCLUSIVE_LEASES[name] < interval, (
            f"{name}: a leaked lock would still block the next run"
        )


def _task_functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(TASKS_PATH.read_text())
    found: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(
            "celery_app.task" in ast.unparse(decorator) for decorator in node.decorator_list
        ):
            found[node.name] = node
    return found


def _lock_lease(function: ast.FunctionDef) -> int | None:
    """The lease of the lock a task takes, whichever of the two ways it takes it."""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called = ast.unparse(node.func)
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if called == "task_lock" and isinstance(keywords.get("ttl_seconds"), ast.Constant):
            return int(keywords["ttl_seconds"].value)
        if called == "run_monitored_task" and isinstance(
            keywords.get("exclusive_ttl_seconds"), ast.Constant
        ):
            return int(keywords["exclusive_ttl_seconds"].value)
    return None


@pytest.mark.parametrize("task_name", sorted(EXCLUSIVE_LEASES))
def test_exclusive_task_passes_itself_so_a_redelivery_can_be_retried(task_name: str) -> None:
    function = _task_functions()[task_name]
    monitored_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "run_monitored_task"
    ]

    assert len(monitored_calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in monitored_calls[0].keywords}
    assert keywords.get("task") == "self"
    assert keywords.get("exclusive_ttl_seconds") == str(EXCLUSIVE_LEASES[task_name])
    assert isinstance(function.body[-1], ast.Return)
    assert function.body[-1].value is monitored_calls[0]


def test_every_task_in_the_module_takes_a_lock_that_outlives_its_hard_time_limit() -> None:
    functions = _task_functions()
    registered = {
        name.removeprefix(TASK_MODULE_PREFIX)
        for name in celery_app.tasks
        if name.startswith(TASK_MODULE_PREFIX)
    }

    assert set(functions) == registered
    for name, function in sorted(functions.items()):
        lease = _lock_lease(function)
        assert lease is not None, f"{name} runs without a single-owner lock"
        hard_limit = getattr(tasks, name).time_limit
        assert hard_limit is not None, f"{name} has no hard time limit for its lease to outlive"
        assert lease > hard_limit, f"{name}: lease {lease}s must outlive time_limit {hard_limit}s"


def test_every_scheduled_entry_for_this_module_is_a_registered_task() -> None:
    scheduled = {
        entry["task"].removeprefix(TASK_MODULE_PREFIX)
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"].startswith(TASK_MODULE_PREFIX)
    }

    assert scheduled <= set(_task_functions())
    assert set(EXCLUSIVE_LEASES) <= scheduled
