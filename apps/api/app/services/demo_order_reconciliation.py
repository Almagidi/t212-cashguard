"""Trading 212 demo order-history reconciliation.

This service is intentionally terminal/test oriented. It reads Trading 212 DEMO
order history and reconciles an existing local demo order only after a broker
history record matches the stored broker_order_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.broker.trading212_mappers import map_trading212_history_order_to_snapshot
from app.core.config import settings
from app.db.models import AuditLog, Order, OrderEvent
from app.execution.engine import _safe_broker_error_reason
from app.execution.paper_engine import PAPER_EXECUTION_ENVIRONMENT
from app.execution.state_machine import transition_order_status
from app.services.execution_quality import (
    apply_order_execution_quality,
    milliseconds_between,
)
from app.services.safety_policy import SafetyPolicyViolation

if TYPE_CHECKING:
    from app.broker.protocols import ReconciliationHistoryBrokerProtocol
    from app.broker.snapshots import BrokerOrderSnapshot


@dataclass(frozen=True)
class DemoOrderReconciliationResult:
    order_id: uuid.UUID
    broker_order_id: str | None
    previous_status: str
    new_status: str
    matched: bool
    outcome: str
    broker_status: str | None = None
    error_type: str | None = None
    audit_events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _HistoryMatch:
    item: dict[str, Any]
    snapshot: BrokerOrderSnapshot | None = None
    parse_error: ValueError | None = None


class DemoOrderReconciler:
    """Reconcile local demo orders against Trading 212 historical orders."""

    def __init__(
        self,
        db: Any,
        broker: ReconciliationHistoryBrokerProtocol,
        *,
        actor: str = "demo_order_reconciler",
        history_limit: int | None = None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.actor = actor
        self.history_limit = history_limit or 50
        self._audit_events: list[str] = []

    async def reconcile_by_order_id(self, order_id: uuid.UUID) -> DemoOrderReconciliationResult:
        order = await self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"Local order not found: {order_id}")
        return await self.reconcile_order(order)

    async def reconcile_by_broker_order_id(
        self,
        broker_order_id: str,
    ) -> DemoOrderReconciliationResult:
        result = await self.db.execute(
            select(Order).where(Order.broker_order_id == str(broker_order_id)).limit(1)
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise ValueError(f"Local order not found for broker_order_id: {broker_order_id}")
        return await self.reconcile_order(order)

    async def reconcile_order(self, order: Order) -> DemoOrderReconciliationResult:
        self._audit_events = []
        self._require_demo_reconciliation_allowed(order)

        previous_status = order.status
        await self._audit(
            "demo_order_reconciliation_attempt",
            order,
            {
                "broker_environment": "demo",
                "broker_order_id": order.broker_order_id,
                "read_only_endpoint": "/api/v0/equity/history/orders",
                "no_broker_order_sent": True,
            },
        )

        try:
            history = await self.broker.get_historical_orders(limit=self.history_limit)
        except T212RateLimitError as exc:
            await self._audit(
                "demo_order_reconciliation_rate_limited",
                order,
                {
                    "broker_environment": "demo",
                    "broker_order_id": order.broker_order_id,
                    "retry_after": exc.retry_after,
                    "error_type": type(exc).__name__,
                    "no_status_update": True,
                    "no_broker_order_sent": True,
                },
            )
            return self._result(
                order,
                previous_status=previous_status,
                matched=False,
                outcome="rate_limited",
                error_type=type(exc).__name__,
            )
        except (T212AuthError, T212APIError) as exc:
            await self._audit(
                "demo_order_reconciliation_failed",
                order,
                {
                    "broker_environment": "demo",
                    "broker_order_id": order.broker_order_id,
                    "error_type": type(exc).__name__,
                    "safe_reason": _safe_broker_error_reason(exc),
                    "no_status_update": True,
                    "no_broker_order_sent": True,
                },
            )
            return self._result(
                order,
                previous_status=previous_status,
                matched=False,
                outcome="failed",
                error_type=type(exc).__name__,
            )

        matched_history = self._find_history_match(history, order.broker_order_id)
        if matched_history is None:
            await self._audit(
                "demo_order_reconciliation_missing",
                order,
                {
                    "broker_environment": "demo",
                    "broker_order_id": order.broker_order_id,
                    "no_status_update": True,
                    "no_broker_order_sent": True,
                },
            )
            return self._result(
                order,
                previous_status=previous_status,
                matched=False,
                outcome="missing",
            )

        if matched_history.parse_error is not None:
            await self._audit(
                "demo_order_reconciliation_failed",
                order,
                {
                    "broker_environment": "demo",
                    "broker_order_id": order.broker_order_id,
                    "error_type": type(matched_history.parse_error).__name__,
                    "safe_reason": _safe_broker_error_reason(matched_history.parse_error),
                    "parse_failed": True,
                    "no_status_update": True,
                    "no_broker_order_sent": True,
                },
            )
            return self._result(
                order,
                previous_status=previous_status,
                matched=True,
                outcome="failed",
                error_type=type(matched_history.parse_error).__name__,
            )

        match = matched_history.item
        snapshot = matched_history.snapshot
        if snapshot is None:
            raise RuntimeError("Matched Trading 212 history item is missing a parsed snapshot.")
        broker_status = self._broker_status(snapshot)
        broker_ticker = snapshot.ticker
        mapped_status = self._map_broker_status(broker_status)
        if mapped_status is None:
            order.last_reconciled_at = datetime.now(UTC)
            order.broker_response = match
            await self._audit(
                "demo_order_reconciliation_unknown_status",
                order,
                {
                    "broker_environment": "demo",
                    "broker_order_id": order.broker_order_id,
                    "broker_ticker": broker_ticker,
                    "broker_status": broker_status,
                    "previous_status": previous_status,
                    "new_status": order.status,
                    "no_destructive_update": True,
                    "no_broker_order_sent": True,
                },
            )
            await self.db.flush()
            return self._result(
                order,
                previous_status=previous_status,
                matched=True,
                outcome="unknown_status",
                broker_status=broker_status,
            )

        await self._apply_match(order, match, snapshot, mapped_status)
        await self._log_order_event(
            order,
            "demo_history_reconciled",
            previous_status,
            order.status,
            {
                "broker_status": broker_status,
                "broker_order_id": order.broker_order_id,
                "broker_ticker": broker_ticker,
                "read_only_endpoint": "/api/v0/equity/history/orders",
            },
        )
        await self._audit(
            "demo_order_reconciliation_success",
            order,
            {
                "broker_environment": "demo",
                "broker_order_id": order.broker_order_id,
                "broker_ticker": broker_ticker,
                "broker_status": broker_status,
                "previous_status": previous_status,
                "new_status": order.status,
                "matched": True,
                "no_broker_order_sent": True,
            },
        )
        await self.db.flush()
        return self._result(
            order,
            previous_status=previous_status,
            matched=True,
            outcome="success",
            broker_status=broker_status,
        )

    def _require_demo_reconciliation_allowed(self, order: Order) -> None:
        broker_environment = getattr(self.broker, "environment", None)
        if settings.APP_MODE != "demo":
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: APP_MODE must be demo.",
                decision_code="demo_reconciliation_app_mode_block",
            )
        if settings.T212_ENVIRONMENT != "demo":
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: T212_ENVIRONMENT must be demo.",
                decision_code="demo_reconciliation_t212_environment_block",
            )
        if bool(settings.LIVE_TRADING_ENABLED):
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: LIVE_TRADING_ENABLED must be false.",
                decision_code="demo_reconciliation_live_flag_block",
            )
        if broker_environment != "demo":
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: broker environment must be demo.",
                decision_code="demo_reconciliation_broker_environment_block",
            )
        if order.execution_environment != "demo":
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: local order is not a demo order.",
                decision_code="demo_reconciliation_order_environment_block",
            )
        if order.is_dry_run or order.execution_environment == PAPER_EXECUTION_ENVIRONMENT:
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: local order is dry-run or paper-only.",
                decision_code="demo_reconciliation_dry_run_block",
            )
        if not order.broker_order_id:
            raise SafetyPolicyViolation(
                "Demo order reconciliation blocked: local order has no broker_order_id.",
                decision_code="demo_reconciliation_missing_broker_order_id",
            )

    @staticmethod
    def _history_items(history: object) -> list[dict[str, Any]]:
        """Return dict items from Trading 212 history payloads.

        Trading 212 returns paginated history as {"items": [...]}. Older tests
        and defensive callers may provide {"data": [...]} or a bare list.
        Keep the input as object and raw_items as object so mypy does not infer
        impossible branch types during narrowing.
        """
        raw_items: object

        if isinstance(history, list):
            raw_items = history
        elif isinstance(history, dict):
            raw_items = history.get("items")
            if not isinstance(raw_items, list):
                raw_items = history.get("data")
            if not isinstance(raw_items, list):
                return []
        else:
            return []

        return [item for item in raw_items if isinstance(item, dict)]

    def _find_history_match(
        self,
        history: Any,
        broker_order_id: str | None,
    ) -> _HistoryMatch | None:
        target = str(broker_order_id)
        for item in self._history_items(history):
            candidates = (
                item.get("id"),
                item.get("orderId"),
                item.get("order_id"),
                item.get("broker_order_id"),
                _nested_value(item, "order", "id"),
            )
            if not any(value is not None and str(value) == target for value in candidates):
                continue
            try:
                snapshot = map_trading212_history_order_to_snapshot(item)
            except ValueError as exc:
                return _HistoryMatch(item=item, parse_error=exc)
            return _HistoryMatch(item=item, snapshot=snapshot)
        return None

    @staticmethod
    def _broker_status(snapshot: BrokerOrderSnapshot) -> str | None:
        return snapshot.status.upper() if snapshot.status is not None else None

    @staticmethod
    def _map_broker_status(broker_status: str | None) -> str | None:
        if broker_status == "FILLED":
            return "filled"
        if broker_status == "CANCELLED":
            return "cancelled"
        if broker_status == "REJECTED":
            return "rejected"
        if broker_status in {"WORKING", "PENDING"}:
            return "accepted"
        return None

    async def _apply_match(
        self,
        order: Order,
        item: dict[str, Any],
        snapshot: BrokerOrderSnapshot,
        mapped_status: str,
    ) -> None:
        reconciled_at = datetime.now(UTC)
        transition_order_status(
            order,
            mapped_status,
            reason="demo history reconciliation status update",
        )
        order.last_reconciled_at = reconciled_at
        order.broker_response = item

        if snapshot.filled_quantity is not None:
            order.filled_quantity = snapshot.filled_quantity
        if snapshot.average_fill_price is not None:
            order.avg_fill_price = snapshot.average_fill_price

        # For Trading 212 history snapshots, filled_at carries the best available
        # terminal event timestamp for filled, cancelled, and rejected records.
        terminal_at = snapshot.filled_at or reconciled_at

        if mapped_status == "filled":
            order.filled_at = order.filled_at or terminal_at
            order.fill_latency_ms = milliseconds_between(order.submitted_at, order.filled_at)
        elif mapped_status == "cancelled":
            order.cancelled_at = order.cancelled_at or terminal_at
        elif mapped_status == "rejected":
            order.rejected_at = order.rejected_at or terminal_at
            reason = _first_text(item, "rejectReason", "rejectionReason", "reason")
            if reason:
                order.error_message = reason

        order.reconciliation_latency_ms = milliseconds_between(order.submitted_at, reconciled_at)
        apply_order_execution_quality(order)

    async def _audit(self, action: str, order: Order, payload: dict[str, Any]) -> None:
        self._audit_events.append(action)
        self.db.add(
            AuditLog(
                action=action,
                entity_type="order",
                entity_id=str(order.id),
                actor=self.actor,
                payload={
                    "mode": settings.APP_MODE,
                    "execution_environment": order.execution_environment,
                    "is_dry_run": order.is_dry_run,
                    "order_id": str(order.id),
                    "ticker": order.ticker,
                    "side": order.side,
                    **payload,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    async def _log_order_event(
        self,
        order: Order,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.db.add(
            OrderEvent(
                id=uuid.uuid4(),
                order_id=order.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    def _result(
        self,
        order: Order,
        *,
        previous_status: str,
        matched: bool,
        outcome: str,
        broker_status: str | None = None,
        error_type: str | None = None,
    ) -> DemoOrderReconciliationResult:
        return DemoOrderReconciliationResult(
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            previous_status=previous_status,
            new_status=order.status,
            matched=matched,
            outcome=outcome,
            broker_status=broker_status,
            error_type=error_type,
            audit_events=list(self._audit_events),
        )


Path = str | tuple[str, ...]


def _nested_value(item: dict[str, Any], *path: str) -> Any:
    current: Any = item
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_text(item: dict[str, Any], *paths: Path) -> str | None:
    for path in paths:
        value = _nested_value(item, path) if isinstance(path, str) else _nested_value(item, *path)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
