"""Order lifecycle transition guard."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Order, OrderEvent


TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "rejected", "error"})

ORDER_STATUS_TRANSITIONS = {
    "pending_intent": frozenset({"submitted", "rejected", "cancelled", "error"}),
    "submitted": frozenset(
        {
            "accepted",
            "partially_filled",
            "filled",
            "rejected",
            "cancelled",
            "error",
        }
    ),
    "accepted": frozenset({"partially_filled", "filled", "rejected", "cancelled", "error"}),
    "partially_filled": frozenset({"filled", "cancelled", "rejected", "error"}),
    "filled": frozenset(),
    "cancelled": frozenset(),
    "rejected": frozenset(),
    "error": frozenset(),
}

KNOWN_ORDER_STATUSES = frozenset(ORDER_STATUS_TRANSITIONS)
ACTIVE_ORDER_STATUSES = KNOWN_ORDER_STATUSES - TERMINAL_ORDER_STATUSES


class InvalidOrderTransition(ValueError):
    """Raised when an order lifecycle transition is not explicitly allowed."""


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_ORDER_STATUSES


def can_transition_order_status(from_status: str, to_status: str) -> bool:
    if from_status not in KNOWN_ORDER_STATUSES or to_status not in KNOWN_ORDER_STATUSES:
        return False
    if from_status == to_status:
        return True
    return to_status in ORDER_STATUS_TRANSITIONS[from_status]


def transition_order_status(
    order: Order,
    to_status: str,
    *,
    reason: str | None = None,
) -> None:
    from_status = order.status
    if can_transition_order_status(from_status, to_status):
        order.status = to_status
        return

    details = f"Invalid order status transition: {from_status!r} -> {to_status!r}"
    if reason:
        details = f"{details} ({reason})"
    raise InvalidOrderTransition(details)


def transition_order_status_with_evidence(
    db: AsyncSession,
    order: Order,
    to_status: str,
    *,
    event_type: str,
    reason: str,
    actor: str,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> OrderEvent | None:
    """Stage a validated transition and its event/audit in one transaction."""
    if not event_type or len(event_type) > 50:
        raise ValueError("event_type must contain 1-50 characters")
    if not actor or len(actor) > 100:
        raise ValueError("actor must contain 1-100 characters")
    if not reason or len(reason) > 500:
        raise ValueError("reason must contain 1-500 characters")
    if correlation_id is not None and len(correlation_id) > 100:
        raise ValueError("correlation_id must contain at most 100 characters")

    from app.db.models import AuditLog, OrderEvent

    from_status = order.status
    if from_status == to_status:
        transition_order_status(order, to_status, reason=reason)
        return None

    transition_order_status(order, to_status, reason=reason)
    occurred_at = datetime.now(UTC)
    event_payload = {
        **dict(payload or {}),
        "transition_reason": reason,
        "transition_actor": actor,
        "correlation_id": correlation_id,
    }
    event = OrderEvent(
        id=uuid.uuid4(),
        order_id=order.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        payload=event_payload,
        occurred_at=occurred_at,
    )
    audit = AuditLog(
        id=uuid.uuid4(),
        action="order_status_transitioned",
        entity_type="order",
        entity_id=str(order.id),
        actor=actor,
        payload={
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "correlation_id": correlation_id,
        },
        occurred_at=occurred_at,
    )
    db.add_all((event, audit))
    return event
