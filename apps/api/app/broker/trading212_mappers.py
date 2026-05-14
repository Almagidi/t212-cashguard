"""Pure Trading 212 payload mappers for broker-neutral snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.broker.snapshots import BrokerAccountSnapshot, BrokerOrderSnapshot

Path = str | tuple[str, ...]

_BROKER = "trading212"


def map_trading212_account_summary_to_snapshot(
    summary: Mapping[str, Any],
    *,
    environment: str = "demo",
) -> BrokerAccountSnapshot:
    cash_raw = summary.get("cash")
    blocked_funds: Decimal | None = None
    free_funds: Decimal | None = None
    cash: Decimal | None

    if isinstance(cash_raw, Mapping):
        blocked_funds = _first_decimal(
            cash_raw,
            "blockedForPendingOrders",
            "blocked",
            "reserved",
        )
        free_funds = _first_decimal(
            cash_raw,
            "availableToTrade",
            "free",
            "available",
        )
        cash = _first_decimal(cash_raw, "total", "cashTotal", "totalCash")
        if cash is None:
            cash = _sum_decimals(free_funds, blocked_funds, _first_decimal(cash_raw, "inPies"))
    else:
        cash = _to_decimal(cash_raw)
        free_funds = _first_decimal(summary, "free", "availableToTrade")
        if free_funds is None:
            free_funds = cash
        blocked_funds = _first_decimal(summary, "blockedForPendingOrders", "blocked", "reserved")

    return BrokerAccountSnapshot(
        broker=_BROKER,
        environment=environment,
        currency=_first_text(summary, "currency", "currencyCode"),
        cash=cash,
        free_funds=free_funds,
        blocked_funds=blocked_funds,
        total_value=_first_decimal(summary, "total", "totalValue"),
        raw=dict(summary),
    )


def map_trading212_pending_order_to_snapshot(
    order: Mapping[str, Any],
    *,
    environment: str = "demo",
) -> BrokerOrderSnapshot:
    return _map_order_payload(
        order,
        environment=environment,
        id_paths=("id", "orderId", "brokerOrderId", "broker_order_id"),
        ticker_paths=("ticker", ("instrument", "ticker")),
        status_paths=("status",),
        side_paths=("side",),
        order_type_paths=("type", "orderType"),
        quantity_paths=("quantity",),
        filled_quantity_paths=("filledQuantity", "filled_quantity"),
        average_fill_price_paths=("filledPrice", "avgFillPrice", "averageFillPrice"),
        currency_paths=("currency", ("instrument", "currency")),
        created_at_paths=("createdAt", "created"),
        filled_at_paths=("filledAt",),
    )


def map_trading212_history_order_to_snapshot(
    item: Mapping[str, Any],
    *,
    environment: str = "demo",
) -> BrokerOrderSnapshot:
    return _map_order_payload(
        item,
        environment=environment,
        id_paths=(("order", "id"), "id", "orderId", "brokerOrderId", "broker_order_id"),
        ticker_paths=(
            "ticker",
            ("instrument", "ticker"),
            ("order", "ticker"),
            ("order", "instrument", "ticker"),
        ),
        status_paths=("status", ("order", "status")),
        side_paths=("side", ("order", "side")),
        order_type_paths=("type", "orderType", ("order", "type"), ("order", "orderType")),
        quantity_paths=("quantity", ("fill", "quantity"), ("order", "quantity")),
        filled_quantity_paths=("filledQuantity", ("order", "filledQuantity"), ("fill", "quantity")),
        average_fill_price_paths=(
            "filledPrice",
            "avgFillPrice",
            "averageFillPrice",
            ("fill", "price"),
        ),
        currency_paths=(
            "currency",
            ("instrument", "currency"),
            ("order", "currency"),
            ("order", "instrument", "currency"),
        ),
        created_at_paths=("createdAt", "created", ("order", "createdAt")),
        filled_at_paths=("filledAt", ("fill", "filledAt")),
    )


def map_trading212_order_response_to_snapshot(
    response: Mapping[str, Any],
    *,
    environment: str = "demo",
) -> BrokerOrderSnapshot:
    return _map_order_payload(
        response,
        environment=environment,
        id_paths=("id", "orderId", "brokerOrderId", "broker_order_id"),
        ticker_paths=("ticker", ("instrument", "ticker")),
        status_paths=("status",),
        side_paths=("side",),
        order_type_paths=("type", "orderType"),
        quantity_paths=("quantity",),
        filled_quantity_paths=("filledQuantity", "filled_quantity"),
        average_fill_price_paths=("filledPrice", "avgFillPrice", "averageFillPrice"),
        currency_paths=("currency", ("instrument", "currency")),
        created_at_paths=("createdAt", "created"),
        filled_at_paths=("filledAt",),
    )


def _map_order_payload(
    payload: Mapping[str, Any],
    *,
    environment: str,
    id_paths: tuple[Path, ...],
    ticker_paths: tuple[Path, ...],
    status_paths: tuple[Path, ...],
    side_paths: tuple[Path, ...],
    order_type_paths: tuple[Path, ...],
    quantity_paths: tuple[Path, ...],
    filled_quantity_paths: tuple[Path, ...],
    average_fill_price_paths: tuple[Path, ...],
    currency_paths: tuple[Path, ...],
    created_at_paths: tuple[Path, ...],
    filled_at_paths: tuple[Path, ...],
) -> BrokerOrderSnapshot:
    broker_order_id = _first_text(payload, *id_paths)
    if broker_order_id is None:
        raise ValueError("Trading 212 snapshot payload is missing broker_order_id")

    return BrokerOrderSnapshot(
        broker=_BROKER,
        environment=environment,
        broker_order_id=broker_order_id,
        ticker=_first_text(payload, *ticker_paths),
        status=_first_text(payload, *status_paths),
        side=_first_text(payload, *side_paths),
        order_type=_first_text(payload, *order_type_paths),
        quantity=_first_decimal(payload, *quantity_paths),
        filled_quantity=_first_decimal(payload, *filled_quantity_paths),
        average_fill_price=_first_decimal(payload, *average_fill_price_paths),
        currency=_first_text(payload, *currency_paths),
        created_at=_first_datetime(payload, *created_at_paths),
        filled_at=_first_datetime(payload, *filled_at_paths),
        raw=dict(payload),
    )


def _nested_value(item: Mapping[str, Any], *path: str) -> Any:
    current: Any = item
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first_decimal(item: Mapping[str, Any], *paths: Path) -> Decimal | None:
    for path in paths:
        value = _nested_value(item, path) if isinstance(path, str) else _nested_value(item, *path)
        decimal_value = _to_decimal(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _first_text(item: Mapping[str, Any], *paths: Path) -> str | None:
    for path in paths:
        value = _nested_value(item, path) if isinstance(path, str) else _nested_value(item, *path)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _first_datetime(item: Mapping[str, Any], *paths: Path) -> datetime | None:
    for path in paths:
        value = _nested_value(item, path) if isinstance(path, str) else _nested_value(item, *path)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _sum_decimals(*values: Decimal | None) -> Decimal | None:
    total = Decimal("0")
    found = False
    for value in values:
        if value is None:
            continue
        total += value
        found = True
    return total if found else None
