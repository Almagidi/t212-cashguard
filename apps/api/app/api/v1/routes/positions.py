"""Positions routes — live positions from broker."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_broker, get_current_user
from app.api.schemas import PositionOut
from app.api.v1.routes._broker_errors import broker_http_exception
from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
async def list_positions(
    _: object = Depends(get_current_user),
    broker=Depends(get_broker),
) -> list[PositionOut]:
    try:
        async with broker as b:
            raw = await b.get_positions()
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc

    return [
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
