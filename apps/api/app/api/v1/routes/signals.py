"""Signals routes."""
from __future__ import annotations

import uuid  # noqa: TC003

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.api.schemas import SignalOut
from app.db.models import Signal, User
from app.db.session import get_db

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", response_model=list[SignalOut])
async def list_signals(
    status: str | None = Query(None),
    ticker: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Signal]:
    q = (
        select(Signal)
        .options(selectinload(Signal.strategy))
        .order_by(desc(Signal.generated_at))
        .limit(limit)
    )
    if status:
        q = q.where(Signal.status == status)
    if ticker:
        q = q.where(Signal.ticker == ticker.upper())
    return list((await db.execute(q)).scalars().all())


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(
    signal_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Signal:
    result = await db.execute(
        select(Signal)
        .options(selectinload(Signal.strategy))
        .where(Signal.id == signal_id)
    )
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    return sig
