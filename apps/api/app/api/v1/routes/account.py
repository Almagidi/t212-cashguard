"""Account routes — summary, cash guard status."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_broker
from app.api.schemas import AccountSummaryOut, CashGuardStatus
from app.core.config import settings
from app.db.session import get_db

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/summary", response_model=AccountSummaryOut)
async def account_summary(
    _: object = Depends(get_current_user),
    broker=Depends(get_broker),
):
    async with broker as b:
        summary = await b.get_account_summary()

    return AccountSummaryOut(
        total_value=summary.get("total", 0),
        cash=summary.get("cash", 0),
        free_funds=summary.get("free", 0),
        invested=summary.get("invested", 0),
        result=summary.get("result", 0),
        currency=summary.get("currency", "USD"),
        synced_at=datetime.now(timezone.utc),
        mode=settings.APP_MODE,
    )


@router.get("/cash-guard", response_model=CashGuardStatus)
async def cash_guard_status(
    _: object = Depends(get_current_user),
    broker=Depends(get_broker),
):
    async with broker as b:
        summary = await b.get_account_summary()

    free = float(summary.get("free", 0))
    cash = float(summary.get("cash", 0))
    return CashGuardStatus(
        available_to_trade=free,
        reserved=max(0.0, cash - free),
        total_cash=cash,
        cash_only_mode=True,  # Hardcoded — never False
        currency=summary.get("currency", "USD"),
    )
