"""Worker heartbeat repository.

Small persistence wrapper for observability-only worker liveness rows.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from app.db.models import WorkerHeartbeat

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class WorkerHeartbeatRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_component(self, component: str) -> WorkerHeartbeat | None:
        result = await self.db.execute(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.component == component)
            .order_by(desc(WorkerHeartbeat.last_seen_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert_heartbeat(
        self,
        component: str,
        worker_name: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkerHeartbeat:
        result = await self.db.execute(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.component == component,
                WorkerHeartbeat.worker_name == worker_name,
            )
        )
        row = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None:
            row = WorkerHeartbeat(
                component=component,
                worker_name=worker_name,
                last_seen_at=now,
                status=status,
                payload=payload or {},
            )
            self.db.add(row)
        else:
            row.last_seen_at = now
            row.status = status
            row.payload = payload or {}
        await self.db.flush()
        return row

    async def list_all(self) -> Sequence[WorkerHeartbeat]:
        result = await self.db.execute(
            select(WorkerHeartbeat).order_by(
                WorkerHeartbeat.component,
                WorkerHeartbeat.worker_name,
            )
        )
        return result.scalars().all()
