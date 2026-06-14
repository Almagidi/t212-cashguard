"""Order lifecycle transition guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import Order


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
    "partially_filled": frozenset({"filled", "cancelled", "error"}),
    "filled": frozenset(),
    "cancelled": frozenset(),
    "rejected": frozenset(),
    "error": frozenset(),
}

KNOWN_ORDER_STATUSES = frozenset(ORDER_STATUS_TRANSITIONS)


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
