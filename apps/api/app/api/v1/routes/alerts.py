"""Alerts routes — list, mark-read, test, channel config."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import AlertOut
from app.db.models import Alert, User
from app.db.session import get_db
from app.services.alert_service import AlertService

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    is_read: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Alert]:
    q = select(Alert).order_by(desc(Alert.created_at)).limit(limit)
    if is_read is not None:
        q = q.where(Alert.is_read == is_read)
    return list((await db.execute(q)).scalars().all())


@router.post("/test")
async def test_alert(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a test alert to all configured channels."""
    svc = AlertService(db)
    alert = await svc.send(
        alert_type="test",
        title="Test Alert",
        message="This is a test notification from T212 CashGuard Trader.",
        severity="info",
    )
    return {"sent": True, "alert_id": str(alert.id)}


@router.patch("/{alert_id}/read")
async def mark_alert_read(
    alert_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert:
        alert.is_read = True
        await db.flush()
    return {"read": True}


@router.post("/mark-all-read")
async def mark_all_read(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import update
    await db.execute(
        update(Alert).where(Alert.is_read == False).values(is_read=True)  # noqa: E712
    )
    return {"marked_read": True}
