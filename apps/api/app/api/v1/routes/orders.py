"""Orders routes — CRUD, placement, cancellation."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.api.deps import get_broker, get_current_admin, get_current_user
from app.api.schemas import (
    OrderCreate,
    OrderDetail,
    OrderOut,
    PaperExecutionAuditEntry,
    PaperExecutionAuditList,
    PaperExecutionHistoryItem,
    PaperExecutionHistoryList,
    PaperOrderCreate,
)
from app.core.config import settings
from app.db.models import AuditLog, Order, User
from app.db.repositories import OrderRepository
from app.db.session import get_db
from app.execution.engine import ExecutionEngine
from app.execution.paper_engine import (
    PAPER_EXECUTION_ENVIRONMENT,
    PaperExecutionEngine,
    PaperExecutionError,
)
from app.risk.engine import RiskEngine, RiskViolation
from app.services.live_readiness import LiveReadinessService

router = APIRouter(prefix="/orders", tags=["orders"])

_PAPER_HISTORY_DEFAULT_LIMIT = 25
_PAPER_HISTORY_MAX_LIMIT = 100
_SENSITIVE_METADATA = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "api_secret",
)


def _capped_paper_limit(limit: int) -> int:
    return min(limit, _PAPER_HISTORY_MAX_LIMIT)


def _payload(audit: AuditLog | Order, attr: str = "payload") -> dict[str, Any]:
    value = getattr(audit, attr, None)
    return value if isinstance(value, dict) else {}


def _str_payload(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _decimal_payload(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    return Decimal(str(value))


def _safe_audit_metadata(audit: AuditLog) -> dict[str, Any]:
    payload = _payload(audit)
    return {
        key: value
        for key, value in payload.items()
        if not any(sensitive in key.lower() for sensitive in _SENSITIVE_METADATA)
    }


def _audit_summary(action: str, metadata: dict[str, Any]) -> str:
    ticker = _str_payload(metadata, "ticker")
    reason = _str_payload(metadata, "reason")
    if action == "paper_order_created":
        return f"Paper order created for {ticker or 'unknown ticker'}."
    if action == "paper_fill_simulated":
        return f"Paper fill simulated for {ticker or 'unknown ticker'}."
    if action == "paper_position_updated":
        return f"Paper position updated for {ticker or 'unknown ticker'}."
    if action == "paper_signal_rejected":
        return reason or "Paper signal rejected."
    return action.replace("_", " ")


def _paper_order_item(
    order: Order,
    audits: list[AuditLog],
) -> PaperExecutionHistoryItem:
    request_payload = _payload(order, "broker_request")
    latest_audit_at = max((audit.occurred_at for audit in audits), default=None)
    return PaperExecutionHistoryItem(
        id=order.id,
        order_id=order.id,
        created_at=order.created_at,
        updated_at=order.updated_at,
        ticker=order.ticker,
        side=order.side,
        quantity=order.quantity,
        notional=_decimal_payload(request_payload, "notional") or order.cash_used,
        venue=order.venue,
        source=_str_payload(request_payload, "source"),
        strategy=_str_payload(request_payload, "strategy"),
        status=order.status,
        risk_result="allowed",
        fill_price=order.avg_fill_price,
        filled_quantity=order.filled_quantity,
        paper_only=True,
        live_order_sent=False,
        no_broker_order_sent=True,
        rejection_reason=order.error_message,
        audit_count=len(audits),
        latest_audit_at=latest_audit_at,
    )


def _rejected_audit_item(audit: AuditLog) -> PaperExecutionHistoryItem:
    payload = _payload(audit)
    return PaperExecutionHistoryItem(
        id=audit.id,
        order_id=None,
        created_at=audit.occurred_at,
        updated_at=None,
        ticker=_str_payload(payload, "ticker") or "UNKNOWN",
        side=_str_payload(payload, "side"),
        quantity=_decimal_payload(payload, "quantity"),
        notional=_decimal_payload(payload, "notional"),
        venue=_str_payload(payload, "venue"),
        source=_str_payload(payload, "source"),
        strategy=_str_payload(payload, "strategy"),
        status="rejected",
        risk_result="blocked",
        fill_price=None,
        filled_quantity=None,
        paper_only=True,
        live_order_sent=False,
        no_broker_order_sent=True,
        rejection_reason=_str_payload(payload, "reason"),
        audit_count=1,
        latest_audit_at=audit.occurred_at,
    )


def _audit_related_to_order(audit: AuditLog, order_id: uuid.UUID) -> bool:
    order_id_text = str(order_id)
    if audit.entity_id == order_id_text:
        return True
    return _payload(audit).get("order_id") == order_id_text


async def _ensure_live_order_ready(
    *,
    db: AsyncSession,
    current_user: User,
    body: OrderCreate,
) -> None:
    if settings.APP_MODE != "live":
        return

    readiness = await LiveReadinessService(db).evaluate()
    if readiness["ready_for_live"]:
        return

    blockers = readiness.get("blockers") or ["Live readiness checklist is incomplete."]
    reason = f"Live readiness incomplete. Real order blocked before broker submission: {blockers[0]}"
    db.add(
        AuditLog(
            action="live_order_blocked",
            entity_type="order",
            actor=current_user.email,
            payload={
                "ticker": body.ticker.upper(),
                "side": body.side,
                "order_type": body.order_type,
                "quantity": str(body.quantity),
                "reason": reason,
                "blockers": blockers,
                "no_broker_order_sent": True,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()
    raise HTTPException(status_code=403, detail=reason)


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
):
    """
    Place an order with full pre-flight risk checks.
    Order flow: risk checks → intent created → submitted to broker → reconciled.
    """
    repo = OrderRepository(db)

    await _ensure_live_order_ready(db=db, current_user=current_user, body=body)
    broker = await get_broker(current_user=current_user, db=db)

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


@router.post("/paper", response_model=OrderOut, status_code=201)
async def place_paper_order(
    body: PaperOrderCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Create and fill a local paper-only order.

    This route is intentionally separate from broker-backed order placement and
    does not depend on get_broker. It is available only in APP_MODE=mock.
    """
    repo = OrderRepository(db)
    engine = PaperExecutionEngine(db)
    try:
        order = await engine.execute(body, user=current_user)
    except PaperExecutionError as exc:
        # PaperExecutionEngine records rejected/blocked paper attempts before
        # raising. Commit those local audit rows before returning the 4xx so
        # safety blocks remain visible in paper history and operator activity.
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    hydrated = await repo.get_by_id(order.id)
    return hydrated or order


@router.get("/paper", response_model=PaperExecutionHistoryList)
async def list_paper_orders(
    limit: int = Query(_PAPER_HISTORY_DEFAULT_LIMIT, ge=1),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperExecutionHistoryList:
    capped_limit = _capped_paper_limit(limit)
    paper_order_query = select(Order).where(
        Order.is_dry_run == True,  # noqa: E712
        Order.execution_environment == PAPER_EXECUTION_ENVIRONMENT,
    )
    rejected_query = select(AuditLog).where(AuditLog.action == "paper_signal_rejected")

    paper_orders = list(
        (
            await db.execute(
                paper_order_query.order_by(desc(Order.created_at)).limit(capped_limit)
            )
        ).scalars().all()
    )
    rejected_audits = list(
        (
            await db.execute(
                rejected_query.order_by(desc(AuditLog.occurred_at)).limit(capped_limit)
            )
        ).scalars().all()
    )

    total_orders = int(
        (await db.execute(select(func.count()).select_from(paper_order_query.subquery()))).scalar_one()
    )
    total_rejected = int(
        (await db.execute(select(func.count()).select_from(rejected_query.subquery()))).scalar_one()
    )

    order_ids = {str(order.id) for order in paper_orders}
    paper_audits = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action.ilike("paper_%"))
                .order_by(desc(AuditLog.occurred_at))
                .limit(max(capped_limit * 10, 50))
            )
        ).scalars().all()
    )
    audits_by_order: dict[uuid.UUID, list[AuditLog]] = {order.id: [] for order in paper_orders}
    for audit in paper_audits:
        if audit.entity_id in order_ids:
            audits_by_order[uuid.UUID(audit.entity_id)].append(audit)
            continue
        payload_order_id = _payload(audit).get("order_id")
        if isinstance(payload_order_id, str) and payload_order_id in order_ids:
            audits_by_order[uuid.UUID(payload_order_id)].append(audit)

    items: list[PaperExecutionHistoryItem] = [
        _paper_order_item(order, audits_by_order.get(order.id, []))
        for order in paper_orders
    ]
    items.extend(_rejected_audit_item(audit) for audit in rejected_audits)
    items.sort(key=lambda item: item.created_at, reverse=True)

    return PaperExecutionHistoryList(
        items=items[:capped_limit],
        total=total_orders + total_rejected,
        limit=capped_limit,
    )


@router.get("/paper/{order_id}/audit", response_model=PaperExecutionAuditList)
async def get_paper_order_audit(
    order_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperExecutionAuditList:
    order = (
        await db.execute(
            select(Order).where(
                Order.id == order_id,
                Order.is_dry_run == True,  # noqa: E712
                Order.execution_environment == PAPER_EXECUTION_ENVIRONMENT,
            )
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Paper order not found")

    paper_audits = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action.ilike("paper_%"))
                .order_by(desc(AuditLog.occurred_at))
                .limit(200)
            )
        ).scalars().all()
    )
    related = [audit for audit in paper_audits if _audit_related_to_order(audit, order_id)]

    return PaperExecutionAuditList(
        order_id=order_id,
        paper_only=True,
        live_order_sent=False,
        no_broker_order_sent=True,
        items=[
            PaperExecutionAuditEntry(
                id=audit.id,
                occurred_at=audit.occurred_at,
                action=audit.action,
                entity_type=audit.entity_type,
                entity_id=audit.entity_id,
                actor=audit.actor,
                summary=_audit_summary(audit.action, _safe_audit_metadata(audit)),
                metadata=_safe_audit_metadata(audit),
            )
            for audit in related
        ],
    )


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
