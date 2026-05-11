"""
Execution engine.
Handles order intent creation, dedup, broker submission, and reconciliation.
Trading 212 order placement is NOT idempotent — app-level dedup is mandatory.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from app.broker.trading212 import make_sell_quantity
from app.core.config import settings
from app.db.models import Order, OrderEvent
from app.services.execution_quality import (
    apply_order_execution_quality,
    mark_slippage_alerted,
    milliseconds_between,
    should_alert_abnormal_slippage,
)
from app.services.safety_policy import (
    audit_broker_request_attempt,
    audit_safety_decision,
    current_runtime_mode,
    require_order_submission_allowed,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


def _safe_broker_error_reason(exc: Exception) -> str:
    """Keep broker failures auditable without echoing potentially sensitive response text."""
    message = str(exc)
    sensitive_markers = ("secret", "token", "password", "api_key", "api secret", "authorization")
    if not message or any(marker in message.lower() for marker in sensitive_markers):
        return f"Broker request failed with {type(exc).__name__}."
    return message


class ExecutionEngine:
    DUPLICATE_LOOKBACK_SECONDS = 180

    def __init__(self, db: AsyncSession, broker: Any):
        self.db = db
        self.broker = broker

    def _make_client_order_key(
        self,
        ticker: str,
        side: str,
        signal_id: str | None,
        salt: str = "",
    ) -> str:
        """Deterministic client key for dedup. Based on signal + ticker + side."""
        raw = f"{signal_id or 'manual'}:{ticker}:{side}:{salt}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    async def _log_order_event(
        self,
        order_id: uuid.UUID,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = OrderEvent(
            id=uuid.uuid4(),
            order_id=order_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
        self.db.add(event)

    async def create_order_intent(
        self,
        ticker: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        *,
        signal_id: uuid.UUID | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        time_validity: str = "DAY",
        is_dry_run: bool = False,
        available_cash: Decimal | None = None,
        estimated_price: Decimal | None = None,
        venue: str = "t212",
    ) -> Order:
        """
        Create an order intent in the DB before any broker call.
        Checks for duplicates by client_order_key.
        """
        salt = str(uuid.uuid4())[:8]  # Small salt for non-signal orders
        client_key = self._make_client_order_key(
            ticker, side, str(signal_id) if signal_id else None, salt if not signal_id else ""
        )

        # Dedup check
        result = await self.db.execute(
            select(Order).where(Order.client_order_key == client_key)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        duplicate = await self._find_recent_duplicate_intent(
            ticker=ticker,
            side=side,
            order_type=order_type,
            quantity=quantity,
            signal_id=signal_id,
            limit_price=limit_price,
            stop_price=stop_price,
            time_validity=time_validity,
            is_dry_run=is_dry_run,
        )
        if duplicate:
            await self._log_order_event(
                duplicate.id,
                "duplicate_blocked",
                from_status=duplicate.status,
                to_status=duplicate.status,
                payload={
                    "ticker": ticker,
                    "side": side,
                    "order_type": order_type,
                    "quantity": float(quantity),
                },
            )
            return duplicate

        cash_used = None
        if side == "buy" and estimated_price and quantity > 0:
            cash_used = quantity * estimated_price
        expected_fill_price = estimated_price or limit_price or stop_price

        order = Order(
            id=uuid.uuid4(),
            signal_id=signal_id,
            client_order_key=client_key,
            ticker=ticker,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            time_validity=time_validity,
            status="pending_intent",
            is_dry_run=is_dry_run,
            venue=venue,
            execution_environment="dry_run" if is_dry_run else settings.APP_MODE,
            expected_fill_price=expected_fill_price,
            cash_used=cash_used,
            available_cash_at_submission=available_cash,
        )
        self.db.add(order)
        await self.db.flush()

        await self._log_order_event(
            order.id,
            "intent_created",
            to_status="pending_intent",
            payload={"ticker": ticker, "side": side, "quantity": float(quantity), "is_dry_run": is_dry_run},
        )
        return order

    async def _find_recent_duplicate_intent(
        self,
        *,
        ticker: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        signal_id: uuid.UUID | None,
        limit_price: Decimal | None,
        stop_price: Decimal | None,
        time_validity: str,
        is_dry_run: bool,
    ) -> Order | None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.DUPLICATE_LOOKBACK_SECONDS)
        result = await self.db.execute(
            select(Order).where(
                Order.ticker == ticker,
                Order.side == side,
                Order.order_type == order_type,
                Order.time_validity == time_validity,
                Order.is_dry_run == is_dry_run,
                Order.created_at >= cutoff,
                Order.status.in_(("pending_intent", "submitted", "accepted")),
            )
        )
        candidates = result.scalars().all()
        for candidate in candidates:
            if signal_id and candidate.signal_id != signal_id:
                continue
            if signal_id is None and candidate.signal_id is not None:
                continue
            if candidate.quantity != quantity:
                continue
            if candidate.limit_price != limit_price:
                continue
            if candidate.stop_price != stop_price:
                continue
            return candidate
        return None

    async def _maybe_alert_abnormal_slippage(self, order: Order) -> None:
        if not should_alert_abnormal_slippage(order):
            return

        try:
            from app.services.alert_service import alert_abnormal_slippage

            await alert_abnormal_slippage(
                self.db,
                order_id=str(order.id),
                ticker=order.ticker,
                side=order.side,
                expected_price=float(order.expected_fill_price or 0),
                fill_price=float(order.avg_fill_price or 0),
                slippage_pct=float(order.slippage_pct or 0),
                slippage_value=float(order.slippage_value or 0),
            )
            mark_slippage_alerted(order)
        except Exception as exc:
            log.warning(
                "execution.slippage_alert_failed",
                order_id=str(order.id),
                ticker=order.ticker,
                error=str(exc),
            )

    async def submit_order(self, order: Order) -> Order:
        """
        Submit order to broker. Updates order status throughout.
        For dry-run orders, simulates success without contacting broker.
        """
        if order.status != "pending_intent":
            raise ValueError(f"Cannot submit order in status {order.status!r}")

        broker_environment = getattr(self.broker, "environment", None)
        await require_order_submission_allowed(
            self.db,
            order=order,
            broker_environment=broker_environment,
        )

        # Dry-run: simulate fill
        if order.is_dry_run:
            now = datetime.now(UTC)
            order.status = "filled"
            order.filled_quantity = order.quantity
            order.avg_fill_price = order.expected_fill_price or order.limit_price or Decimal("100.00")
            order.submitted_at = now
            order.first_ack_at = now
            order.filled_at = now
            order.broker_latency_ms = 0
            order.fill_latency_ms = 0
            order.reconciliation_latency_ms = 0
            order.broker_response = {"dry_run": True, "simulated": True}
            apply_order_execution_quality(order)
            await self._log_order_event(order.id, "dry_run_fill", from_status="pending_intent", to_status="filled")
            await audit_safety_decision(
                self.db,
                action="order_submitted",
                actor="execution_engine",
                decision="simulated",
                reason="Dry-run order simulated locally. No broker order sent.",
                order=order,
            )
            await self.db.flush()
            return order

        # Real submission
        submitted_at = datetime.now(UTC)
        order.status = "submitted"
        order.submitted_at = submitted_at
        order.execution_environment = order.execution_environment or settings.APP_MODE
        await self._log_order_event(
            order.id,
            "submitted",
            from_status="pending_intent",
            to_status="submitted",
            payload={"submitted_at": submitted_at.isoformat()},
        )
        await self.db.flush()

        try:
            # Build the correct quantity (T212 requires negative for sells)
            submit_qty = order.quantity
            if order.side == "sell":
                submit_qty = make_sell_quantity(order.quantity)

            # Build request payload for audit
            request_payload: dict[str, Any] = {
                "ticker": order.ticker,
                "quantity": float(submit_qty),
                "timeValidity": order.time_validity,
            }
            if order.limit_price:
                request_payload["limitPrice"] = float(order.limit_price)
            if order.stop_price:
                request_payload["stopPrice"] = float(order.stop_price)

            order.broker_request = request_payload
            await audit_broker_request_attempt(
                self.db,
                order=order,
                actor="execution_engine",
                broker_environment=broker_environment,
            )

            # Call the correct endpoint
            if order.order_type == "market":
                response = await self.broker.place_market_order(
                    order.ticker, submit_qty, time_validity=order.time_validity
                )
            elif order.order_type == "limit":
                response = await self.broker.place_limit_order(
                    order.ticker, submit_qty, order.limit_price, time_validity=order.time_validity
                )
            elif order.order_type == "stop":
                response = await self.broker.place_stop_order(
                    order.ticker, submit_qty, order.stop_price, time_validity=order.time_validity
                )
            elif order.order_type == "stop_limit":
                response = await self.broker.place_stop_limit_order(
                    order.ticker, submit_qty, order.stop_price, order.limit_price,
                    time_validity=order.time_validity
                )
            else:
                raise ValueError(f"Unknown order type: {order.order_type}")

            first_ack_at = datetime.now(UTC)
            order.broker_response = response
            order.broker_order_id = str(response.get("id", ""))
            order.first_ack_at = first_ack_at
            order.broker_latency_ms = milliseconds_between(order.submitted_at, first_ack_at)

            # Map broker status
            broker_status = response.get("status", "")
            if broker_status in ("FILLED",):
                order.status = "filled"
                order.filled_quantity = Decimal(str(response.get("filledQuantity", float(abs(order.quantity)))))
                order.avg_fill_price = Decimal(str(response.get("filledPrice", 0) or 0))
                order.filled_at = first_ack_at
                order.fill_latency_ms = milliseconds_between(order.submitted_at, first_ack_at)
                order.reconciliation_latency_ms = order.fill_latency_ms
            elif broker_status in ("CANCELLED",):
                order.status = "cancelled"
                order.cancelled_at = first_ack_at
            elif broker_status in ("REJECTED",):
                order.status = "rejected"
                order.rejected_at = first_ack_at
            elif broker_status in ("WORKING", "PENDING"):
                order.status = "accepted"
            else:
                order.status = "accepted"

            apply_order_execution_quality(order)
            await self._maybe_alert_abnormal_slippage(order)
            if current_runtime_mode() == "demo" and broker_environment == "demo":
                await audit_safety_decision(
                    self.db,
                    action="demo_broker_order_success",
                    actor="execution_engine",
                    decision="allowed",
                    reason="Demo broker accepted the order request.",
                    order=order,
                    metadata={
                        "broker_environment": "demo",
                        "broker_order_id": order.broker_order_id,
                        "no_broker_order_sent": False,
                    },
                )
            await self._log_order_event(
                order.id, "broker_accepted",
                from_status="submitted", to_status=order.status,
                payload={
                    "broker_status": broker_status,
                    "broker_order_id": order.broker_order_id,
                    "broker_latency_ms": order.broker_latency_ms,
                    "execution_quality_score": float(order.execution_quality_score) if order.execution_quality_score is not None else None,
                    "slippage_pct": float(order.slippage_pct) if order.slippage_pct is not None else None,
                }
            )

        except Exception as e:
            # Persist the user-visible error on the order and in the audit trail,
            # and emit a structured log with traceback so operators can debug
            # broker integration failures (connectivity, schema drift, auth).
            order.status = "error"
            order.error_message = _safe_broker_error_reason(e)
            order.rejected_at = datetime.now(UTC)
            apply_order_execution_quality(order)
            log.exception(
                "execution.broker_submit_error",
                order_id=str(order.id),
                ticker=order.ticker,
                side=order.side,
            )
            await self._log_order_event(
                order.id, "broker_error",
                from_status="submitted", to_status="error",
                payload={"error": str(e), "error_type": type(e).__name__}
            )
            await audit_safety_decision(
                self.db,
                action=(
                    "demo_broker_order_failure"
                    if current_runtime_mode() == "demo" and broker_environment == "demo"
                    else "broker_request_failed"
                ),
                actor="execution_engine",
                decision="failed",
                reason=_safe_broker_error_reason(e),
                order=order,
                metadata={
                    "error_type": type(e).__name__,
                    "broker_environment": broker_environment,
                },
            )

        await self.db.flush()
        return order

    async def cancel_order(self, order: Order) -> Order:
        """Cancel a pending order at the broker."""
        if order.broker_order_id and not order.is_dry_run:
            try:
                await self.broker.cancel_order(order.broker_order_id)
            except Exception as e:
                order.error_message = f"Cancel error: {e}"

        old_status = order.status
        now = datetime.now(UTC)
        order.status = "cancelled"
        order.cancelled_at = now
        order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, now)
        apply_order_execution_quality(order)
        await self._log_order_event(
            order.id, "cancelled",
            from_status=old_status, to_status="cancelled",
            payload={"reconciliation_latency_ms": order.reconciliation_latency_ms},
        )
        await self.db.flush()
        return order

    async def reconcile_order(self, order: Order) -> Order:
        """
        Poll broker for latest order status.
        Only reconcile orders in accepted/submitted state.
        NEVER blindly retry on uncertain state.
        """
        if order.status not in ("accepted", "submitted") or not order.broker_order_id:
            return order

        if order.is_dry_run:
            return order

        try:
            response = await self.broker.get_order_by_id(order.broker_order_id)
            broker_status = response.get("status", "")

            if broker_status == "FILLED":
                old_status = order.status
                reconciled_at = datetime.now(UTC)
                order.status = "filled"
                order.filled_quantity = Decimal(str(response.get("filledQuantity", 0)))
                order.avg_fill_price = Decimal(str(response.get("filledPrice", 0) or 0))
                order.broker_response = response
                order.filled_at = order.filled_at or reconciled_at
                order.fill_latency_ms = milliseconds_between(order.submitted_at, order.filled_at)
                order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, reconciled_at)
                order.last_reconciled_at = reconciled_at
                apply_order_execution_quality(order)
                await self._maybe_alert_abnormal_slippage(order)
                await self._log_order_event(
                    order.id, "reconciled_fill",
                    from_status=old_status, to_status="filled",
                    payload={
                        "broker_status": broker_status,
                        "fill_latency_ms": order.fill_latency_ms,
                        "reconciliation_latency_ms": order.reconciliation_latency_ms,
                        "execution_quality_score": float(order.execution_quality_score) if order.execution_quality_score is not None else None,
                        "slippage_pct": float(order.slippage_pct) if order.slippage_pct is not None else None,
                    },
                )
            elif broker_status in ("CANCELLED", "REJECTED"):
                old_status = order.status
                reconciled_at = datetime.now(UTC)
                order.status = broker_status.lower()
                if order.status == "cancelled":
                    order.cancelled_at = order.cancelled_at or reconciled_at
                else:
                    order.rejected_at = order.rejected_at or reconciled_at
                order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, reconciled_at)
                order.last_reconciled_at = reconciled_at
                order.broker_response = response
                apply_order_execution_quality(order)
                await self._log_order_event(
                    order.id, "reconciled_status",
                    from_status=old_status, to_status=order.status,
                    payload={
                        "broker_status": broker_status,
                        "reconciliation_latency_ms": order.reconciliation_latency_ms,
                        "execution_quality_score": float(order.execution_quality_score) if order.execution_quality_score is not None else None,
                    }
                )
            else:
                order.last_reconciled_at = datetime.now(UTC)

        except Exception as e:
            # Deliberately do not mutate order status on a reconciliation error —
            # better to keep our in-flight belief than flip to a wrong terminal
            # state on a transient broker hiccup. But we MUST log: silent pass
            # used to hide real drift (e.g. broker_order_id unknown upstream).
            log.warning(
                "execution.reconcile_error",
                order_id=str(order.id),
                broker_order_id=order.broker_order_id,
                error=str(e),
                error_type=type(e).__name__,
            )

        await self.db.flush()
        return order
