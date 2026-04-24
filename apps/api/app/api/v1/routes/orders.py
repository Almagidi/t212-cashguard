"""Orders routes — CRUD, placement, cancellation."""
from __future__ import annotations

import uuid  # noqa: TC003
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.api.deps import get_broker, get_current_admin, get_current_user
from app.api.schemas import OrderCreate, OrderDetail, OrderOut
from app.core.config import settings
from app.db.models import AuditLog, Order, User
from app.db.repositories import OrderRepository
from app.db.session import get_db
from app.execution.engine import ExecutionEngine
from app.risk.engine import RiskEngine, RiskViolation

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
async def list_orders(
    status: str | None = Query(None),
    ticker: str | None = Query(None),
    is_dry_run: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, description="Number of records to skip (cursor-style pagination)"),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrderRepository(db)
    return await repo.list(
        status=status, ticker=ticker, is_dry_run=is_dry_run, limit=limit, offset=offset
    )


@router.post("", response_model=OrderOut, status_code=201)
async def place_order(
    body: OrderCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker=Depends(get_broker),
):
    """
    Place an order with full pre-flight risk checks.
    Order flow: risk checks → intent created → submitted to broker → reconciled.
    """
    repo = OrderRepository(db)

    # Fetch live account state for risk calculations
    async with broker as b:
        summary = await b.get_account_summary()

    available_cash = Decimal(str(summary.get("free", 0)))
    account_value = Decimal(str(summary.get("total", 0)))
    estimated_price = body.limit_price or body.stop_price or Decimal("100")

    # Count currently open positions
    open_result = await db.execute(
        select(func.count(Order.id)).where(
            Order.status.in_(["accepted", "filled"])
        )
    )
    open_count: int = open_result.scalar_one()

    # --- Risk gate (all checks must pass) ---
    risk = RiskEngine(db)
    try:
        await risk.run_all_checks(
            ticker=body.ticker,
            side=body.side,
            quantity=body.quantity,
            estimated_price=estimated_price,
            available_cash=available_cash,
            account_value=account_value,
            realized_pnl_today=Decimal("0"),
            current_open_positions=open_count,
            signal_id=body.signal_id,
        )
    except RiskViolation as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc

    # --- Execute ---
    async with broker as b:
        engine = ExecutionEngine(db, b)
        order = await engine.create_order_intent(
            ticker=body.ticker.upper(),
            side=body.side,
            order_type=body.order_type,
            quantity=body.quantity,
            signal_id=body.signal_id,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_validity=body.time_validity,
            is_dry_run=(settings.APP_MODE == "mock"),
            available_cash=available_cash,
            estimated_price=estimated_price,
        )
        order = await engine.submit_order(order)

    db.add(AuditLog(
        action="order_placed",
        entity_type="order",
        entity_id=str(order.id),
        actor=current_user.email,
        payload={"ticker": body.ticker, "side": body.side, "qty": str(body.quantity)},
        occurred_at=datetime.now(UTC),
    ))
    await db.flush()
    hydrated = await repo.get_by_id(order.id)
    return hydrated or order


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrderRepository(db)
    order = await repo.get_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker=Depends(get_broker),
):
    repo = OrderRepository(db)
    order = await repo.get_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("filled", "cancelled", "rejected"):
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel order in status: {order.status}"
        )
    async with broker as b:
        engine = ExecutionEngine(db, b)
        order = await engine.cancel_order(order)

    db.add(AuditLog(
        action="order_cancelled",
        entity_type="order",
        entity_id=str(order_id),
        actor=current_user.email,
        occurred_at=datetime.now(UTC),
    ))
    return {"cancelled": True, "order_id": str(order_id)}


@router.post("/cancel-all-pending")
async def cancel_all_pending(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker=Depends(get_broker),
):
    repo = OrderRepository(db)
    orders = await repo.list_pending()
    async with broker as b:
        engine = ExecutionEngine(db, b)
        for order in orders:
            await engine.cancel_order(order)

    db.add(AuditLog(
        action="cancel_all_pending",
        actor=current_user.email,
        payload={"count": len(orders)},
        occurred_at=datetime.now(UTC),
    ))
    return {"cancelled": len(orders)}
