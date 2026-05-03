"""Unit tests for persisted worker heartbeat repository."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.db.models import WorkerHeartbeat
from app.db.repositories.worker_heartbeat_repo import WorkerHeartbeatRepository


@pytest.mark.asyncio
async def test_repository_upserts_heartbeat(db):
    repo = WorkerHeartbeatRepository(db)

    row = await repo.upsert_heartbeat(
        "celery_worker",
        "worker-a",
        "healthy",
        {"source": "test"},
    )

    assert row.component == "celery_worker"
    assert row.worker_name == "worker-a"
    assert row.status == "healthy"
    assert row.payload == {"source": "test"}
    assert isinstance(row.last_seen_at, datetime)


@pytest.mark.asyncio
async def test_repeated_heartbeat_updates_same_row_not_duplicate(db):
    repo = WorkerHeartbeatRepository(db)

    first = await repo.upsert_heartbeat("celery_worker", "worker-a", "healthy")
    await db.commit()
    first_id = first.id
    first_seen = first.last_seen_at
    second = await repo.upsert_heartbeat(
        "celery_worker",
        "worker-a",
        "healthy",
        {"sequence": 2},
    )
    await db.commit()

    count = (await db.execute(select(func.count()).select_from(WorkerHeartbeat))).scalar_one()
    assert count == 1
    assert second.id == first_id
    assert second.payload == {"sequence": 2}
    assert second.last_seen_at >= first_seen


@pytest.mark.asyncio
async def test_list_all_returns_ordered_rows(db):
    repo = WorkerHeartbeatRepository(db)
    await repo.upsert_heartbeat("celery_worker", "worker-b", "healthy")
    await repo.upsert_heartbeat("celery_worker", "worker-a", "healthy")
    await repo.upsert_heartbeat("dca_paper_scheduler", "worker-a", "healthy")
    await db.commit()

    rows = await repo.list_all()

    assert [(row.component, row.worker_name) for row in rows] == [
        ("celery_worker", "worker-a"),
        ("celery_worker", "worker-b"),
        ("dca_paper_scheduler", "worker-a"),
    ]


@pytest.mark.asyncio
async def test_timestamps_round_trip(db):
    repo = WorkerHeartbeatRepository(db)
    created = await repo.upsert_heartbeat("celery_worker", "worker-a", "healthy")
    await db.commit()

    fetched = await repo.get_by_component("celery_worker")

    assert fetched is not None
    assert fetched.id == created.id
    assert isinstance(fetched.last_seen_at, datetime)
    assert isinstance(fetched.created_at, datetime)
    assert isinstance(fetched.updated_at, datetime)
