from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.broker.trading212_mappers import (
    map_trading212_account_summary_to_snapshot,
    map_trading212_history_order_to_snapshot,
    map_trading212_order_response_to_snapshot,
    map_trading212_pending_order_to_snapshot,
)


def test_account_summary_nested_cash_maps_to_broker_snapshot() -> None:
    summary = {
        "cash": {
            "availableToTrade": 5000,
            "blockedForPendingOrders": 300,
        },
        "total": 10000,
        "currencyCode": "GBP",
    }

    snapshot = map_trading212_account_summary_to_snapshot(summary)

    assert snapshot.broker == "trading212"
    assert snapshot.environment == "demo"
    assert snapshot.currency == "GBP"
    assert snapshot.cash == Decimal("5300")
    assert snapshot.free_funds == Decimal("5000")
    assert snapshot.blocked_funds == Decimal("300")
    assert snapshot.total_value == Decimal("10000")
    assert snapshot.raw == summary
    assert snapshot.raw is not summary


def test_pending_order_real_qa_shape_maps_to_broker_snapshot() -> None:
    order = {
        "id": 48950280036,
        "strategy": "QUANTITY",
        "type": "MARKET",
        "ticker": "AAPL_US_EQ",
        "quantity": 0.01,
        "filledQuantity": 0,
        "status": "NEW",
        "currency": "GBP",
        "extendedHours": False,
        "initiatedFrom": "API",
        "side": "BUY",
        "createdAt": "2026-05-15T00:55:01.053+03:00",
        "instrument": {
            "ticker": "AAPL_US_EQ",
            "name": "Apple",
            "isin": "US0378331005",
            "currency": "USD",
        },
    }

    snapshot = map_trading212_pending_order_to_snapshot(order)

    assert snapshot.broker == "trading212"
    assert snapshot.environment == "demo"
    assert snapshot.broker_order_id == "48950280036"
    assert snapshot.ticker == "AAPL_US_EQ"
    assert snapshot.status == "NEW"
    assert snapshot.side == "BUY"
    assert snapshot.order_type == "MARKET"
    assert snapshot.quantity == Decimal("0.01")
    assert snapshot.filled_quantity == Decimal("0")
    assert snapshot.average_fill_price is None
    assert snapshot.currency == "GBP"
    assert snapshot.created_at == datetime.fromisoformat("2026-05-15T00:55:01.053+03:00")
    assert snapshot.filled_at is None


def test_history_order_nested_filled_shape_maps_to_broker_snapshot() -> None:
    item = {
        "fill": {
            "filledAt": "2026-05-13T13:30:00.000Z",
            "id": 48900510985,
            "price": 293.69,
            "quantity": 0.01,
        },
        "order": {
            "id": 48850886521,
            "status": "FILLED",
            "filledQuantity": 0.01,
            "ticker": "AAPL_US_EQ",
            "type": "MARKET",
        },
    }

    snapshot = map_trading212_history_order_to_snapshot(item)

    assert snapshot.broker_order_id == "48850886521"
    assert snapshot.ticker == "AAPL_US_EQ"
    assert snapshot.status == "FILLED"
    assert snapshot.order_type == "MARKET"
    assert snapshot.quantity == Decimal("0.01")
    assert snapshot.filled_quantity == Decimal("0.01")
    assert snapshot.average_fill_price == Decimal("293.69")
    assert snapshot.filled_at == datetime(2026, 5, 13, 13, 30, tzinfo=UTC)
    assert snapshot.created_at is None


def test_order_response_shape_from_controlled_placement_maps_to_snapshot() -> None:
    response = {
        "id": "DEMO-ROUTE-ORDER-1",
        "ticker": "AAPL_US_EQ",
        "status": "WORKING",
        "filledQuantity": 0,
        "filledPrice": 0,
        "type": "MARKET",
    }

    snapshot = map_trading212_order_response_to_snapshot(response, environment="demo")

    assert snapshot.broker_order_id == "DEMO-ROUTE-ORDER-1"
    assert snapshot.ticker == "AAPL_US_EQ"
    assert snapshot.status == "WORKING"
    assert snapshot.order_type == "MARKET"
    assert snapshot.filled_quantity == Decimal("0")
    assert snapshot.average_fill_price == Decimal("0")


def test_missing_optional_and_malformed_values_map_to_none_safely() -> None:
    snapshot = map_trading212_pending_order_to_snapshot(
        {
            "id": 123,
            "quantity": "not-a-number",
            "filledQuantity": "",
            "createdAt": "not-a-date",
            "instrument": {"ticker": "MSFT_US_EQ", "currency": "USD"},
        }
    )

    assert snapshot.broker_order_id == "123"
    assert snapshot.ticker == "MSFT_US_EQ"
    assert snapshot.currency == "USD"
    assert snapshot.quantity is None
    assert snapshot.filled_quantity is None
    assert snapshot.created_at is None
    assert snapshot.status is None
    assert snapshot.side is None
    assert snapshot.order_type is None


def test_missing_required_broker_order_id_raises() -> None:
    with pytest.raises(ValueError, match="broker_order_id"):
        map_trading212_pending_order_to_snapshot({"ticker": "AAPL_US_EQ"})


def test_datetime_parsing_handles_z_and_timezone_offsets() -> None:
    history_snapshot = map_trading212_history_order_to_snapshot(
        {
            "order": {"id": "history-1"},
            "fill": {"filledAt": "2026-05-13T13:30:00.000Z"},
        }
    )
    pending_snapshot = map_trading212_pending_order_to_snapshot(
        {
            "id": "pending-1",
            "createdAt": "2026-05-15T00:55:01.053+03:00",
        }
    )

    assert history_snapshot.filled_at == datetime(2026, 5, 13, 13, 30, tzinfo=UTC)
    assert pending_snapshot.created_at is not None
    assert pending_snapshot.created_at.utcoffset() == timedelta(hours=3)


def test_snapshot_mappers_are_pure_transformations() -> None:
    import app.broker.trading212_mappers as mappers

    source = inspect.getsource(mappers)

    assert "Trading212Adapter" not in source
    assert "httpx" not in source
    assert "sqlalchemy" not in source
    assert "requests" not in source
