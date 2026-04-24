"""
Trades route — trade history + trade journal CRUD.
GET  /v1/trades          - paginated trade history with journal fields
GET  /v1/trades/{id}     - single trade detail
PATCH /v1/trades/{id}/journal - update journal fields (notes, tags, emotion, rating)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import TradeJournalUpdate, TradeList, TradeOut
from app.db.models import Trade, User
from app.db.session import get_db

router = APIRouter(prefix="/trades", tags=["trades"])
log = structlog.get_logger()


@router.get("", response_model=TradeList)
async def list_trades(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ticker: str | None = Query(None),
    tagged: str | None = Query(None, description="Filter by tag"),
    has_notes: bool | None = Query(None, description="Only return journaled trades"),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeList:
    """Paginated trade history including journal annotations."""
    q = select(Trade).where(Trade.is_dry_run == False)  # noqa: E712

    if ticker:
        q = q.where(Trade.ticker == ticker.upper())
    if has_notes is True:
        q = q.where(Trade.journal_notes.isnot(None))
    if tagged:
        q = q.where(Trade.journal_tags.contains([tagged]))

    total_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = total_result.scalar_one()

    q = q.order_by(desc(Trade.opened_at)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    trades = result.scalars().all()

    return TradeList(
        items=[_trade_to_out(t) for t in trades],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{trade_id}", response_model=TradeOut)
async def get_trade(
    trade_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeOut:
    """Get single trade detail with journal."""
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _trade_to_out(trade)


@router.patch("/{trade_id}/journal", response_model=TradeOut)
async def update_trade_journal(
    trade_id: uuid.UUID,
    body: TradeJournalUpdate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeOut:
    """
    Update the journal annotation for a closed trade.
    All fields are optional — only provided fields are updated.
    """
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    if body.notes is not None:
        trade.journal_notes = body.notes or None  # empty string → null
    if body.tags is not None:
        trade.journal_tags = [t.strip().lower() for t in body.tags if t.strip()]
    if body.emotion is not None:
        trade.journal_emotion = body.emotion
    if body.rating is not None:
        trade.journal_rating = body.rating

    trade.journal_updated_at = datetime.now(timezone.utc)
    await db.flush()

    log.info("trade.journal_updated", trade_id=str(trade_id))
    return _trade_to_out(trade)


@router.delete("/{trade_id}/journal", response_model=TradeOut)
async def clear_trade_journal(
    trade_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeOut:
    """Clear all journal annotations from a trade."""
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade.journal_notes = None
    trade.journal_tags = None
    trade.journal_emotion = None
    trade.journal_rating = None
    trade.journal_updated_at = datetime.now(timezone.utc)
    await db.flush()

    return _trade_to_out(trade)


@router.get("/journal/tags", response_model=list[str])
async def list_journal_tags(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Return all unique tags used across journaled trades (for autocomplete)."""
    result = await db.execute(
        select(Trade.journal_tags).where(Trade.journal_tags.isnot(None))
    )
    all_tags: set[str] = set()
    for (tags,) in result:
        if tags:
            all_tags.update(tags)
    return sorted(all_tags)


# ── Internal helper ───────────────────────────────────────────────────────────

def _trade_to_out(t: Trade) -> TradeOut:
    return TradeOut(
        id=t.id,
        ticker=t.ticker,
        side=t.side,
        quantity=t.quantity,
        open_price=t.open_price,
        close_price=t.close_price,
        realized_pnl=t.realized_pnl,
        opened_at=t.opened_at,
        closed_at=t.closed_at,
        is_dry_run=t.is_dry_run,
        journal_notes=t.journal_notes,
        journal_tags=t.journal_tags,
        journal_emotion=t.journal_emotion,
        journal_rating=t.journal_rating,
        journal_updated_at=t.journal_updated_at,
    )
