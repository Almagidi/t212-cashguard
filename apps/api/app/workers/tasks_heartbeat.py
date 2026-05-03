"""Persisted worker heartbeat task.

Observability-only Celery task used by operator status to evaluate worker
liveness from database state.
"""
from __future__ import annotations

import socket
from typing import Any

import structlog

from app.db.repositories.worker_heartbeat_repo import WorkerHeartbeatRepository
from app.workers.celery_app import celery_app
from app.workers.tasks import run_monitored_task

log = structlog.get_logger()

HEARTBEAT_COMPONENT = "celery_worker"
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_STALE_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 3


async def record_worker_heartbeat(
    db: Any,
    *,
    worker_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = worker_name or socket.gethostname() or "unknown"
    safe_payload = {
        "source": "celery_beat",
        "interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
        **(payload or {}),
    }
    row = await WorkerHeartbeatRepository(db).upsert_heartbeat(
        component=HEARTBEAT_COMPONENT,
        worker_name=name,
        status="healthy",
        payload=safe_payload,
    )
    await db.commit()
    summary = {
        "component": row.component,
        "worker_name": row.worker_name,
        "status": row.status,
        "last_seen_at": row.last_seen_at.isoformat(),
    }
    log.info("worker_heartbeat.recorded", **summary)
    return summary


@celery_app.task(name="app.workers.tasks_heartbeat.record_worker_heartbeat_task", bind=True, max_retries=0, time_limit=30)
def record_worker_heartbeat_task(self: Any) -> dict[str, Any]:
    """Record a persisted worker heartbeat row."""

    async def _run() -> dict[str, Any]:
        from app.db.session import AsyncSessionLocal

        request = getattr(self, "request", None)
        worker_name = getattr(request, "hostname", None) or socket.gethostname()
        async with AsyncSessionLocal() as db:
            return await record_worker_heartbeat(db, worker_name=worker_name)

    return run_monitored_task("record_worker_heartbeat", _run)
