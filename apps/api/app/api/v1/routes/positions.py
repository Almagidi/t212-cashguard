"""Positions routes — live positions from broker."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select

from app.api.deps import get_broker, get_current_user
from app.api.schemas import PositionOut
from app.api.v1.routes._broker_errors import broker_http_exception
from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.db.models import BrokerConnection, PositionSnapshot, User
from app.db.session import get_db
from app.execution.paper_engine import PAPER_BROKER

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
async def list_positions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    broker=Depends(get_broker),
) -> list[PositionOut]:
    try:
        async with broker as b:
            raw = await b.get_positions()
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc

    positions = [
        PositionOut(
            ticker=p.get("ticker", ""),
            quantity=float(p.get("quantity", 0)),
            avg_price=float(p.get("averagePrice", 0)),
            current_price=p.get("currentPrice"),
            unrealized_pnl=p.get("ppl"),
            quantity_available=p.get("maxSell"),
            value=(
                float(p.get("quantity", 0)) * float(p.get("currentPrice", 0))
                if p.get("currentPrice")
                else None
            ),
        )
        for p in raw
    ]
    existing = {position.ticker.upper() for position in positions}

    paper_result = await db.execute(
        select(PositionSnapshot)
        .join(BrokerConnection, PositionSnapshot.connection_id == BrokerConnection.id)
        .where(
            BrokerConnection.user_id == current_user.id,
            BrokerConnection.broker == PAPER_BROKER,
        )
        .order_by(desc(PositionSnapshot.snapshotted_at))
    )
    latest_paper: dict[str, PositionSnapshot] = {}
    for snapshot in paper_result.scalars().all():
        latest_paper.setdefault(snapshot.ticker.upper(), snapshot)

    for snapshot in latest_paper.values():
        if snapshot.quantity <= 0 or snapshot.ticker.upper() in existing:
            continue
        positions.append(
            PositionOut(
                ticker=snapshot.ticker,
                quantity=float(snapshot.quantity),
                avg_price=float(snapshot.avg_price),
                current_price=float(snapshot.current_price) if snapshot.current_price is not None else None,
                unrealized_pnl=float(snapshot.unrealized_pnl) if snapshot.unrealized_pnl is not None else None,
                quantity_available=float(snapshot.quantity_available) if snapshot.quantity_available is not None else None,
                value=(
                    float(snapshot.quantity) * float(snapshot.current_price)
                    if snapshot.current_price is not None
                    else None
                ),
            )
        )
    return positions


@router.post("/refresh")
async def refresh_positions(
    _: object = Depends(get_current_user),
    broker=Depends(get_broker),
) -> dict:
    from datetime import UTC, datetime
    try:
        async with broker as b:
            positions = await b.get_positions()
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc
    return {
        "positions": len(positions),
        "refreshed_at": datetime.now(UTC).isoformat(),
    }
