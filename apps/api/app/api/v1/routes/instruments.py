"""Instruments routes — list, sync, get by ticker."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_broker, get_current_user
from app.api.schemas import InstrumentList, InstrumentOut
from app.db.models import Instrument, User
from app.db.session import get_db

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentList)
async def list_instruments(
    search: str | None = Query(None),
    type: str | None = Query(None),
    enabled_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InstrumentList:
    q = select(Instrument)
    if search:
        q = q.where(
            Instrument.ticker.ilike(f"%{search}%")
            | Instrument.name.ilike(f"%{search}%")
        )
    if type:
        q = q.where(Instrument.type == type.upper())
    if enabled_only:
        q = q.where(Instrument.trading_enabled == True)  # noqa: E712

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total: int = total_result.scalar_one()

    q = q.offset((page - 1) * page_size).limit(page_size).order_by(Instrument.ticker)
    items = (await db.execute(q)).scalars().all()
    return InstrumentList(items=list(items), total=total)


@router.post("/sync")
async def sync_instruments(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    broker=Depends(get_broker),
) -> dict:
    async with broker as b:
        raw_list = await b.get_instruments()

    now = datetime.now(timezone.utc)
    synced = 0
    for raw in raw_list:
        ticker = raw.get("ticker", "")
        if not ticker:
            continue
        result = await db.execute(select(Instrument).where(Instrument.ticker == ticker))
        inst = result.scalar_one_or_none()
        if inst:
            inst.name = raw.get("name", inst.name)
            inst.type = raw.get("type", inst.type)
            inst.currency_code = raw.get("currencyCode", inst.currency_code)
            inst.extended_hours = raw.get("extendedHours", False)
            inst.working_schedule_id = raw.get("workingScheduleId")
            inst.synced_at = now
            inst.raw = raw
        else:
            db.add(Instrument(
                id=uuid.uuid4(),
                ticker=ticker,
                name=raw.get("name", ticker),
                type=raw.get("type", "STOCK"),
                currency_code=raw.get("currencyCode", "USD"),
                extended_hours=raw.get("extendedHours", False),
                working_schedule_id=raw.get("workingScheduleId"),
                trading_enabled=True,
                synced_at=now,
                raw=raw,
            ))
        synced += 1

    return {"synced": synced, "timestamp": now.isoformat()}


@router.get("/{ticker}", response_model=InstrumentOut)
async def get_instrument(
    ticker: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Instrument:
    result = await db.execute(
        select(Instrument).where(Instrument.ticker == ticker.upper())
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail=f"Instrument {ticker} not found")
    return inst
