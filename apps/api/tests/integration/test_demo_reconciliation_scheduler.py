from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.api.deps import get_broker
from app.core.config import settings
from app.db.models import AppSettings, AuditLog
from app.main import app
from app.services.demo_reconciliation_scheduler import DemoReconciliationScheduler
from app.services.demo_reconciliation_worker import DemoReconciliationWorkerRunSummary
from app.services.safety_policy import SafetyPolicyViolation


class SchedulerBroker:
    environment = "demo"

    def __init__(self) -> None:
        self.placement_calls = 0
        self.cancel_calls = 0
        self.modify_calls = 0

    async def place_market_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.placement_calls += 1
        raise AssertionError("scheduler must not place broker orders")

    async def cancel_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.cancel_calls += 1
        raise AssertionError("scheduler must not cancel broker orders")

    async def modify_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.modify_calls += 1
        raise AssertionError("scheduler must not modify broker orders")


def _worker_summary(**overrides: Any) -> DemoReconciliationWorkerRunSummary:
    now = datetime.now(UTC)
    values = {
        "run_id": uuid.uuid4(),
        "started_at": now,
        "finished_at": now,
        "duration_ms": 1,
        "outcome": "completed",
        "worker_enabled": True,
        "read_only_broker_calls": True,
        "no_broker_order_sent": True,
        "app_mode": "demo",
        "broker_environment": "demo",
        "live_trading_enabled": False,
        "batch_size": 10,
        "candidates_found": 1,
        "attempted": 1,
        "succeeded": 1,
        "missing": 0,
        "skipped": 0,
        "rate_limited": 0,
        "failed": 0,
        "unchanged": 0,
        "updated_order_ids": [],
        "failed_order_ids": [],
        "rate_limited_order_ids": [],
        "order_results": [],
        "audit_event_ids": [],
        "message": None,
        "warnings": [],
    }
    values.update(overrides)
    return DemoReconciliationWorkerRunSummary(**values)


@dataclass
class FakeWorker:
    calls: list[str]
    summary: DemoReconciliationWorkerRunSummary | None = None
    exc: Exception | None = None
    delay_seconds: float = 0

    async def run_once(self) -> DemoReconciliationWorkerRunSummary:
        self.calls.append("run_once")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.exc:
            raise self.exc
        return self.summary or _worker_summary()


async def _actions(db) -> list[str]:
    return [
        audit.action
        for audit in (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
    ]


def _scheduler(db, broker, worker: FakeWorker) -> DemoReconciliationScheduler:
    return DemoReconciliationScheduler(
        db,
        broker,
        worker_factory=lambda *_args, **_kwargs: worker,
        actor="test-scheduler",
    )


@pytest.fixture(autouse=True)
def _demo_scheduler_safety(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS", 120, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS", 10, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS", 300, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 60, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP", False, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_LOCK_TTL_SECONDS", 90, raising=False
    )


def test_scheduler_config_defaults_are_disabled() -> None:
    from app.core.config import Settings

    defaults = Settings(_env_file=None)

    assert defaults.DEMO_RECONCILIATION_SCHEDULER_ENABLED is False
    assert defaults.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("APP_MODE", "paper"),
        ("T212_ENVIRONMENT", "live"),
        ("LIVE_TRADING_ENABLED", True),
    ],
)
async def test_scheduler_refuses_unsafe_global_boundaries(
    db,
    monkeypatch,
    setting_name,
    unsafe_value,
):
    db.add(AppSettings(id=1, extra={}))
    await db.flush()
    monkeypatch.setattr(settings, setting_name, unsafe_value)
    worker = FakeWorker(calls=[])

    with pytest.raises(SafetyPolicyViolation):
        await _scheduler(db, SchedulerBroker(), worker).run_once()

    assert worker.calls == []


@pytest.mark.asyncio
async def test_scheduler_refuses_non_demo_broker_environment(db):
    broker = SchedulerBroker()
    broker.environment = "live"
    worker = FakeWorker(calls=[])

    with pytest.raises(SafetyPolicyViolation):
        await _scheduler(db, broker, worker).run_once()

    assert worker.calls == []


@pytest.mark.asyncio
async def test_scheduler_skips_when_worker_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", False)
    worker = FakeWorker(calls=[])

    result = await _scheduler(db, SchedulerBroker(), worker).run_once()

    assert result.outcome == "skipped"
    assert result.skip_reason == "worker_disabled"
    assert worker.calls == []


@pytest.mark.asyncio
async def test_scheduler_run_once_calls_existing_worker_once_under_safe_conditions(db):
    db.add(AppSettings(id=1, extra={}))
    await db.flush()
    broker = SchedulerBroker()
    worker = FakeWorker(calls=[], summary=_worker_summary(candidates_found=2, attempted=2))

    result = await _scheduler(db, broker, worker).run_once()

    assert result.outcome == "completed"
    assert result.worker_summary is not None
    assert result.worker_summary["attempted"] == 2
    assert worker.calls == ["run_once"]
    assert broker.placement_calls == 0
    assert broker.cancel_calls == 0
    assert broker.modify_calls == 0


@pytest.mark.asyncio
async def test_scheduler_prevents_overlapping_runs_and_records_skip(db):
    db.add(AppSettings(id=1, extra={}))
    await db.flush()
    worker = FakeWorker(calls=[], delay_seconds=0.05)
    scheduler = _scheduler(db, SchedulerBroker(), worker)

    first = asyncio.create_task(scheduler.run_once())
    await asyncio.sleep(0)
    second = await scheduler.run_once()
    first_result = await first

    assert first_result.outcome == "completed"
    assert second.outcome == "skipped"
    assert second.skip_reason == "already_running"
    assert worker.calls == ["run_once"]
    assert "demo_reconciliation_scheduler_tick_skipped" in await _actions(db)


@pytest.mark.asyncio
async def test_scheduler_missing_history_summary_is_not_fatal(db):
    worker = FakeWorker(
        calls=[], summary=_worker_summary(candidates_found=1, attempted=1, missing=1, succeeded=0)
    )

    result = await _scheduler(db, SchedulerBroker(), worker).run_once()

    assert result.outcome == "completed"
    assert result.worker_summary is not None
    assert result.worker_summary["missing"] == 1
    assert result.consecutive_failures == 0


@pytest.mark.asyncio
async def test_scheduler_rate_limit_sets_backoff_and_consecutive_count(db):
    db.add(AppSettings(id=1, extra={}))
    await db.flush()
    worker = FakeWorker(
        calls=[], summary=_worker_summary(outcome="rate_limited", rate_limited=1, succeeded=0)
    )
    scheduler = _scheduler(db, SchedulerBroker(), worker)

    result = await scheduler.run_once()
    blocked = await scheduler.run_once()
    status = await scheduler.get_status()

    assert result.outcome == "rate_limited"
    assert result.next_run_not_before is not None
    assert blocked.outcome == "skipped"
    assert blocked.skip_reason == "backoff_active"
    assert worker.calls == ["run_once"]
    assert status.consecutive_rate_limits == 1
    assert status.total_rate_limited_runs == 1
    assert "demo_reconciliation_scheduler_rate_limited" in await _actions(db)


@pytest.mark.asyncio
async def test_scheduler_handles_worker_exception_safely(db):
    worker = FakeWorker(calls=[], exc=RuntimeError("secret-token-123 failed"))

    result = await _scheduler(db, SchedulerBroker(), worker).run_once()

    assert result.outcome == "failed"
    assert result.last_error_message == "RuntimeError"
    assert result.consecutive_failures == 1
    assert "secret-token-123" not in str(result)
    assert "demo_reconciliation_scheduler_failed" in await _actions(db)


@pytest.mark.asyncio
async def test_scheduler_status_endpoint_returns_safe_status(client, auth_headers):
    response = await client.get(
        "/v1/broker/trading212/reconciliation/scheduler/status",
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["app_mode"] == "demo"
    assert data["broker_environment"] == "demo"
    assert data["live_trading_enabled"] is False
    assert data["no_broker_order_sent"] is True
    assert data["read_only_broker_calls"] is True
    assert "api_key" not in str(data).lower()


@pytest.mark.asyncio
async def test_scheduler_manual_run_once_endpoint_obeys_safety_gates(
    client,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_MODE", "paper")

    response = await client.post(
        "/v1/broker/trading212/reconciliation/scheduler/run-once",
        headers=auth_headers,
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_manual_run_once_endpoint_uses_read_only_worker(
    client,
    auth_headers,
):
    broker = SchedulerBroker()

    app.dependency_overrides[get_broker] = lambda: broker
    try:
        response = await client.post(
            "/v1/broker/trading212/reconciliation/scheduler/run-once",
            headers=auth_headers,
        )
    finally:
        app.dependency_overrides.pop(get_broker, None)

    assert response.status_code == 200
    data = response.json()
    assert data["no_broker_order_sent"] is True
    assert data["read_only_broker_calls"] is True
    assert broker.placement_calls == 0
    assert broker.cancel_calls == 0
    assert broker.modify_calls == 0
