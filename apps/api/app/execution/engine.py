"""
Execution engine.
Handles order intent creation, dedup, broker submission, and reconciliation.
Trading 212 order placement is NOT idempotent — app-level dedup is mandatory.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.broker.protocols import (
    BrokerSubmissionError,
    BrokerSubmissionNotTransmitted,
    BrokerSubmissionRejected,
)
from app.broker.trading212 import make_sell_quantity
from app.broker.trading212_mappers import map_trading212_history_order_to_snapshot
from app.core.config import settings
from app.db.models import Order, OrderEvent, OrderSubmissionAttempt, RiskEvent
from app.execution.state_machine import (
    ACTIVE_ORDER_STATUSES,
    can_transition_order_status,
    transition_order_status_with_evidence,
)
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

    from app.broker.snapshots import BrokerOrderSnapshot

log = structlog.get_logger()


def _safe_broker_error_reason(exc: Exception) -> str:
    """Keep broker failures auditable without echoing potentially sensitive response text."""
    return f"Broker request failed with {type(exc).__name__}."


SUBMISSION_UNKNOWN_MESSAGE = (
    "Broker submission outcome is unknown. An order may exist at the broker; "
    "it will not be resubmitted and needs reconciliation."
)


def _request_fingerprint(order: Order, request_payload: dict[str, Any]) -> str:
    """Stable digest of what was asked of the broker, for matching evidence later."""
    canonical = json.dumps(
        {
            "client_order_key": order.client_order_key,
            "order_type": order.order_type,
            "side": order.side,
            "request": request_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class OrderCancellationFailed(RuntimeError):
    """Raised after a failed broker cancellation is staged for persistence."""

    def __init__(self, order: Order):
        self.order = order
        super().__init__("Broker cancellation failed; reconciliation is required.")


class ExecutionEngine:
    DUPLICATE_LOOKBACK_SECONDS = 180
    # A broker record can match an unknown submission only if it was created in this window
    # around the committed dispatch time (allowing for clock skew and broker queueing).
    UNKNOWN_SUBMISSION_MATCH_BEFORE = timedelta(minutes=2)
    UNKNOWN_SUBMISSION_MATCH_AFTER = timedelta(minutes=15)
    UNKNOWN_SUBMISSION_HISTORY_LIMIT = 50
    # Reconciliation leaves a dispatch alone until the original request can no longer be in
    # flight (the broker client times out after 30 seconds).
    UNKNOWN_SUBMISSION_MIN_AGE = timedelta(seconds=90)

    def __init__(self, db: AsyncSession, broker: Any):
        self.db = db
        self.broker = broker

    def _make_client_order_key(
        self,
        ticker: str,
        side: str,
        signal_id: str | None,
        salt: str = "",
        stable_operation_identity: str | None = None,
    ) -> str:
        """Deterministic client key for dedup. Based on signal + ticker + side."""
        raw = (
            f"operation:{stable_operation_identity}"
            if stable_operation_identity is not None
            else f"{signal_id or 'manual'}:{ticker}:{side}:{salt}"
        )
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
        stable_operation_identity: str | None = None,
    ) -> Order:
        """
        Create an order intent in the DB before any broker call.
        Checks for duplicates by client_order_key.
        """
        salt = str(uuid.uuid4())[:8]  # Small salt for non-signal orders
        client_key = self._make_client_order_key(
            ticker,
            side,
            str(signal_id) if signal_id else None,
            salt if not signal_id else "",
            stable_operation_identity,
        )

        # Dedup check
        result = await self.db.execute(select(Order).where(Order.client_order_key == client_key))
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        duplicate = None
        if stable_operation_identity is None:
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

        broker_account_scope = inspect.getattr_static(self.broker, "account_scope", None)
        if not isinstance(broker_account_scope, str) or not broker_account_scope.strip():
            broker_account_scope = None

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
            broker_account_scope=broker_account_scope,
            expected_fill_price=expected_fill_price,
            cash_used=cash_used,
            available_cash_at_submission=available_cash,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(order)
                await self.db.flush()
        except IntegrityError:
            result = await self.db.execute(
                select(Order).where(Order.client_order_key == client_key)
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                raise
            return existing

        await self._log_order_event(
            order.id,
            "intent_created",
            to_status="pending_intent",
            payload={
                "ticker": ticker,
                "side": side,
                "quantity": float(quantity),
                "is_dry_run": is_dry_run,
            },
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
                or_(
                    and_(Order.created_at >= cutoff, Order.status.in_(ACTIVE_ORDER_STATUSES)),
                    # An order that may exist at the broker blocks an identical intent
                    # until it is resolved, however long that takes.
                    Order.status == "submission_unknown",
                ),
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
            transition_order_status_with_evidence(
                self.db,
                order,
                "submitted",
                event_type="dry_run_submitted",
                reason="dry-run simulated submission",
                actor="execution_engine",
                correlation_id=order.client_order_key,
            )
            transition_order_status_with_evidence(
                self.db,
                order,
                "filled",
                event_type="dry_run_fill",
                reason="dry-run simulated fill",
                actor="execution_engine",
                correlation_id=order.client_order_key,
            )
            order.filled_quantity = order.quantity
            order.avg_fill_price = (
                order.expected_fill_price or order.limit_price or Decimal("100.00")
            )
            order.submitted_at = now
            order.first_ack_at = now
            order.filled_at = now
            order.broker_latency_ms = 0
            order.fill_latency_ms = 0
            order.reconciliation_latency_ms = 0
            order.broker_response = {"dry_run": True, "simulated": True}
            apply_order_execution_quality(order)
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
        try:
            submit_qty, request_payload = self._build_broker_request(order)
        except ValueError as exc:
            # Nothing has been recorded as dispatched and nothing was sent.
            await self._record_failed_before_dispatch(order, exc, broker_environment)
            return order

        # Durability boundary. The intent, the unknown-outcome state and the attempt evidence
        # are committed before the request is sent, so a crash at any later point leaves a
        # record that an order may exist. If this commit fails, the broker is never called.
        dispatched_at = datetime.now(UTC)
        attempt = OrderSubmissionAttempt(
            id=uuid.uuid4(),
            order_id=order.id,
            request_fingerprint=_request_fingerprint(order, request_payload),
            execution_environment=order.execution_environment or settings.APP_MODE,
            broker_environment=broker_environment,
            broker_account_scope=order.broker_account_scope,
            expected_cost=order.cash_used,
            dispatch_committed_at=dispatched_at,
        )
        transition_order_status_with_evidence(
            self.db,
            order,
            "submission_unknown",
            event_type="dispatch_committed",
            reason="broker request recorded before transmission",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload={
                "submitted_at": dispatched_at.isoformat(),
                "attempt_id": str(attempt.id),
                "request_fingerprint": attempt.request_fingerprint,
            },
        )
        order.submitted_at = dispatched_at
        order.execution_environment = attempt.execution_environment
        order.broker_request = request_payload
        self.db.add(attempt)
        await audit_broker_request_attempt(
            self.db,
            order=order,
            actor="execution_engine",
            broker_environment=broker_environment,
        )
        await self.db.commit()

        # Network I/O happens outside any transaction; no row lock is held across it.
        try:
            response = await self._send_to_broker(order, submit_qty)
        except BrokerSubmissionNotTransmitted as exc:
            await self._record_not_transmitted(order, attempt, exc, broker_environment)
        except BrokerSubmissionRejected as exc:
            await self._record_broker_refusal(order, attempt, exc, broker_environment)
        except Exception as exc:
            # Ambiguous by classification, or unclassified: an order may exist.
            await self._record_unknown_outcome(order, attempt, exc, broker_environment)
        else:
            await self._record_acknowledgement(order, attempt, response, broker_environment)
        return order

    def _build_broker_request(self, order: Order) -> tuple[Decimal, dict[str, Any]]:
        """Validate the order and build the audited request payload without any I/O."""
        if order.order_type not in {"market", "limit", "stop", "stop_limit"}:
            raise ValueError(f"Unknown order type: {order.order_type}")
        if order.order_type in {"limit", "stop_limit"} and order.limit_price is None:
            raise ValueError(f"A {order.order_type} order needs a limit price")
        if order.order_type in {"stop", "stop_limit"} and order.stop_price is None:
            raise ValueError(f"A {order.order_type} order needs a stop price")
        # T212 requires a negative quantity for sells.
        submit_qty = make_sell_quantity(order.quantity) if order.side == "sell" else order.quantity
        request_payload: dict[str, Any] = {
            "ticker": order.ticker,
            "quantity": float(submit_qty),
            "timeValidity": order.time_validity,
        }
        if order.limit_price:
            request_payload["limitPrice"] = float(order.limit_price)
        if order.stop_price:
            request_payload["stopPrice"] = float(order.stop_price)
        return submit_qty, request_payload

    async def _send_to_broker(self, order: Order, submit_qty: Decimal) -> Any:
        if order.order_type == "market":
            return await self.broker.place_market_order(
                order.ticker, submit_qty, time_validity=order.time_validity
            )
        if order.order_type == "limit":
            return await self.broker.place_limit_order(
                order.ticker, submit_qty, order.limit_price, time_validity=order.time_validity
            )
        if order.order_type == "stop":
            return await self.broker.place_stop_order(
                order.ticker, submit_qty, order.stop_price, time_validity=order.time_validity
            )
        return await self.broker.place_stop_limit_order(
            order.ticker,
            submit_qty,
            order.stop_price,
            order.limit_price,
            time_validity=order.time_validity,
        )

    @staticmethod
    def _failure_evidence(exc: BaseException) -> dict[str, Any]:
        """Redacted description of a failure: type and HTTP status only."""
        if isinstance(exc, BrokerSubmissionError):
            return {"error_type": exc.error_type, "http_status": exc.http_status}
        return {"error_type": type(exc).__name__, "http_status": None}

    async def _audit_broker_failure(
        self,
        order: Order,
        *,
        action: str,
        decision: str,
        reason: str,
        evidence: dict[str, Any],
        broker_environment: str | None,
        order_may_exist: bool,
    ) -> None:
        demo = current_runtime_mode() == "demo" and broker_environment == "demo"
        await audit_safety_decision(
            self.db,
            action="demo_broker_order_failure" if demo else action,
            actor="execution_engine",
            decision=decision,
            reason=reason,
            order=order,
            metadata={
                **evidence,
                "broker_environment": broker_environment,
                # audit_safety_decision defaults this to True for any decision but "allowed".
                "no_broker_order_sent": not order_may_exist,
            },
        )

    async def _record_failed_before_dispatch(
        self, order: Order, exc: Exception, broker_environment: str | None
    ) -> None:
        evidence = self._failure_evidence(exc)
        transition_order_status_with_evidence(
            self.db,
            order,
            "error",
            event_type="broker_error",
            reason="order could not be prepared; nothing was sent",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload=evidence,
        )
        order.error_message = _safe_broker_error_reason(exc)
        order.rejected_at = datetime.now(UTC)
        apply_order_execution_quality(order)
        log.error(
            "execution.broker_request_invalid",
            order_id=str(order.id),
            ticker=order.ticker,
            side=order.side,
            error_type=evidence["error_type"],
        )
        await self._audit_broker_failure(
            order,
            action="broker_request_failed",
            decision="failed",
            reason=_safe_broker_error_reason(exc),
            evidence=evidence,
            broker_environment=broker_environment,
            order_may_exist=False,
        )
        await self.db.flush()

    async def _resolved_elsewhere(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt,
        outcome: str,
        evidence: dict[str, Any],
    ) -> bool:
        """Re-read the order under a row lock before recording the request's outcome.

        Reconciliation may have resolved it while the request was in flight. In that case
        the outcome is kept as evidence only and the resolved state is left untouched.
        """
        current_status = (
            await self.db.execute(
                select(Order.status).where(Order.id == order.id).with_for_update()
            )
        ).scalar_one()
        if current_status == "submission_unknown":
            return False
        # Load what the other writer committed before adding evidence to it.
        await self.db.refresh(order)
        late_id = evidence.get("broker_order_id")
        if late_id and order.broker_order_id and late_id != order.broker_order_id:
            # Reconciliation adopted one broker order and the broker has now named another:
            # two orders may exist. This needs a person, so it is raised as a risk event.
            self.db.add(
                RiskEvent(
                    id=uuid.uuid4(),
                    event_type="submission_identity_conflict",
                    ticker=order.ticker,
                    signal_id=order.signal_id,
                    order_id=order.id,
                    message=(
                        "A late broker acknowledgement names a different order than the one "
                        "reconciliation adopted. Check the broker for two orders."
                    ),
                    payload={
                        "severity": "critical",
                        "adopted_broker_order_id": order.broker_order_id,
                        "acknowledged_broker_order_id": late_id,
                        "attempt_id": str(attempt.id),
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
        self._close_attempt(attempt, outcome, evidence)
        await self._log_order_event(
            order.id,
            "late_submission_outcome",
            from_status=order.status,
            to_status=order.status,
            payload={**evidence, "attempt_id": str(attempt.id), "attempt_outcome": outcome},
        )
        await self.db.commit()
        return True

    def _close_attempt(
        self, attempt: OrderSubmissionAttempt, outcome: str, evidence: dict[str, Any]
    ) -> None:
        if attempt.outcome is not None:
            raise RuntimeError("A submission attempt outcome is recorded once and never rewritten")
        attempt.outcome = outcome
        attempt.outcome_recorded_at = datetime.now(UTC)
        attempt.outcome_evidence = evidence

    async def _record_not_transmitted(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt,
        exc: BrokerSubmissionNotTransmitted,
        broker_environment: str | None,
    ) -> None:
        evidence = self._failure_evidence(exc)
        if await self._resolved_elsewhere(order, attempt, "not_transmitted", evidence):
            return
        self._close_attempt(attempt, "not_transmitted", evidence)
        transition_order_status_with_evidence(
            self.db,
            order,
            "error",
            event_type="broker_error",
            reason="broker request was not transmitted",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload={**evidence, "transmitted": False, "attempt_id": str(attempt.id)},
        )
        order.error_message = "Broker request was not transmitted; no order was sent."
        order.rejected_at = datetime.now(UTC)
        apply_order_execution_quality(order)
        log.error(
            "execution.broker_submit_not_transmitted",
            order_id=str(order.id),
            ticker=order.ticker,
            side=order.side,
            error_type=evidence["error_type"],
        )
        await self._audit_broker_failure(
            order,
            action="broker_request_failed",
            decision="failed",
            reason=order.error_message,
            evidence=evidence,
            broker_environment=broker_environment,
            order_may_exist=False,
        )
        await self.db.commit()

    async def _record_broker_refusal(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt,
        exc: BrokerSubmissionRejected,
        broker_environment: str | None,
    ) -> None:
        evidence = self._failure_evidence(exc)
        if await self._resolved_elsewhere(order, attempt, "rejected", evidence):
            return
        self._close_attempt(attempt, "rejected", evidence)
        now = datetime.now(UTC)
        transition_order_status_with_evidence(
            self.db,
            order,
            "rejected",
            event_type="broker_rejected",
            reason="broker refused the order request",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload={**evidence, "attempt_id": str(attempt.id)},
        )
        order.error_message = "Broker refused the order request; no order was created."
        order.first_ack_at = now
        order.rejected_at = now
        order.broker_latency_ms = milliseconds_between(order.submitted_at, now)
        apply_order_execution_quality(order)
        log.warning(
            "execution.broker_submit_rejected",
            order_id=str(order.id),
            ticker=order.ticker,
            side=order.side,
            http_status=evidence["http_status"],
        )
        await self._audit_broker_failure(
            order,
            action="broker_request_rejected",
            decision="failed",
            reason=order.error_message,
            evidence=evidence,
            broker_environment=broker_environment,
            order_may_exist=False,
        )
        await self.db.commit()

    async def _record_unknown_outcome(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt,
        exc: BaseException,
        broker_environment: str | None,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        """Keep the order in submission_unknown and raise a durable critical event."""
        evidence = self._failure_evidence(exc)
        if broker_order_id:
            evidence = {**evidence, "broker_order_id": broker_order_id}
        if await self._resolved_elsewhere(order, attempt, "ambiguous", evidence):
            return
        if broker_order_id:
            # The broker named an order but the acknowledgement could not be interpreted:
            # keep the identity so reconciliation can query it directly.
            order.broker_order_id = broker_order_id
        self._close_attempt(attempt, "ambiguous", evidence)
        order.error_message = SUBMISSION_UNKNOWN_MESSAGE
        await self._log_order_event(
            order.id,
            "submission_outcome_unknown",
            from_status=order.status,
            to_status=order.status,
            payload={
                **evidence,
                "attempt_id": str(attempt.id),
                "automatic_resubmission": False,
            },
        )
        self._add_unknown_submission_risk_event(order, attempt, evidence)
        log.error(
            "execution.broker_submit_outcome_unknown",
            order_id=str(order.id),
            ticker=order.ticker,
            side=order.side,
            error_type=evidence["error_type"],
            http_status=evidence["http_status"],
        )
        await self._audit_broker_failure(
            order,
            action="broker_submission_unknown",
            decision="unknown",
            reason=SUBMISSION_UNKNOWN_MESSAGE,
            evidence=evidence,
            broker_environment=broker_environment,
            order_may_exist=True,
        )
        await self.db.commit()

        # Notify after the evidence is durable; a delivery failure must not undo it.
        await self._send_unknown_submission_alert(order)
        await self.db.commit()

    def _add_unknown_submission_risk_event(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt | None,
        evidence: dict[str, Any],
    ) -> None:
        self.db.add(
            RiskEvent(
                id=uuid.uuid4(),
                event_type="submission_unknown",
                ticker=order.ticker,
                signal_id=order.signal_id,
                order_id=order.id,
                message=SUBMISSION_UNKNOWN_MESSAGE,
                payload={
                    **evidence,
                    "severity": "critical",
                    "side": order.side,
                    "quantity": str(order.quantity),
                    "attempt_id": str(attempt.id) if attempt is not None else None,
                    "automatic_resubmission": False,
                },
                occurred_at=datetime.now(UTC),
            )
        )

    async def _send_unknown_submission_alert(self, order: Order) -> None:
        """Store and send the critical alert inside a savepoint, so a failure here cannot
        disturb anything else in the caller's session."""
        try:
            from app.services.alert_service import AlertService

            async with self.db.begin_nested():
                await AlertService(self.db).send(
                    alert_type="submission_unknown",
                    title=f"Order outcome unknown: {order.ticker}",
                    message=(
                        f"A {order.side} order for {order.ticker} may or may not exist at "
                        "the broker. It will not be resubmitted. Check the broker and "
                        "reconcile."
                    ),
                    severity="critical",
                    payload={
                        "order_id": str(order.id),
                        "ticker": order.ticker,
                        "side": order.side,
                    },
                )
        except Exception as alert_exc:
            log.warning(
                "execution.submission_unknown_alert_failed",
                order_id=str(order.id),
                error_type=type(alert_exc).__name__,
            )

    async def _record_acknowledgement(
        self,
        order: Order,
        attempt: OrderSubmissionAttempt,
        response: Any,
        broker_environment: str | None,
    ) -> None:
        # Interpret the acknowledgement completely before changing anything, so that a
        # malformed one leaves the order unknown instead of half-updated.
        try:
            if not isinstance(response, dict):
                raise ValueError("Broker acknowledgement is not an object")
            raw_id = response.get("id")
            broker_order_id = "" if raw_id is None else str(raw_id)
            broker_status = str(response.get("status", "") or "")
            filled_quantity = avg_fill_price = Decimal("0")
            if broker_status == "FILLED":
                filled_quantity = Decimal(
                    str(response.get("filledQuantity", float(abs(order.quantity))))
                )
                avg_fill_price = Decimal(str(response.get("filledPrice", 0) or 0))
            if not broker_order_id:
                raise ValueError("Broker acknowledgement has no order id")
        except Exception as exc:
            partial_id = None
            if isinstance(response, dict) and response.get("id") not in (None, ""):
                partial_id = str(response["id"])
            await self._record_unknown_outcome(
                order, attempt, exc, broker_environment, broker_order_id=partial_id
            )
            return

        if await self._resolved_elsewhere(
            order,
            attempt,
            "rejected" if broker_status == "REJECTED" else "accepted",
            {"broker_order_id": broker_order_id, "broker_status": broker_status},
        ):
            return

        first_ack_at = datetime.now(UTC)
        order.broker_response = response
        order.broker_order_id = broker_order_id
        order.first_ack_at = first_ack_at
        order.broker_latency_ms = milliseconds_between(order.submitted_at, first_ack_at)

        if broker_status == "FILLED":
            new_status, reason = "filled", "broker returned FILLED"
        elif broker_status == "CANCELLED":
            new_status, reason = "cancelled", "broker returned CANCELLED"
        elif broker_status == "REJECTED":
            new_status, reason = "rejected", "broker returned REJECTED"
        elif broker_status in ("WORKING", "PENDING"):
            new_status, reason = "accepted", "broker returned working status"
        else:
            new_status, reason = "accepted", "broker status defaulted to accepted"
        transition_event = transition_order_status_with_evidence(
            self.db,
            order,
            new_status,
            event_type="broker_accepted",
            reason=reason,
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload={"broker_status": broker_status, "attempt_id": str(attempt.id)},
        )
        if new_status == "filled":
            order.filled_quantity = filled_quantity
            order.avg_fill_price = avg_fill_price
            order.filled_at = first_ack_at
            order.fill_latency_ms = milliseconds_between(order.submitted_at, first_ack_at)
            order.reconciliation_latency_ms = order.fill_latency_ms
        elif new_status == "cancelled":
            order.cancelled_at = first_ack_at
        elif new_status == "rejected":
            order.rejected_at = first_ack_at
        self._close_attempt(
            attempt,
            "rejected" if new_status == "rejected" else "accepted",
            {"broker_order_id": broker_order_id, "broker_status": broker_status},
        )

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
        if transition_event is not None:
            transition_event.payload = {
                **dict(transition_event.payload or {}),
                "broker_status": broker_status,
                "broker_order_id": order.broker_order_id,
                "broker_latency_ms": order.broker_latency_ms,
                "execution_quality_score": float(order.execution_quality_score)
                if order.execution_quality_score is not None
                else None,
                "slippage_pct": float(order.slippage_pct)
                if order.slippage_pct is not None
                else None,
            }
        await self.db.commit()

    async def cancel_order(self, order: Order) -> Order:
        """Cancel a pending order at the broker."""
        locked_result = await self.db.execute(
            select(Order)
            .where(Order.id == order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        order = locked_result.scalar_one()
        if order.status == "cancelled":
            return order
        if order.status == "submission_unknown" and not order.is_dry_run:
            return await self._cancel_unknown_submission(order)
        if not can_transition_order_status(order.status, "cancelled"):
            transition_order_status_with_evidence(
                self.db,
                order,
                "cancelled",
                event_type="cancelled",
                reason="local cancellation requested",
                actor="execution_engine",
                correlation_id=order.client_order_key,
            )

        if order.broker_order_id and not order.is_dry_run:
            try:
                await self.broker.cancel_order(order.broker_order_id)
            except Exception as e:
                now = datetime.now(UTC)
                transition_order_status_with_evidence(
                    self.db,
                    order,
                    "error",
                    event_type="cancel_failed",
                    reason="broker cancellation failed",
                    actor="execution_engine",
                    correlation_id=order.client_order_key,
                    payload={"error_type": type(e).__name__},
                )
                order.error_message = _safe_broker_error_reason(e)
                order.rejected_at = now
                order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, now)
                apply_order_execution_quality(order)
                await self.db.flush()
                raise OrderCancellationFailed(order) from None

        now = datetime.now(UTC)
        cancellation_payload = {
            "reconciliation_latency_ms": milliseconds_between(order.submitted_at, now),
            "filled_quantity": str(order.filled_quantity or Decimal("0")),
            "cancelled_quantity": str(order.remaining_quantity),
        }
        transition_order_status_with_evidence(
            self.db,
            order,
            "cancelled",
            event_type="cancelled",
            reason="local cancellation requested",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload=cancellation_payload,
        )
        order.cancelled_at = now
        order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, now)
        apply_order_execution_quality(order)
        await self.db.flush()
        return order

    async def _cancel_unknown_submission(self, order: Order) -> Order:
        """Cancel an order whose submission outcome is unknown, on broker evidence only.

        Without a broker order id nothing can be cancelled at the broker, and marking the
        order cancelled locally would hide an order that may be live. With an id the
        cancellation is attempted; a failure leaves the order unknown.
        """
        failure: Exception | None = None
        if order.broker_order_id:
            try:
                await self.broker.cancel_order(order.broker_order_id)
            except Exception as exc:
                failure = exc
            else:
                now = datetime.now(UTC)
                transition_order_status_with_evidence(
                    self.db,
                    order,
                    "cancelled",
                    event_type="cancelled",
                    reason="broker accepted cancellation of an unconfirmed order",
                    actor="execution_engine",
                    correlation_id=order.client_order_key,
                    payload={"broker_order_id": order.broker_order_id},
                )
                order.cancelled_at = now
                order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, now)
                apply_order_execution_quality(order)
                await self.db.flush()
                return order

        await self._log_order_event(
            order.id,
            "cancel_blocked_unknown_submission",
            from_status=order.status,
            to_status=order.status,
            payload={
                "broker_order_id": order.broker_order_id,
                "error_type": type(failure).__name__ if failure is not None else None,
                "reason": "cancellation cannot be confirmed; reconciliation is required",
            },
        )
        await self.db.flush()
        raise OrderCancellationFailed(order) from None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _matches_unknown_submission(
        self, order: Order, snapshot: BrokerOrderSnapshot, dispatched_at: datetime
    ) -> bool:
        """True only when every available broker field agrees with the dispatched request."""
        if not snapshot.broker_order_id or snapshot.quantity is None:
            return False
        if (snapshot.ticker or "").upper() != order.ticker.upper():
            return False
        if abs(snapshot.quantity) != order.quantity:
            return False
        side = (snapshot.side or "").lower() or ("sell" if snapshot.quantity < 0 else "buy")
        if side != order.side:
            return False
        if snapshot.order_type and snapshot.order_type.lower() != order.order_type:
            return False
        raw = snapshot.raw or {}
        for field, expected in (("limitPrice", order.limit_price), ("stopPrice", order.stop_price)):
            reported = raw.get(field)
            if reported is None:
                continue
            try:
                if expected is None or Decimal(str(reported)) != expected:
                    return False
            except ArithmeticError:
                return False
        if snapshot.created_at is None:
            return False
        created_at = self._as_utc(snapshot.created_at)
        return (
            dispatched_at - self.UNKNOWN_SUBMISSION_MATCH_BEFORE
            <= created_at
            <= dispatched_at + self.UNKNOWN_SUBMISSION_MATCH_AFTER
        )

    async def _fits_another_unknown_submission(
        self, order: Order, snapshot: BrokerOrderSnapshot
    ) -> bool:
        """True when another unresolved local order would match the same broker record."""
        others = (
            await self.db.execute(
                select(Order, OrderSubmissionAttempt.dispatch_committed_at)
                .outerjoin(OrderSubmissionAttempt, OrderSubmissionAttempt.order_id == Order.id)
                .where(
                    Order.id != order.id,
                    Order.status == "submission_unknown",
                    Order.broker_order_id.is_(None),
                    Order.ticker == order.ticker,
                    Order.side == order.side,
                )
            )
        ).all()
        for other, other_dispatched in others:
            reference = other_dispatched or other.submitted_at
            if reference is not None and self._matches_unknown_submission(
                other, snapshot, self._as_utc(reference)
            ):
                return True
        return False

    async def reconcile_unknown_submission(self, order: Order) -> Order:
        """Resolve a submission_unknown order from read-only broker history.

        The order is resolved only when exactly one history record matches the dispatched
        request and is not already linked to another order. No match, several matches or a
        history failure leave it unresolved. This method never places an order.
        """
        if order.status != "submission_unknown" or order.is_dry_run:
            return order
        if order.broker_order_id:
            return await self.reconcile_order(order)

        attempt = (
            await self.db.execute(
                select(OrderSubmissionAttempt).where(OrderSubmissionAttempt.order_id == order.id)
            )
        ).scalar_one_or_none()
        dispatched = attempt.dispatch_committed_at if attempt is not None else order.submitted_at
        now = datetime.now(UTC)
        if (
            dispatched is not None
            and now - self._as_utc(dispatched) < self.UNKNOWN_SUBMISSION_MIN_AGE
        ):
            # The original request may still be in flight; its own outcome comes first.
            return order

        # A process that died after dispatch never raised the alarm itself: do it once here.
        already_raised = (
            await self.db.execute(
                select(RiskEvent.id)
                .where(RiskEvent.order_id == order.id, RiskEvent.event_type == "submission_unknown")
                .limit(1)
            )
        ).scalar_one_or_none()
        if already_raised is None:
            self._add_unknown_submission_risk_event(
                order, attempt, {"error_type": "NoRecordedOutcome", "http_status": None}
            )
            order.error_message = SUBMISSION_UNKNOWN_MESSAGE
            await self.db.flush()
            await self._send_unknown_submission_alert(order)

        matches: list[BrokerOrderSnapshot] = []
        unresolved_reason: str | None = None
        if dispatched is None:
            unresolved_reason = "no_dispatch_time"
        else:
            dispatched_at = self._as_utc(dispatched)
            window_start = dispatched_at - self.UNKNOWN_SUBMISSION_MATCH_BEFORE
            try:
                history = await self.broker.get_historical_orders(
                    ticker=order.ticker, limit=self.UNKNOWN_SUBMISSION_HISTORY_LIMIT
                )
                items = history.get("items") if isinstance(history, dict) else history
                items = (
                    [item for item in items if isinstance(item, dict)]
                    if isinstance(items, list)
                    else []
                )
                reaches_before_window = False
                for item in items:
                    try:
                        snapshot = map_trading212_history_order_to_snapshot(
                            item, environment=getattr(self.broker, "environment", None) or "demo"
                        )
                    except ValueError:
                        continue
                    if snapshot.created_at and self._as_utc(snapshot.created_at) < window_start:
                        reaches_before_window = True
                    if self._matches_unknown_submission(order, snapshot, dispatched_at):
                        matches.append(snapshot)
                if (
                    len(items) >= self.UNKNOWN_SUBMISSION_HISTORY_LIMIT
                    and not reaches_before_window
                ):
                    # A full page that does not reach back past the window may hide more
                    # records, so "exactly one" cannot be established from it.
                    matches = []
                    unresolved_reason = "history_page_does_not_cover_window"
            except Exception as exc:
                matches = []
                unresolved_reason = f"history_failed:{type(exc).__name__}"

        if matches:
            linked = (
                (
                    await self.db.execute(
                        select(Order.broker_order_id).where(
                            Order.broker_order_id.in_([m.broker_order_id for m in matches]),
                            Order.id != order.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            matches = [m for m in matches if m.broker_order_id not in set(linked)]

        if len(matches) == 1 and await self._fits_another_unknown_submission(order, matches[0]):
            # Two local orders could own this one broker record; neither may claim it.
            matches = []
            unresolved_reason = "record_fits_several_local_orders"

        if len(matches) != 1:
            order.last_reconciled_at = now
            log.warning(
                "execution.unknown_submission_unresolved",
                order_id=str(order.id),
                ticker=order.ticker,
                candidate_count=len(matches),
                reason=unresolved_reason,
            )
            await self.db.flush()
            return order

        match = matches[0]
        broker_status = (match.status or "").upper()
        new_status = {
            "FILLED": "filled",
            "CANCELLED": "cancelled",
            "REJECTED": "rejected",
            "WORKING": "accepted",
            "PENDING": "accepted",
        }.get(broker_status)
        filled_quantity = abs(match.filled_quantity) if match.filled_quantity is not None else None
        if new_status == "filled" and filled_quantity != order.quantity:
            # The record is ours but does not describe a complete fill; keep the identity
            # and let reconciliation by order id settle the final state.
            new_status = None
        if filled_quantity is not None and filled_quantity > order.quantity:
            new_status = None

        order.broker_order_id = match.broker_order_id
        order.last_reconciled_at = now
        evidence = {
            "broker_order_id": match.broker_order_id,
            "broker_status": broker_status or None,
            "broker_created_at": self._as_utc(match.created_at).isoformat()
            if match.created_at
            else None,
            "attempt_id": str(attempt.id) if attempt is not None else None,
            "matched_records": 1,
        }
        if new_status is None:
            await self._log_order_event(
                order.id,
                "unknown_submission_identified",
                from_status=order.status,
                to_status=order.status,
                payload=evidence,
            )
            await self.db.flush()
            return order

        transition_order_status_with_evidence(
            self.db,
            order,
            new_status,
            event_type="unknown_submission_resolved",
            reason="one broker history record matches the dispatched request",
            actor="execution_engine",
            correlation_id=order.client_order_key,
            payload=evidence,
        )
        order.error_message = None
        terminal_at = self._as_utc(match.filled_at) if match.filled_at else now
        if new_status == "filled":
            order.filled_quantity = filled_quantity
            if match.average_fill_price is not None:
                order.avg_fill_price = match.average_fill_price
            order.filled_at = terminal_at
            order.fill_latency_ms = milliseconds_between(order.submitted_at, order.filled_at)
        else:
            if filled_quantity:
                # A partial fill before the cancellation or rejection is real exposure.
                order.filled_quantity = filled_quantity
                if match.average_fill_price is not None:
                    order.avg_fill_price = match.average_fill_price
            if new_status == "cancelled":
                order.cancelled_at = terminal_at
            elif new_status == "rejected":
                order.rejected_at = terminal_at
        order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, now)
        apply_order_execution_quality(order)
        await self._maybe_alert_abnormal_slippage(order)
        await self.db.flush()
        return order

    async def reconcile_order(self, order: Order) -> Order:
        """
        Poll broker for latest order status.
        Only reconcile orders in active state.
        NEVER blindly retry on uncertain state.
        """
        if order.status not in ACTIVE_ORDER_STATUSES or not order.broker_order_id:
            return order

        if order.is_dry_run:
            return order

        try:
            response = await self.broker.get_order_by_id(order.broker_order_id)
            broker_status = response.get("status", "")

            if broker_status == "FILLED":
                reconciled_at = datetime.now(UTC)
                current_filled = order.filled_quantity or Decimal("0")
                reported_filled = Decimal(str(response.get("filledQuantity", 0)))
                if reported_filled < 0 or reported_filled > order.quantity:
                    raise ValueError("Broker reported an invalid filled quantity")
                reconciled_filled = max(current_filled, reported_filled)
                if reconciled_filled != order.quantity:
                    raise ValueError("Broker FILLED response does not cover the order quantity")
                reconciled_price = order.avg_fill_price
                if reported_filled >= current_filled and response.get("filledPrice") is not None:
                    reported_price = Decimal(str(response.get("filledPrice") or 0))
                    if reported_price > 0:
                        reconciled_price = reported_price
                transition_event = transition_order_status_with_evidence(
                    self.db,
                    order,
                    "filled",
                    event_type="reconciled_fill",
                    reason="reconciliation returned FILLED",
                    actor="execution_engine",
                    correlation_id=order.client_order_key,
                    payload={
                        "broker_status": broker_status,
                        "filled_quantity": str(reconciled_filled),
                        "remaining_quantity": "0",
                        "avg_fill_price": str(reconciled_price)
                        if reconciled_price is not None
                        else None,
                    },
                )
                order.filled_quantity = reconciled_filled
                if reconciled_price is not None:
                    order.avg_fill_price = reconciled_price
                order.broker_response = response
                order.filled_at = order.filled_at or reconciled_at
                order.fill_latency_ms = milliseconds_between(order.submitted_at, order.filled_at)
                order.reconciliation_latency_ms = milliseconds_between(
                    order.submitted_at, reconciled_at
                )
                order.last_reconciled_at = reconciled_at
                order.execution_quality_notes = None
                order.slippage_pct = None
                order.slippage_value = None
                quality = apply_order_execution_quality(order)
                await self._maybe_alert_abnormal_slippage(order)
                if transition_event is not None:
                    transition_event.payload = {
                        **dict(transition_event.payload or {}),
                        "broker_status": broker_status,
                        "fill_latency_ms": order.fill_latency_ms,
                        "reconciliation_latency_ms": order.reconciliation_latency_ms,
                        "execution_quality_score": float(quality["execution_quality_score"])
                        if quality["execution_quality_score"] is not None
                        else None,
                        "slippage_pct": float(quality["slippage_pct"])
                        if quality["slippage_pct"] is not None
                        else None,
                    }
            elif broker_status in ("CANCELLED", "REJECTED"):
                reconciled_at = datetime.now(UTC)
                current_filled = order.filled_quantity or Decimal("0")
                broker_filled = current_filled
                broker_fill_price = order.avg_fill_price
                if response.get("filledQuantity") is not None:
                    reported_filled = Decimal(str(response["filledQuantity"]))
                    if reported_filled < 0 or reported_filled > order.quantity:
                        raise ValueError("Broker reported an invalid terminal filled quantity")
                    if reported_filled >= current_filled:
                        broker_filled = reported_filled
                        if response.get("filledPrice") is not None:
                            reported_price = Decimal(str(response["filledPrice"] or 0))
                            if reported_price > 0:
                                broker_fill_price = reported_price
                remaining_quantity = max(Decimal("0"), order.quantity - broker_filled)
                transition_order_status_with_evidence(
                    self.db,
                    order,
                    broker_status.lower(),
                    event_type="reconciled_status",
                    reason=f"reconciliation returned {broker_status}",
                    actor="execution_engine",
                    correlation_id=order.client_order_key,
                    payload={
                        "broker_status": broker_status,
                        "filled_quantity": str(broker_filled),
                        "remaining_quantity": str(remaining_quantity),
                        "avg_fill_price": str(broker_fill_price)
                        if broker_fill_price is not None
                        else None,
                    },
                )
                order.filled_quantity = broker_filled
                if broker_filled > 0 and broker_fill_price is not None:
                    order.avg_fill_price = broker_fill_price
                if order.status == "cancelled":
                    order.cancelled_at = order.cancelled_at or reconciled_at
                else:
                    order.rejected_at = order.rejected_at or reconciled_at
                order.reconciliation_latency_ms = milliseconds_between(
                    order.submitted_at, reconciled_at
                )
                order.last_reconciled_at = reconciled_at
                order.broker_response = response
                order.execution_quality_notes = None
                order.slippage_pct = None
                order.slippage_value = None
                apply_order_execution_quality(order)
            elif order.status == "submission_unknown" and broker_status in ("WORKING", "PENDING"):
                transition_order_status_with_evidence(
                    self.db,
                    order,
                    "accepted",
                    event_type="unknown_submission_resolved",
                    reason="broker reports the order as working",
                    actor="execution_engine",
                    correlation_id=order.client_order_key,
                    payload={
                        "broker_status": broker_status,
                        "broker_order_id": order.broker_order_id,
                    },
                )
                order.error_message = None
                order.last_reconciled_at = datetime.now(UTC)
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
                error_type=type(e).__name__,
            )

        await self.db.flush()
        return order
