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
from app.db.models import Order, OrderEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


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

    async def submit_order(self, order: Order) -> Order:
        """
        Submit order to broker. Updates order status throughout.
        For dry-run orders, simulates success without contacting broker.
        """
        if order.status != "pending_intent":
            raise ValueError(f"Cannot submit order in status {order.status!r}")

        # Dry-run: simulate fill
        if order.is_dry_run:
            order.status = "filled"
            order.filled_quantity = order.quantity
            order.avg_fill_price = order.limit_price or Decimal("100.00")
            order.broker_response = {"dry_run": True, "simulated": True}
            await self._log_order_event(order.id, "dry_run_fill", from_status="pending_intent", to_status="filled")
            await self.db.flush()
            return order

        # Real submission
        order.status = "submitted"
        await self._log_order_event(order.id, "submitted", from_status="pending_intent", to_status="submitted")
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

            order.broker_response = response
            order.broker_order_id = str(response.get("id", ""))

            # Map broker status
            broker_status = response.get("status", "")
            if broker_status in ("FILLED",):
                order.status = "filled"
                order.filled_quantity = Decimal(str(response.get("filledQuantity", float(abs(order.quantity)))))
                order.avg_fill_price = Decimal(str(response.get("filledPrice", 0) or 0))
            elif broker_status in ("WORKING", "PENDING"):
                order.status = "accepted"
            else:
                order.status = "accepted"

            await self._log_order_event(
                order.id, "broker_accepted",
                from_status="submitted", to_status=order.status,
                payload={"broker_status": broker_status, "broker_order_id": order.broker_order_id}
            )

        except Exception as e:
            # Persist the user-visible error on the order and in the audit trail,
            # and emit a structured log with traceback so operators can debug
            # broker integration failures (connectivity, schema drift, auth).
            order.status = "error"
            order.error_message = str(e)
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
        order.status = "cancelled"
        await self._log_order_event(
            order.id, "cancelled",
            from_status=old_status, to_status="cancelled",
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
                order.status = "filled"
                order.filled_quantity = Decimal(str(response.get("filledQuantity", 0)))
                order.avg_fill_price = Decimal(str(response.get("filledPrice", 0) or 0))
                order.broker_response = response
                await self._log_order_event(
                    order.id, "reconciled_fill",
                    from_status=old_status, to_status="filled",
                )
            elif broker_status in ("CANCELLED", "REJECTED"):
                old_status = order.status
                order.status = broker_status.lower()
                await self._log_order_event(
                    order.id, "reconciled_status",
                    from_status=old_status, to_status=order.status,
                    payload={"broker_status": broker_status}
                )

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
