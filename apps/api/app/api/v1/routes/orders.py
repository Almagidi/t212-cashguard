"""Orders routes — CRUD, placement, cancellation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.api.deps import get_broker, get_current_admin, get_current_user
from app.api.schemas import (
    IdentifiedPaperOrderOut,
    OrderCreate,
    OrderDetail,
    OrderOut,
    PaperExecutionAuditEntry,
    PaperExecutionAuditList,
    PaperExecutionHistoryItem,
    PaperExecutionHistoryList,
    PaperOperationReceiptView,
    PaperOrderCreate,
)
from app.api.v1.routes._broker_errors import broker_http_exception
from app.api.v1.routes.account import _normalise_account_summary
from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order, User
from app.db.repositories import OrderRepository
from app.db.session import get_db
from app.execution.engine import (
    ExecutionEngine,
    OrderCancellationFailed,
    OrderCancellationUnconfirmed,
    plain_amount,
)
from app.execution.paper_engine import (
    PAPER_EXECUTION_ENVIRONMENT,
    PaperExecutionEngine,
    PaperExecutionError,
)
from app.execution.paper_operations import PaperOperationService
from app.execution.state_machine import InvalidOrderTransition, require_already_finished
from app.risk.engine import RiskEngine, RiskViolation
from app.risk.realized_pnl import realized_pnl_today
from app.services.cash_only import sellable_quantity
from app.services.live_readiness import LiveReadinessService
from app.services.safety_policy import SafetyPolicyViolation, audit_safety_decision

router = APIRouter(prefix="/orders", tags=["orders"])
log = structlog.get_logger()

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
    reason = (
        f"Live readiness incomplete. Real order blocked before broker submission: {blockers[0]}"
    )
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


async def _ensure_demo_order_enabled(
    *,
    db: AsyncSession,
    current_user: User,
    body: OrderCreate,
) -> None:
    if settings.APP_MODE != "demo":
        return
    if settings.T212_DEMO_ORDER_ENABLED:
        return

    reason = (
        "Trading 212 demo order placement is disabled by default. "
        "Set T212_DEMO_ORDER_ENABLED=true only for an explicit controlled demo-order test."
    )
    db.add(
        AuditLog(
            action="demo_order_blocked_by_feature_gate",
            entity_type="order",
            actor=current_user.email,
            payload={
                "ticker": body.ticker.upper(),
                "side": body.side,
                "order_type": body.order_type,
                "quantity": str(body.quantity),
                "reason": reason,
                "mode": settings.APP_MODE,
                "broker_environment": settings.T212_ENVIRONMENT,
                "feature_gate": "T212_DEMO_ORDER_ENABLED",
                "feature_gate_enabled": False,
                "no_broker_order_sent": True,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    raise HTTPException(status_code=403, detail=reason)


async def _ensure_manual_order_not_halted(
    *,
    db: AsyncSession,
    current_user: User,
    body: OrderCreate,
) -> None:
    app_settings = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one_or_none()
    if app_settings is None or not app_settings.kill_switch_active:
        return

    reason = "Kill switch is active. Manual order placement is blocked before broker reads."
    await audit_safety_decision(
        db,
        action="order_blocked_by_kill_switch",
        actor=current_user.email,
        decision="blocked",
        reason=reason,
        metadata={
            "ticker": body.ticker.upper(),
            "side": body.side,
            "order_type": body.order_type,
            "quantity": str(body.quantity),
            "source": "manual_order_route",
            "decision_code": "kill_switch_block",
        },
    )
    await db.commit()
    raise HTTPException(status_code=403, detail=reason)


# Order types whose price does not cap what a buy can cost (N-02).
_UNCAPPED_ORDER_TYPES = frozenset({"market", "stop"})
# Mock mode only (nothing reaches a broker): the placeholder a market buy was always costed at.
_MOCK_MARKET_PRICE = Decimal("100")


def _price_estimate(positions: object, body: OrderCreate) -> Decimal:
    """The price the checks cost an order at, by its type.

    A limit or stop-limit order: its limit. A stop order: its stop (a sell, outside mock
    mode). A market sell: the broker's current price of the held instrument, or 0 when none
    is usable (a sell spends no cash, so nothing is costed by it). A mock market buy keeps
    its placeholder; a real market or stop buy was refused before this is reached.
    """
    if body.order_type in ("limit", "stop_limit") and body.limit_price is not None:
        return body.limit_price
    if body.order_type == "stop" and body.stop_price is not None:
        return body.stop_price
    if body.side == "buy":
        return _MOCK_MARKET_PRICE
    if isinstance(positions, list):
        for position in positions:
            if not isinstance(position, dict):
                continue
            if str(position.get("ticker", "")).strip().upper() != body.ticker:
                continue
            for key in ("currentPrice", "averagePrice"):
                try:
                    price = Decimal(str(position.get(key)))
                except ArithmeticError:
                    continue
                if price.is_finite() and price > 0:
                    return price
    return Decimal("0")


@router.get("", response_model=list[OrderOut])
async def list_orders(
    status: str | None = Query(None),
    ticker: str | None = Query(None),
    is_dry_run: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, description="Number of records to skip (cursor-style pagination)"),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    repo = OrderRepository(db)
    return await repo.list(
        status=status, ticker=ticker, is_dry_run=is_dry_run, limit=limit, offset=offset
    )


@router.post("", response_model=OrderOut, status_code=201)
async def place_order(
    body: OrderCreate,
    response: Response,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Place an order with full pre-flight risk checks.
    Order flow: risk checks → intent created → submitted to broker → reconciled.
    """
    repo = OrderRepository(db)

    await _ensure_manual_order_not_halted(db=db, current_user=current_user, body=body)
    await _ensure_live_order_ready(db=db, current_user=current_user, body=body)
    await _ensure_demo_order_enabled(db=db, current_user=current_user, body=body)

    if (
        body.side == "buy"
        and body.order_type in _UNCAPPED_ORDER_TYPES
        and (settings.APP_MODE != "mock")
    ):
        # N-02: a market buy has no price, and a stop buy's stop is a trigger, not a cap: it
        # fills at the market once triggered. The cash and risk limits need the most a buy
        # can cost, which only a limit states.
        raise HTTPException(
            status_code=422,
            detail=(
                f"A {body.order_type} buy has no price that caps its cost, so it cannot be "
                "checked against cash and risk limits. Place a limit or stop-limit order "
                "instead: its limit price caps the cost."
            ),
        )
    broker = await get_broker(current_user=current_user, db=db)

    # Fetch broker account state for risk calculations.
    # Broker-side rate limits/auth/API errors must return structured HTTP errors,
    # not an unhandled 500, and this happens before any broker order submission.
    # Cash-only: outside mock mode a sell must be covered by a position the broker reports.
    # In mock mode nothing reaches a broker; this route then uses the mock adapter, which
    # does not check a sell against holdings.
    # The broker's positions are also what counts as open positions for the risk limits.
    needs_holding_check = body.side == "sell" and settings.APP_MODE != "mock"
    positions: object = None
    cash_read_at = datetime.now(UTC)
    try:
        async with broker as b:
            summary = await b.get_account_summary()
            positions = await b.get_positions()
    except (T212RateLimitError, T212AuthError, T212APIError) as exc:
        raise broker_http_exception(exc) from exc

    if needs_holding_check:
        sellable = sellable_quantity(positions, body.ticker)
        if body.quantity > sellable:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Sell of {body.quantity} {body.ticker.upper()} refused: {sellable} is held "
                    "and free to sell. Short selling is not supported; CashGuard is cash-only."
                ),
            )

    normalised_summary = _normalise_account_summary(summary)
    available_cash = Decimal(str(normalised_summary["free_funds"]))
    account_value = Decimal(str(normalised_summary["total_value"]))
    estimated_price = _price_estimate(positions, body)

    # N-03: open positions are the broker's, not every order that ever filled.
    if not isinstance(positions, list):
        raise HTTPException(
            status_code=502, detail="The broker's positions could not be read; order refused."
        )
    open_count = len(positions)
    try:
        pnl_today = await realized_pnl_today(db)
    except Exception as exc:
        log.error("orders.realized_pnl_unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Today's realised result could not be read; order refused."
        ) from None

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
            realized_pnl_today=pnl_today,
            current_open_positions=open_count,
            signal_id=body.signal_id,
            # A sell of a held position (cash-only) is an exit: entry limits do not hold it back.
            is_exit=body.side == "sell",
        )
    except RiskViolation as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc

    # --- Execute ---
    async with broker as b:
        engine = ExecutionEngine(db, b)
        try:
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
                cash_read_at=cash_read_at,
            )
        except RiskViolation as exc:
            # The account's other open buy orders already claim this cash.
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        if order.status != "pending_intent":
            # An identical order already exists (for example one whose submission outcome
            # is still unknown). It is reported, never sent a second time.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An identical order already exists with status {order.status}; "
                    "it was not submitted again."
                ),
            )
        try:
            order = await engine.submit_order(order)
        except SafetyPolicyViolation as exc:
            raise HTTPException(status_code=403, detail=exc.reason) from exc

    db.add(
        AuditLog(
            action="order_placed",
            entity_type="order",
            entity_id=str(order.id),
            actor=current_user.email,
            payload={"ticker": body.ticker, "side": body.side, "qty": str(body.quantity)},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()
    if order.status == "submission_unknown":
        # Dispatched, outcome unknown: accepted for processing, not confirmed as placed.
        response.status_code = 202
    hydrated = await repo.get_by_id(order.id)
    return hydrated or order


@router.post("/paper", response_model=IdentifiedPaperOrderOut | OrderOut, status_code=201)
async def place_paper_order(
    body: PaperOrderCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    expected_owner: uuid.UUID | None = Header(default=None, alias="X-Paper-Expected-Owner"),
) -> Any:
    """
    Create and fill a local paper-only order.

    This route is intentionally separate from broker-backed order placement and
    does not depend on get_broker. It is available only in APP_MODE=mock.
    """
    if body.operation_id is not None:
        # Non-authoritative precondition: never use this header as an owner selector.
        if expected_owner != current_user.id:
            return JSONResponse(
                {
                    "detail": "Authenticated paper owner changed or was not confirmed. No command was admitted; retain the original operation identity."
                },
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        result = await PaperOperationService(db).submit(body, user=current_user)
        return JSONResponse(
            result.payload, status_code=result.status_code, headers={"Cache-Control": "no-store"}
        )
    # Compatibility only: no operation ID means a new request, not safe replay.
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


@router.get("/paper/operations/{operation_id}", response_model=PaperOperationReceiptView)
async def get_paper_operation(
    operation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    expected_owner: uuid.UUID | None = Header(default=None, alias="X-Paper-Expected-Owner"),
) -> Any:
    if expected_owner != current_user.id:
        return JSONResponse(
            {
                "detail": "Authenticated paper owner changed or was not confirmed. No receipt disclosed."
            },
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    result = await PaperOperationService(db).lookup(current_user.id, operation_id)
    return JSONResponse(
        result.payload, status_code=result.status_code, headers={"Cache-Control": "no-store"}
    )


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
        (await db.execute(paper_order_query.order_by(desc(Order.created_at)).limit(capped_limit)))
        .scalars()
        .all()
    )
    rejected_audits = list(
        (await db.execute(rejected_query.order_by(desc(AuditLog.occurred_at)).limit(capped_limit)))
        .scalars()
        .all()
    )

    total_orders = int(
        (
            await db.execute(select(func.count()).select_from(paper_order_query.subquery()))
        ).scalar_one()
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
        )
        .scalars()
        .all()
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
        _paper_order_item(order, audits_by_order.get(order.id, [])) for order in paper_orders
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
        )
        .scalars()
        .all()
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
) -> Any:
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
    broker: Any = Depends(get_broker),
) -> Any:
    repo = OrderRepository(db)
    order = await repo.get_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("filled", "cancelled", "rejected", "error"):
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel order in status: {order.status}"
        )
    try:
        async with broker as b:
            engine = ExecutionEngine(db, b)
            order = await engine.cancel_order(order)
    except OrderCancellationUnconfirmed as exc:
        # The broker took the request; it has not said yet what became of the order. The
        # order stays open here, its cash reserved, until a final record of it is read.
        db.add(
            AuditLog(
                action="order_cancel_unconfirmed",
                entity_type="order",
                entity_id=str(order_id),
                actor=current_user.email,
                payload={
                    "status": exc.order.status,
                    "reason": exc.reason,
                    "requires_reconciliation": True,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        return JSONResponse(
            status_code=202,
            content={
                "cancelled": False,
                "awaiting_confirmation": True,
                "order_id": str(order_id),
                "status": exc.order.status,
                "reason": exc.reason,
                "requires_reconciliation": True,
            },
        )
    except OrderCancellationFailed as exc:
        db.add(
            AuditLog(
                action="order_cancel_failed",
                entity_type="order",
                entity_id=str(order_id),
                actor=current_user.email,
                payload={"status": exc.order.status, "requires_reconciliation": True},
                occurred_at=datetime.now(UTC),
            )
        )
        return JSONResponse(
            status_code=502,
            content={
                "cancelled": False,
                "order_id": str(order_id),
                "status": exc.order.status,
                "requires_reconciliation": True,
            },
        )
    except InvalidOrderTransition:
        # It ended (a fill recorded by reconciliation, say) after it was read above.
        require_already_finished(order)
        return _cancelled_too_late(db, order, current_user)

    if order.status != "cancelled":
        # The broker's own record shows the order ended another way (it filled, or was
        # rejected) before the cancellation could take effect.
        return _cancelled_too_late(db, order, current_user)

    filled = plain_amount(order.filled_quantity)
    db.add(
        AuditLog(
            action="order_cancelled",
            entity_type="order",
            entity_id=str(order_id),
            actor=current_user.email,
            payload={"filled_quantity": filled} if order.filled_quantity else None,
            occurred_at=datetime.now(UTC),
        )
    )
    return {"cancelled": True, "order_id": str(order_id), "filled_quantity": filled}


def _cancelled_too_late(db: AsyncSession, order: Order, current_user: User) -> JSONResponse:
    filled = plain_amount(order.filled_quantity)
    db.add(
        AuditLog(
            action="order_cancel_too_late",
            entity_type="order",
            entity_id=str(order.id),
            actor=current_user.email,
            payload={"status": order.status, "filled_quantity": filled},
            occurred_at=datetime.now(UTC),
        )
    )
    return JSONResponse(
        status_code=409,
        content={
            "cancelled": False,
            "order_id": str(order.id),
            "status": order.status,
            "filled_quantity": filled,
        },
    )


@router.post("/cancel-all-pending")
async def cancel_all_pending(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker: Any = Depends(get_broker),
) -> Any:
    repo = OrderRepository(db)
    orders = await repo.list_pending()
    # Every cancel request goes out before any order is read back (see cancel_many). An
    # order that ended between being listed and being reached, or whose final record shows
    # it filled first, is reported as already finished; it does not stop the rest.
    async with broker as b:
        batch = await ExecutionEngine(db, b).cancel_many(list(orders))
    cancelled = len(batch.cancelled)
    failed = len(batch.failed)
    already_finished = len(batch.finished)
    failed_orders = batch.failed
    finished_orders = batch.finished
    unconfirmed_orders = [order for order, _reason in batch.unconfirmed]
    partly_filled_orders = batch.partly_filled

    counts: dict[str, int] = {"cancelled": cancelled, "failed": failed}
    if already_finished:
        counts["already_finished"] = already_finished
    if unconfirmed_orders:
        counts["awaiting_confirmation"] = len(unconfirmed_orders)
    payload: dict[str, Any] = {f"{name}_count": count for name, count in counts.items()}
    if failed_orders:
        payload["failed_orders"] = [
            {"order_id": str(order.id), "ticker": order.ticker} for order in failed_orders
        ]
    if finished_orders:
        # A fill among these is exposure the cancel did not prevent.
        payload["already_finished_orders"] = [
            {
                "order_id": str(order.id),
                "ticker": order.ticker,
                "status": order.status,
                "filled_quantity": plain_amount(order.filled_quantity),
            }
            for order in finished_orders
        ]
    if unconfirmed_orders:
        payload["awaiting_confirmation_orders"] = [
            {
                "order_id": str(order.id),
                "ticker": order.ticker,
                "status": order.status,
                "reason": reason,
            }
            for order, reason in batch.unconfirmed
        ]
    if partly_filled_orders:
        payload["cancelled_after_partial_fill"] = [
            {
                "order_id": str(order.id),
                "ticker": order.ticker,
                "filled_quantity": plain_amount(order.filled_quantity),
                "quantity": plain_amount(order.quantity),
            }
            for order in partly_filled_orders
        ]
    if failed_orders or finished_orders or unconfirmed_orders:
        log.warning(
            "orders.cancel_all_needs_attention",
            failed=[str(order.id) for order in failed_orders],
            already_finished={str(order.id): order.status for order in finished_orders},
            awaiting_confirmation=[str(order.id) for order in unconfirmed_orders],
        )
    db.add(
        AuditLog(
            action="cancel_all_pending",
            actor=current_user.email,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
    )
    # The operator is told which orders need a look, not only how many: a fill before or
    # instead of the cancellation is exposure, and an unconfirmed order is still open.
    answer: dict[str, Any] = dict(counts)
    for name in (
        "already_finished_orders",
        "cancelled_after_partial_fill",
        "awaiting_confirmation_orders",
    ):
        if name in payload:
            answer[name] = payload[name]
    return answer
