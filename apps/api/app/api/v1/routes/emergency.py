"""Emergency controls — kill switch, flatten, cancel all."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.api.schemas import EmergencyActionResult
from app.db.models import User
from app.db.session import get_db
from app.services.system_control import SystemControlService

router = APIRouter(prefix="/emergency", tags=["emergency"])


@router.post("/kill-switch", response_model=EmergencyActionResult)
async def emergency_kill_switch(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Activate kill switch AND disable auto-trading in one atomic action."""
    message = await SystemControlService(db, current_user.id).activate_kill_switch(current_user.email)
    return EmergencyActionResult(
        success=True, action="kill_switch",
        message=message,
        timestamp=datetime.now(timezone.utc),
    )


@router.post("/auto-trading/off", response_model=EmergencyActionResult)
async def disable_auto_trading(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    message = await SystemControlService(db, current_user.id).pause_auto_trading(current_user.email)
    return EmergencyActionResult(
        success=True, action="auto_trading_off",
        message=message, timestamp=datetime.now(timezone.utc),
    )


@router.post("/auto-trading/on", response_model=EmergencyActionResult)
async def enable_auto_trading(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        message = await SystemControlService(db, current_user.id).resume_auto_trading(current_user.email)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EmergencyActionResult(
        success=True, action="auto_trading_on",
        message=message, timestamp=datetime.now(timezone.utc),
    )


@router.post("/cancel-all", response_model=EmergencyActionResult)
async def emergency_cancel_all(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    message = await SystemControlService(db, current_user.id).cancel_all_pending(current_user.email)
    return EmergencyActionResult(
        success=True, action="cancel_all",
        message=message,
        timestamp=datetime.now(timezone.utc),
    )


@router.post("/flatten-all", response_model=EmergencyActionResult)
async def emergency_flatten_all(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Close all open positions via market sell orders."""
    message = await SystemControlService(db, current_user.id).flatten_all(current_user.email)
    return EmergencyActionResult(
        success=True, action="flatten_all",
        message=message,
        timestamp=datetime.now(timezone.utc),
    )
