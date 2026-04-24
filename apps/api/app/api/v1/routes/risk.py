"""Risk routes — profile, events, kill switch, daily reset."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user
from app.api.schemas import RiskEventOut, RiskProfileOut, RiskProfileUpdate
from app.db.models import AuditLog, RiskEvent, RiskProfile, User
from app.db.session import get_db
from app.risk.engine import activate_kill_switch, deactivate_kill_switch

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/profile", response_model=RiskProfileOut | None)
async def get_risk_profile(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RiskProfile | None:
    result = await db.execute(
        select(RiskProfile).where(RiskProfile.is_default == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


@router.patch("/profile", response_model=RiskProfileOut)
async def update_risk_profile(
    body: RiskProfileUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RiskProfile:
    result = await db.execute(
        select(RiskProfile).where(RiskProfile.is_default == True)  # noqa: E712
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="No default risk profile found")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(profile, field, value)

    db.add(AuditLog(
        action="risk_profile_updated",
        entity_type="risk_profile",
        entity_id=str(profile.id),
        actor=current_user.email,
        payload=body.model_dump(exclude_none=True),
        occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    await db.refresh(profile)
    return profile


@router.get("/events", response_model=list[RiskEventOut])
async def get_risk_events(
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = Query(None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RiskEvent]:
    q = select(RiskEvent).order_by(desc(RiskEvent.occurred_at)).limit(limit)
    if event_type:
        q = q.where(RiskEvent.event_type == event_type)
    return list((await db.execute(q)).scalars().all())


@router.post("/kill-switch/enable")
async def enable_kill_switch(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await activate_kill_switch(db, actor=current_user.email)
    db.add(AuditLog(
        action="kill_switch_enabled",
        actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {"kill_switch_active": True}


@router.post("/kill-switch/disable")
async def disable_kill_switch(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await deactivate_kill_switch(db, actor=current_user.email)
    db.add(AuditLog(
        action="kill_switch_disabled",
        actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {"kill_switch_active": False}


@router.post("/daily-reset")
async def daily_reset(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    db.add(AuditLog(
        action="daily_reset",
        actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {
        "message": "Daily stats reset",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
