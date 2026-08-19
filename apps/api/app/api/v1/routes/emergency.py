"""Emergency controls — kill switch, flatten, cancel all."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_admin
from app.api.schemas import EmergencyActionResult
from app.db.session import get_db
from app.services.system_control import SystemControlService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import User

router = APIRouter(prefix="/emergency", tags=["emergency"])


@router.post("/kill-switch", response_model=EmergencyActionResult)
async def emergency_kill_switch(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> EmergencyActionResult:
    """Activate kill switch AND disable auto-trading in one atomic action."""
    message = await SystemControlService(db, current_user.id).activate_kill_switch(
        current_user.email
    )
    return EmergencyActionResult(
        success=True,
        action="kill_switch",
        message=message,
        timestamp=datetime.now(UTC),
    )


@router.post("/auto-trading/off", response_model=EmergencyActionResult)
async def disable_auto_trading(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> EmergencyActionResult:
    message = await SystemControlService(db, current_user.id).pause_auto_trading(current_user.email)
    return EmergencyActionResult(
        success=True,
        action="auto_trading_off",
        message=message,
        timestamp=datetime.now(UTC),
    )


@router.post("/auto-trading/on", response_model=EmergencyActionResult)
async def enable_auto_trading(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> EmergencyActionResult:
    try:
        message = await SystemControlService(db, current_user.id).resume_auto_trading(
            current_user.email
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EmergencyActionResult(
        success=True,
        action="auto_trading_on",
        message=message,
        timestamp=datetime.now(UTC),
    )


@router.post("/cancel-all", response_model=EmergencyActionResult)
async def emergency_cancel_all(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> EmergencyActionResult:
    summary = await SystemControlService(db, current_user.id).cancel_all_pending_summary(
        current_user.email
    )
    return EmergencyActionResult(
        success=summary.failed == 0,
        action="cancel_all",
        message=summary.message,
        timestamp=datetime.now(UTC),
    )


@router.post("/flatten-all", response_model=EmergencyActionResult)
async def emergency_flatten_all(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> EmergencyActionResult:
    """Close all open positions via market sell orders."""
    message = await SystemControlService(db, current_user.id).flatten_all(current_user.email)
    return EmergencyActionResult(
        success=True,
        action="flatten_all",
        message=message,
        timestamp=datetime.now(UTC),
    )
