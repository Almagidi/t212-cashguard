"""What a broker's record of an order, read after it was placed, may be taken to mean.

Reconciliation asks the broker about an order by its exact id: first among the open
orders and, when the broker no longer lists it there, in the order history. Either way the
record is interpreted completely before anything is written. A final state (filled,
cancelled, rejected) is accepted only when the record is about this order (its id, and
whatever it says about instrument, side, type and quantity), does not contradict itself,
and states every number the state depends on in a usable form. Anything else is refused
with a reason, and the caller leaves the order as it is.

Two layouts are read. The flat one, ``{"id", "status", "filledQuantity", "filledPrice"}``,
is what the open-orders read answers with. The history may also nest the same facts as
``{"order": {...}, "fill": {...}}``. A sell may be reported with a negative quantity; the
amount is what is kept. Only these field names are read. A record that states a fact
under another name is taken not to state it; it is never guessed at.

Pure functions: no database, no broker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

from app.execution.acknowledgement import (
    SMALLEST_STORED_PRICE,
    finite_number,
    usable_order_id,
)

# Why nothing was settled when the broker has no record of the order at all: not among its
# open orders, and not in the order history that was read.
NO_BROKER_RECORD = "no_broker_record"

_ZERO = Decimal("0")
# orders.quantity and orders.filled_quantity are Numeric(20, 8). Amounts are compared at
# that scale, so a number the broker writes as 0.30000000000000004 is the 0.3 that was sent.
_AMOUNT_STEP = Decimal("0.00000001")
# orders.avg_fill_price is Numeric(20, 8): twelve digits before the point, and no more.
PRICE_TOO_LARGE_TO_STORE = Decimal("1000000000000")
_FINAL_STATUSES = {"FILLED": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected"}
_ORDER_TYPES = {"MARKET": "market", "LIMIT": "limit", "STOP": "stop", "STOP_LIMIT": "stop_limit"}

_Path = tuple[str, ...]
_ID: tuple[_Path, ...] = (("order", "id"), ("id",), ("orderId",))
_STATUS: tuple[_Path, ...] = (("order", "status"), ("status",))
_TICKER: tuple[_Path, ...] = (("order", "ticker"), ("ticker",), ("order", "instrument", "ticker"))
_SIDE: tuple[_Path, ...] = (("order", "side"), ("side",))
_TYPE: tuple[_Path, ...] = (("order", "type"), ("type",))
_QUANTITY: tuple[_Path, ...] = (("order", "quantity"), ("quantity",), ("orderedQuantity",))
_FILLED: tuple[_Path, ...] = (("order", "filledQuantity"), ("filledQuantity",))
_ONE_FILL: tuple[_Path, ...] = (("fill", "quantity"),)
_PRICE: tuple[_Path, ...] = (("fill", "price"), ("filledPrice",), ("fillPrice",))
_FILLED_AT: tuple[_Path, ...] = (("fill", "filledAt"), ("dateExecuted",))


# Refusals that say the record is about another order (or cannot be told apart from one),
# as against refusals of a number in a record that is about this order.
ABOUT_ANOTHER_ORDER = frozenset(
    {
        "not_an_object",
        "order_id_differs",
        "ticker_differs",
        "side_differs",
        "order_type_differs",
        "quantity_differs",
        "quantity_not_a_number",
        "quantity_sign_contradicts_side",
        "quantity_too_large",
        "record_contradicts_itself",
    }
)


class UntrustedRecord(Exception):
    """The broker's record cannot be relied on to say what became of the order.

    ``reason`` is a fixed word, safe to store. ``lacks_detail`` is true only for an
    open-orders answer that is about this order and final but leaves out a number, so
    that the order history may still supply it; ``broker_status`` and ``filled_quantity``
    then say what that answer did state, and the history has to agree with it. It is
    false when the record contradicts the order or itself, and no other read may be used
    to paper over that.
    """

    def __init__(
        self,
        reason: str,
        *,
        lacks_detail: bool = False,
        broker_status: str | None = None,
        filled_quantity: Decimal | None = None,
    ) -> None:
        self.reason = reason
        self.lacks_detail = lacks_detail
        self.broker_status = broker_status
        self.filled_quantity = filled_quantity
        super().__init__(f"Broker order record not trusted ({reason}).")

    @property
    def about_another_order(self) -> bool:
        return self.reason in ABOUT_ANOTHER_ORDER


@dataclass(frozen=True)
class LocalOrder:
    """What is known here about the order the record has to be about."""

    broker_order_id: str
    ticker: str
    side: str
    order_type: str
    quantity: Decimal


@dataclass(frozen=True)
class FinalRecord:
    """A final state that is safe to record.

    ``filled_quantity`` is an amount the record states, never negative. ``fill_price`` is
    set whenever the amount is above zero. ``filled_at`` is the broker's time of the last
    fill, when it gave one.
    """

    broker_status: str
    order_status: str
    filled_quantity: Decimal
    fill_price: Decimal | None
    filled_at: datetime | None


@dataclass(frozen=True)
class OpenRecord:
    """The broker lists the order among its open orders and does not call it final."""

    broker_status: str


@dataclass(frozen=True)
class _Reported:
    """One record's own statements, after the checks that it is about this order."""

    status: str
    filled: Decimal | None
    one_fill: Decimal | None
    price: Decimal | None
    filled_at: datetime | None


def as_amount(value: Decimal) -> Decimal:
    """An amount at the scale it is stored and compared at."""
    return value.quantize(_AMOUNT_STEP)


def _at(payload: Mapping[str, Any], path: _Path) -> Any:
    current: Any = payload
    for key in path:
        current = current.get(key) if isinstance(current, Mapping) else None
    return current


def _first(payload: Mapping[str, Any], paths: tuple[_Path, ...]) -> Any:
    """The first of these places in the record that says anything."""
    for path in paths:
        value = _at(payload, path)
        if value is not None:
            return value
    return None


def _stated(payload: Mapping[str, Any], paths: tuple[_Path, ...], *, number: bool = False) -> Any:
    """What the record states for one fact, which it may write in more than one place.

    Two places that state it differently make the record one that contradicts itself.
    Numbers are compared as numbers, text without regard to case or surrounding space.
    """
    values = [value for value in (_at(payload, path) for path in paths) if value is not None]
    if len(values) > 1:
        seen: set[Any] = set()
        for value in values:
            as_number = finite_number(value) if number else None
            if as_number is not None:
                try:
                    # A sell's amount may be written with its sign in one place and
                    # without it in another; that is the same amount.
                    as_number = as_number.copy_abs().quantize(_AMOUNT_STEP)
                except DecimalException:
                    as_number = None
            seen.add(as_number if as_number is not None else str(value).strip().upper())
        if len(seen) > 1:
            raise UntrustedRecord("record_contradicts_itself")
    return values[0] if values else None


def _amount(raw: Any, *, side: str, what: str) -> Decimal | None:
    """A reported quantity as an amount; None when the record says nothing. A negative
    number is how a sell is written, and is accepted for a sell and for nothing else."""
    if raw is None:
        return None
    signed = finite_number(raw)
    if signed is None:
        raise UntrustedRecord(f"{what}_not_a_number")
    if signed < 0 and side != "sell":
        raise UntrustedRecord(f"{what}_sign_contradicts_side")
    try:
        # copy_abs, not abs(): it cannot overflow, however large the number.
        return as_amount(signed.copy_abs())
    except DecimalException:
        raise UntrustedRecord(f"{what}_too_large") from None


def _when(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def require_same_order(payload: Any, order: LocalOrder) -> None:
    """Refuse a record that says it is about another order.

    Whatever the record states about the order's id, instrument, side, type and ordered
    quantity has to agree with ``order``. What it does not state is not held against it,
    and an order type this code does not know is not compared.
    """
    if not isinstance(payload, Mapping):
        raise UntrustedRecord("not_an_object")

    # The id is read from the first place that gives one: in the nested layout a record
    # may carry an id of its own beside the order's.
    raw_id = _first(payload, _ID)
    if raw_id is not None and usable_order_id(raw_id) != order.broker_order_id:
        raise UntrustedRecord("order_id_differs")
    ticker = _stated(payload, _TICKER)
    if ticker is not None and str(ticker).strip().upper() != order.ticker.upper():
        raise UntrustedRecord("ticker_differs")
    side = _stated(payload, _SIDE)
    if side is not None and str(side).strip().lower() != order.side:
        raise UntrustedRecord("side_differs")
    order_type = _ORDER_TYPES.get(str(_stated(payload, _TYPE) or "").strip().upper())
    if order_type is not None and order_type != order.order_type:
        raise UntrustedRecord("order_type_differs")
    ordered = _amount(_stated(payload, _QUANTITY, number=True), side=order.side, what="quantity")
    if ordered is not None and ordered != order.quantity:
        raise UntrustedRecord("quantity_differs")


def _read(payload: Any, order: LocalOrder) -> _Reported:
    """Check the record is about this order and take down what it reports."""
    require_same_order(payload, order)

    price: Decimal | None = None
    raw_price = _stated(payload, _PRICE, number=True)
    if raw_price is not None:
        price = finite_number(raw_price)
        if price is None:
            raise UntrustedRecord("fill_price_not_a_number")
        if price < _ZERO:
            raise UntrustedRecord("fill_price_not_positive")
        if price >= PRICE_TOO_LARGE_TO_STORE:
            raise UntrustedRecord("fill_price_too_large")

    one_fill = _amount(_first(payload, _ONE_FILL), side=order.side, what="filled_quantity")
    if one_fill is None and _at(payload, ("fill", "price")) is not None:
        # A fill that gives a price and not its own amount cannot be shown to be the
        # whole of what filled.
        raise UntrustedRecord("fill_without_its_quantity")

    status = _stated(payload, _STATUS)
    return _Reported(
        status=status.strip().upper() if isinstance(status, str) else "",
        filled=_amount(
            _stated(payload, _FILLED, number=True), side=order.side, what="filled_quantity"
        ),
        one_fill=one_fill,
        price=price,
        filled_at=_when(_first(payload, _FILLED_AT)),
    )


def _fill(parts: list[_Reported]) -> tuple[Decimal | None, Decimal | None]:
    """The filled amount and its price, from one record or from one record per fill."""
    totals = {part.filled for part in parts if part.filled is not None}
    if len(totals) > 1:
        raise UntrustedRecord("records_disagree_on_filled_quantity")
    total = next(iter(totals), None)

    if len(parts) == 1:
        part = parts[0]
        if total is not None and part.one_fill is not None and part.one_fill != total:
            # The record carries one fill of several: its price is not the order's.
            raise UntrustedRecord("fill_does_not_cover_filled_quantity")
        # A fill on its own is never taken for the total: other fills may exist.
        return total, part.price

    # Several records for one order are accepted only as its separate fills, and only when
    # they add up to the total the broker itself states.
    fills = [(part.one_fill, part.price) for part in parts]
    if total is None or any(amount is None or price is None for amount, price in fills):
        raise UntrustedRecord("several_records_without_their_fills")
    known = [(amount, price) for amount, price in fills if amount is not None and price is not None]
    if any(amount > _ZERO and price < SMALLEST_STORED_PRICE for amount, price in known):
        raise UntrustedRecord("fill_price_not_positive")
    # Amounts and prices are bounded by now (see _amount and _read), so this arithmetic
    # stays well inside what a Decimal holds.
    if sum((amount for amount, _ in known), _ZERO) != total:
        raise UntrustedRecord("fills_do_not_add_up")
    if total == _ZERO:
        return total, None
    paid = sum((amount * price for amount, price in known), _ZERO)
    return total, (paid / total).quantize(SMALLEST_STORED_PRICE)


def _judge(
    parts: list[_Reported], order: LocalOrder, *, from_history: bool
) -> FinalRecord | OpenRecord:
    statuses = {part.status for part in parts}
    if len(statuses) != 1:
        raise UntrustedRecord("records_disagree_on_status")
    broker_status = next(iter(statuses))
    order_status = _FINAL_STATUSES.get(broker_status)
    if order_status is None:
        # From the history this is refused by interpret_history: only the open-orders
        # read can say an order is still open.
        return OpenRecord(broker_status=broker_status)

    filled, price = _fill(parts)
    if filled is not None and filled > order.quantity:
        raise UntrustedRecord("filled_quantity_exceeds_order")

    # What an open-orders answer leaves out, the history may supply; what the history
    # leaves out is simply missing. Either way a final state is never recorded without an
    # explicit statement of how much filled: zero has to be said.
    may_be_completed = not from_history
    if filled is None:
        raise UntrustedRecord(
            "filled_quantity_missing", lacks_detail=may_be_completed, broker_status=broker_status
        )
    if order_status == "filled" and filled == _ZERO:
        raise UntrustedRecord("filled_without_a_fill")
    if order_status == "filled" and filled < order.quantity:
        raise UntrustedRecord("filled_quantity_below_order")

    if filled == _ZERO:
        price = None
    elif price is None:
        raise UntrustedRecord(
            "fill_price_missing",
            lacks_detail=may_be_completed,
            broker_status=broker_status,
            filled_quantity=filled,
        )
    elif price < SMALLEST_STORED_PRICE:
        raise UntrustedRecord("fill_price_not_positive")

    times = [part.filled_at for part in parts if part.filled_at is not None]
    return FinalRecord(
        broker_status=broker_status,
        order_status=order_status,
        filled_quantity=filled,
        fill_price=price,
        filled_at=max(times) if times else None,
    )


def interpret_open_order_read(payload: Any, *, order: LocalOrder) -> FinalRecord | OpenRecord:
    """Decide what the answer to "what is order <id>?" means for ``order``.

    Raises ``UntrustedRecord``. When ``lacks_detail`` is set on it, the order history may
    be asked instead.
    """
    return _judge([_read(payload, order)], order, from_history=False)


def history_items(history: Any) -> list[Mapping[str, Any]]:
    """The records of a history page, whether it came as ``{"items": [...]}`` or a list.

    Raises ``UntrustedRecord`` for an answer that is not a page of records at all: that
    is not the same as a page on which the order does not appear.
    """
    items = history.get("items") if isinstance(history, Mapping) else history
    if not isinstance(items, list):
        raise UntrustedRecord("history_not_understood")
    return [item for item in items if isinstance(item, Mapping)]


def has_older_records(history: Any, page_size: int) -> bool:
    """True when records older than this page may exist.

    The broker says so itself with ``nextPagePath`` when it sends one; otherwise a page
    as long as was asked for is taken to have more behind it.
    """
    if isinstance(history, Mapping) and "nextPagePath" in history:
        return bool(history["nextPagePath"])
    return len(history_items(history)) >= page_size


def records_of(history: Any, broker_order_id: str) -> list[Mapping[str, Any]]:
    """Every record on the page that carries exactly this broker order id."""
    return [
        item
        for item in history_items(history)
        if usable_order_id(_first(item, _ID)) == broker_order_id
    ]


def interpret_history(
    history: Any, *, order: LocalOrder
) -> tuple[FinalRecord, list[Mapping[str, Any]]] | None:
    """Decide what a history page says became of ``order``.

    ``None`` when the page has no record with the order's id. Otherwise the final state
    and the records it rests on. Raises ``UntrustedRecord`` when a record with the order's
    id cannot be relied on, which includes one that is not final.
    """
    found = records_of(history, order.broker_order_id)
    if not found:
        return None
    verdict = _judge([_read(item, order) for item in found], order, from_history=True)
    if not isinstance(verdict, FinalRecord):
        # A history record with any status but a final one, or none, says nothing this
        # code may act on.
        raise UntrustedRecord("history_record_not_final")
    return verdict, found


def require_reads_agree(incomplete: UntrustedRecord, record: FinalRecord) -> None:
    """Refuse a history record that tells another story than the open-orders answer did.

    The history is read to supply a number the answer left out. It may not change what
    the answer did state: the final status and, where given, how much filled.
    """
    if record.broker_status != incomplete.broker_status:
        raise UntrustedRecord("reads_disagree")
    if (
        incomplete.filled_quantity is not None
        and record.filled_quantity != incomplete.filled_quantity
    ):
        raise UntrustedRecord("reads_disagree")


def says_no_such_order(failure: BaseException) -> bool:
    """True when the open-orders read was answered with "there is no such order".

    That is an answer, not a failed read: the broker lists only orders that are still
    open, so an order that has ended is no longer found there.
    """
    return getattr(failure, "status_code", None) == 404
