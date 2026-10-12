"""What a broker's immediate answer to an order request may be taken to mean.

The answer is interpreted completely before anything is written. A final state (filled,
cancelled, rejected) is accepted only when every field it depends on is present and
consistent with the order that was sent. Anything else is refused with a reason, and the
caller leaves the order unresolved for read-only reconciliation; it is never sent again.

Pure functions: no database, no broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# orders.broker_order_id is String(100).
MAX_BROKER_ORDER_ID_LENGTH = 100
# orders.avg_fill_price is Numeric(20, 8): anything smaller would be stored as zero.
SMALLEST_STORED_PRICE = Decimal("0.00000001")
_ZERO = Decimal("0")


class UntrustedAcknowledgement(Exception):
    """The broker's answer cannot be relied on to say what happened to the order.

    ``reason`` is a fixed word, safe to store. ``broker_order_id`` is set when the answer
    named an order in a usable way, so reconciliation can ask about it directly.
    """

    def __init__(self, reason: str, *, broker_order_id: str | None = None) -> None:
        self.reason = reason
        self.broker_order_id = broker_order_id
        super().__init__(f"Broker acknowledgement not trusted ({reason}).")


@dataclass(frozen=True)
class Acknowledgement:
    """A broker answer that is safe to act on."""

    broker_order_id: str
    broker_status: str
    order_status: str
    transition_reason: str
    filled_quantity: Decimal | None = None
    fill_price: Decimal | None = None


def _usable_order_id(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text and len(text) <= MAX_BROKER_ORDER_ID_LENGTH:
            return text
    return None


def _finite_number(raw: Any) -> Decimal | None:
    """The value as a finite decimal, or None when it is not a number at all."""
    if not isinstance(raw, int | float | str | Decimal):
        return None
    try:
        number = Decimal(str(raw).strip())
    except InvalidOperation:
        # Includes True and False, which are whole numbers to Python but not to a broker.
        return None
    return number if number.is_finite() else None


def _reported_fill(response: dict[str, Any], *, side: str, broker_order_id: str) -> Decimal | None:
    """The amount the answer says was filled, as a positive number; None when it says
    nothing. A sell request carries a negative quantity, so a negative amount is accepted
    for a sell and for nothing else."""
    raw = response.get("filledQuantity")
    if raw is None:
        return None
    signed = _finite_number(raw)
    if signed is None:
        raise UntrustedAcknowledgement(
            "filled_quantity_not_a_number", broker_order_id=broker_order_id
        )
    if signed < 0 and side != "sell":
        raise UntrustedAcknowledgement(
            "filled_quantity_sign_contradicts_side", broker_order_id=broker_order_id
        )
    # copy_abs, not abs(): it cannot overflow, however large the number.
    return signed.copy_abs()


def _fill_price(response: dict[str, Any], *, broker_order_id: str) -> Decimal:
    raw = response.get("filledPrice")
    if raw is None:
        raise UntrustedAcknowledgement("fill_price_missing", broker_order_id=broker_order_id)
    price = _finite_number(raw)
    if price is None:
        raise UntrustedAcknowledgement("fill_price_not_a_number", broker_order_id=broker_order_id)
    if price < SMALLEST_STORED_PRICE:
        raise UntrustedAcknowledgement("fill_price_not_positive", broker_order_id=broker_order_id)
    return price


def interpret_acknowledgement(response: Any, *, side: str, quantity: Decimal) -> Acknowledgement:
    """Decide what the broker's answer means for an order of ``quantity`` on ``side``.

    Raises ``UntrustedAcknowledgement`` when the answer must not be acted on.
    """
    if not isinstance(response, dict):
        raise UntrustedAcknowledgement("not_an_object")
    broker_order_id = _usable_order_id(response.get("id"))
    if broker_order_id is None:
        raise UntrustedAcknowledgement("order_id_not_usable")
    raw_status = response.get("status")
    broker_status = raw_status if isinstance(raw_status, str) else ""

    if broker_status == "FILLED":
        filled = _reported_fill(response, side=side, broker_order_id=broker_order_id)
        if filled is None:
            raise UntrustedAcknowledgement(
                "filled_quantity_missing", broker_order_id=broker_order_id
            )
        if filled == _ZERO:
            raise UntrustedAcknowledgement("filled_without_a_fill", broker_order_id=broker_order_id)
        if filled > quantity:
            raise UntrustedAcknowledgement(
                "filled_quantity_exceeds_order", broker_order_id=broker_order_id
            )
        if filled < quantity:
            raise UntrustedAcknowledgement(
                "filled_quantity_below_order", broker_order_id=broker_order_id
            )
        return Acknowledgement(
            broker_order_id=broker_order_id,
            broker_status=broker_status,
            order_status="filled",
            transition_reason="broker returned FILLED",
            filled_quantity=quantity,
            fill_price=_fill_price(response, broker_order_id=broker_order_id),
        )

    if broker_status in ("CANCELLED", "REJECTED"):
        # A final order is never reconciled again, so a fill reported alongside a final
        # status must not be dropped here. Reconciliation by order id records it.
        filled = _reported_fill(response, side=side, broker_order_id=broker_order_id)
        if filled is not None and filled != _ZERO:
            raise UntrustedAcknowledgement(
                "final_status_reports_a_fill", broker_order_id=broker_order_id
            )
        return Acknowledgement(
            broker_order_id=broker_order_id,
            broker_status=broker_status,
            order_status=broker_status.lower(),
            transition_reason=f"broker returned {broker_status}",
        )

    # Not final: the order stays active and reconciliation asks about it by id.
    working = broker_status in ("WORKING", "PENDING")
    return Acknowledgement(
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        order_status="accepted",
        transition_reason=(
            "broker returned working status" if working else "broker status defaulted to accepted"
        ),
    )
