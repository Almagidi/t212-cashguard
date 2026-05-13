from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.core.config import settings
from app.db.models import AuditLog, Order
from app.services.demo_order_reconciliation import DemoOrderReconciler
from app.services.safety_policy import SafetyPolicyViolation


def _demo_order(**overrides) -> Order:
    values = {
        "id": uuid.uuid4(),
        "client_order_key": f"demo-reconcile-{uuid.uuid4()}",
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("1"),
        "status": "accepted",
        "broker_order_id": "48850886521",
        "execution_environment": "demo",
        "is_dry_run": False,
    }
    values.update(overrides)
    return Order(**values)


class HistoryBroker:
    environment = "demo"

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self.response = response if response is not None else {"items": []}
        self.exc = exc
        self.history_calls: list[dict[str, object]] = []
        self.placement_calls = 0

    async def get_historical_orders(self, **kwargs):
        self.history_calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response

    async def place_market_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.placement_calls += 1
        raise AssertionError("reconciliation must not place broker orders")


async def _actions(db) -> list[str]:
    return [
        audit.action
        for audit in (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
    ]


@pytest.mark.asyncio
async def test_successful_demo_reconciliation_updates_order_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order()
    db.add(order)
    await db.flush()
    broker = HistoryBroker(
        {
            "items": [
                {
                    "id": 48850886521,
                    "status": "FILLED",
                    "filledQuantity": "1",
                    "filledPrice": "101.23",
                }
            ]
        }
    )

    result = await DemoOrderReconciler(db, broker).reconcile_order(order)

    assert result.matched is True
    assert result.broker_status == "FILLED"
    assert result.previous_status == "accepted"
    assert result.new_status == "filled"
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("1")
    assert order.avg_fill_price == Decimal("101.23")
    assert order.filled_at is not None
    assert order.last_reconciled_at is not None
    assert broker.history_calls == [{"limit": 50}]
    assert broker.placement_calls == 0
    actions = await _actions(db)
    assert "demo_order_reconciliation_attempt" in actions
    assert "demo_order_reconciliation_success" in actions


@pytest.mark.asyncio
async def test_nested_trading212_history_shape_updates_filled_demo_order(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order(quantity=Decimal("0.01"), ticker="AAPL_US_EQ")
    db.add(order)
    await db.flush()
    broker = HistoryBroker(
        {
            "items": [
                {
                    "fill": {
                        "filledAt": "2026-05-13T13:30:00.000Z",
                        "id": 48900510985,
                        "price": 293.69,
                        "quantity": 0.01,
                        "tradingMethod": "OTC",
                        "type": "TRADE",
                        "walletImpact": {
                            "currency": "GBP",
                            "fxRate": 1.34720183,
                            "netValue": 2.18,
                            "taxes": [],
                        },
                    },
                    "order": {
                        "createdAt": "2026-05-12T20:48:28.000Z",
                        "currency": "GBP",
                        "extendedHours": False,
                        "filledQuantity": 0.01,
                        "id": 48850886521,
                        "initiatedFrom": "API",
                        "instrument": {
                            "currency": "USD",
                            "isin": "US0378331005",
                            "name": "Apple",
                            "ticker": "AAPL_US_EQ",
                        },
                        "quantity": 0.01,
                        "side": "BUY",
                        "status": "FILLED",
                        "strategy": "QUANTITY",
                        "ticker": "AAPL_US_EQ",
                        "type": "MARKET",
                    },
                }
            ],
            "nextPagePath": None,
        }
    )

    result = await DemoOrderReconciler(db, broker).reconcile_order(order)

    assert result.matched is True
    assert result.broker_status == "FILLED"
    assert result.new_status == "filled"
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("0.01")
    assert order.avg_fill_price == Decimal("293.69")
    assert order.filled_at is not None
    assert order.filled_at.isoformat() == "2026-05-13T13:30:00+00:00"
    assert order.last_reconciled_at is not None
    assert broker.history_calls == [{"limit": 50}]
    assert broker.placement_calls == 0
    audits = (await db.execute(select(AuditLog))).scalars().all()
    assert "demo_order_reconciliation_success" in [audit.action for audit in audits]
    success = next(audit for audit in audits if audit.action == "demo_order_reconciliation_success")
    assert success.payload["broker_ticker"] == "AAPL_US_EQ"
    assert success.payload["no_broker_order_sent"] is True


@pytest.mark.asyncio
async def test_missing_broker_order_does_not_change_status_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order(status="accepted")
    db.add(order)
    await db.flush()

    result = await DemoOrderReconciler(
        db, HistoryBroker({"items": [{"id": "other"}]})
    ).reconcile_order(order)

    assert result.matched is False
    assert result.outcome == "missing"
    assert order.status == "accepted"
    assert "demo_order_reconciliation_missing" in await _actions(db)


@pytest.mark.asyncio
async def test_rate_limited_history_does_not_change_status_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order(status="accepted")
    db.add(order)
    await db.flush()

    result = await DemoOrderReconciler(
        db,
        HistoryBroker(exc=T212RateLimitError(7.0)),
    ).reconcile_order(order)

    assert result.matched is False
    assert result.outcome == "rate_limited"
    assert result.error_type == "T212RateLimitError"
    assert order.status == "accepted"
    assert "demo_order_reconciliation_rate_limited" in await _actions(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        T212AuthError(401, "secret token should not be stored"),
        T212APIError(502, {"error": "api_key must not be stored"}),
    ],
)
async def test_auth_or_api_failure_does_not_change_status_or_audit_secrets(
    db,
    monkeypatch,
    exc,
):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order(status="accepted")
    db.add(order)
    await db.flush()

    result = await DemoOrderReconciler(db, HistoryBroker(exc=exc)).reconcile_order(order)

    assert result.matched is False
    assert result.outcome == "failed"
    assert result.error_type == type(exc).__name__
    assert order.status == "accepted"
    audits = (await db.execute(select(AuditLog))).scalars().all()
    assert "demo_order_reconciliation_failed" in [audit.action for audit in audits]
    payload_text = str([audit.payload for audit in audits]).lower()
    assert "secret" not in payload_text
    assert "token" not in payload_text
    assert "api_key" not in payload_text


@pytest.mark.asyncio
async def test_unknown_broker_status_preserves_local_status_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order(status="accepted")
    db.add(order)
    await db.flush()
    broker = HistoryBroker({"items": [{"id": "48850886521", "status": "PARTIAL_WEIRD"}]})

    result = await DemoOrderReconciler(db, broker).reconcile_order(order)

    assert result.matched is True
    assert result.outcome == "unknown_status"
    assert result.broker_status == "PARTIAL_WEIRD"
    assert result.new_status == "accepted"
    assert order.status == "accepted"
    assert "demo_order_reconciliation_unknown_status" in await _actions(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_mode", "t212_environment", "broker_environment"),
    [
        ("mock", "demo", "demo"),
        ("demo", "live", "demo"),
        ("demo", "demo", "live"),
    ],
)
async def test_reconciliation_refuses_non_demo_boundaries(
    db,
    monkeypatch,
    app_mode,
    t212_environment,
    broker_environment,
):
    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", t212_environment)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order()
    db.add(order)
    await db.flush()
    broker = HistoryBroker({"items": [{"id": "48850886521", "status": "FILLED"}]})
    broker.environment = broker_environment

    with pytest.raises(SafetyPolicyViolation):
        await DemoOrderReconciler(db, broker).reconcile_order(order)

    assert broker.history_calls == []
    assert broker.placement_calls == 0
    assert order.status == "accepted"


@pytest.mark.asyncio
async def test_reconciliation_allows_live_trading_disabled_in_demo(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    order = _demo_order()
    db.add(order)
    await db.flush()

    result = await DemoOrderReconciler(
        db,
        HistoryBroker({"items": [{"id": "48850886521", "status": "WORKING"}]}),
    ).reconcile_order(order)

    assert result.matched is True
    assert result.new_status == "accepted"
