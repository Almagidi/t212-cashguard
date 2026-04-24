"""App settings routes."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.api.deps import get_current_admin, get_current_user
from app.api.schemas import (
    AppSettingsOut,
    AppSettingsUpdate,
    LiveReadinessActionRequest,
    LiveReadinessStatus,
)
from app.db.models import AppSettings, AuditLog, User
from app.db.session import get_db
from app.services.live_readiness import LiveReadinessError, LiveReadinessService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=AppSettingsOut)
async def get_settings(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not initialised — run seed")
    return s


@router.patch("", response_model=AppSettingsOut)
async def update_settings(
    body: AppSettingsUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not initialised")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(s, field, value)

    db.add(AuditLog(
        action="settings_updated",
        actor=current_user.email,
        payload=body.model_dump(exclude_none=True),
        occurred_at=datetime.now(UTC),
    ))
    await db.flush()
    await db.refresh(s)
    return s


@router.get("/live-readiness", response_model=LiveReadinessStatus)
async def get_live_readiness(
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> LiveReadinessStatus:
    return LiveReadinessStatus(**(await LiveReadinessService(db).evaluate()))


@router.post("/live-readiness", response_model=LiveReadinessStatus)
async def update_live_readiness(
    body: LiveReadinessActionRequest,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> LiveReadinessStatus:
    try:
        status = await LiveReadinessService(db).apply_action(
            action=body.action,
            actor=current_user.email,
            notes=body.notes,
        )
    except LiveReadinessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LiveReadinessStatus(**status)
