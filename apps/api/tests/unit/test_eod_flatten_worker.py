"""Celery EOD wrapper proofs; all broker-capable collaborators are replaced."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app.workers import tasks

TASKS_PATH = Path(__file__).resolve().parents[2] / "app" / "workers" / "tasks.py"
NOW = datetime(2026, 7, 6, 20, 5, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


class FakeResult:
    def __init__(self, value: Any, *, many: bool = False) -> None:
        self.value = value
        self.many = many

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self.value)


class FakeSession:
    def __init__(self, settings_obj: Any, strategies: list[Any]) -> None:
        self.results = [FakeResult(settings_obj), FakeResult(strategies, many=True)]
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _query: Any) -> FakeResult:
        return self.results.pop(0)

    async def commit(self) -> None:
        self.commits += 1


class FakePositionMonitor:
    calls: ClassVar[list[tuple[Any, list[Any], datetime]]] = []

    def __init__(self, db: Any) -> None:
        self.db = db

    async def eod_flatten(
        self,
        strategies: list[Any],
        *,
        now_utc: datetime,
    ) -> dict[str, Any]:
        type(self).calls.append((self.db, strategies, now_utc))
        return {"flattened": 1, "operations_created": 1}


@asynccontextmanager
async def _acquired_lock(*_args: Any, **_kwargs: Any):
    yield True


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePositionMonitor.calls = []
    monkeypatch.setattr(tasks, "_LOOP", None)
    monkeypatch.setattr(tasks, "datetime", FrozenDateTime)


def test_worker_has_no_utc_string_latch_or_direct_broker_path() -> None:
    tree = ast.parse(TASKS_PATH.read_text())
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "check_eod_flatten"
    )
    source = ast.unparse(node)

    assert "strftime" not in source
    assert "current_hhmm" not in source
    assert "should_flatten = any" not in source
    assert "task_lock" in source
    assert "PositionMonitor" in source
    assert "Trading212Adapter" not in source
    assert "create_trading212_provider_adapter" not in source


def test_worker_preserves_kill_switch_before_service_or_broker_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = FakeSession(SimpleNamespace(kill_switch_active=True), [])

    async def complete(_db: Any, _name: str, summary: dict[str, Any]) -> dict[str, Any]:
        await _db.commit()
        return summary

    monkeypatch.setattr("app.core.redis.task_lock", _acquired_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr("app.services.position_monitor.PositionMonitor", FakePositionMonitor)
    monkeypatch.setattr(tasks, "_complete_task", complete)

    result = tasks.check_eod_flatten.run()

    assert result == {"flattened": 0, "reason": "kill_switch"}
    assert FakePositionMonitor.calls == []
    assert fake_db.commits == 1


def test_worker_passes_all_eligible_strategies_and_aware_instant_to_safe_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategies = [SimpleNamespace(id="one"), SimpleNamespace(id="two")]
    fake_db = FakeSession(SimpleNamespace(kill_switch_active=False), strategies)

    async def complete(_db: Any, _name: str, summary: dict[str, Any]) -> dict[str, Any]:
        await _db.commit()
        return summary

    monkeypatch.setattr("app.core.redis.task_lock", _acquired_lock)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr("app.services.position_monitor.PositionMonitor", FakePositionMonitor)
    monkeypatch.setattr(tasks, "_complete_task", complete)

    result = tasks.check_eod_flatten.run()

    assert result == {"flattened": 1, "operations_created": 1}
    assert FakePositionMonitor.calls == [(fake_db, strategies, NOW)]
    assert FakePositionMonitor.calls[0][2].tzinfo is UTC
    assert fake_db.commits == 1
