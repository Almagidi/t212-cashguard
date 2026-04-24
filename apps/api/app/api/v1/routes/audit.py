"""Audit log routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import AuditLogList
from app.db.models import AuditLog, User
from app.db.session import get_db

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditLogList)
async def get_audit_logs(
    action: str | None = Query(None),
    entity_type: str | None = Query(None),
    actor: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuditLogList:
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if actor:
        q = q.where(AuditLog.actor.ilike(f"%{actor}%"))

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total: int = total_result.scalar_one()

    q = q.order_by(desc(AuditLog.occurred_at)).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(q)).scalars().all()

    return AuditLogList(items=list(items), total=total, page=page, page_size=page_size)
