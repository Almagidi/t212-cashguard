"""Trading 212 demo order route boundary tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.broker.trading212 import Trading212Adapter
from app.core.config import settings
from app.db.models import AuditLog


@pytest.mark.asyncio
async def test_demo_order_route_blocks_by_default_before_broker_submission(
    client,
    auth_headers: dict,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", False)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    async def forbidden_account_summary(self):  # pragma: no cover - must not be reached
        raise AssertionError(
            "Broker account summary must not be called when demo order gate is closed"
        )

    async def forbidden_market_order(
        self, *args, **kwargs
    ):  # pragma: no cover - must not be reached
        raise AssertionError(
            "Broker order submission must not be called when demo order gate is closed"
        )

    monkeypatch.setattr(Trading212Adapter, "get_account_summary", forbidden_account_summary)
    monkeypatch.setattr(Trading212Adapter, "place_market_order", forbidden_market_order)

    response = await client.post(
        "/v1/orders",
        headers=auth_headers,
        json={
            "ticker": "AAPL",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "time_validity": "DAY",
        },
    )

    assert response.status_code == 403
    assert "T212_DEMO_ORDER_ENABLED=true" in response.json()["detail"]

    audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "demo_order_blocked_by_feature_gate")
        )
    ).scalar_one()

    assert audit.payload["ticker"] == "AAPL"
    assert audit.payload["broker_environment"] == "demo"
    assert audit.payload["feature_gate"] == "T212_DEMO_ORDER_ENABLED"
    assert audit.payload["feature_gate_enabled"] is False
    assert audit.payload["no_broker_order_sent"] is True
    assert "api_key" not in str(audit.payload).lower()
    assert "secret" not in str(audit.payload).lower()


@pytest.mark.asyncio
async def test_demo_order_route_returns_429_when_preflight_account_summary_is_rate_limited(
    client,
    auth_headers: dict,
    db,
    monkeypatch,
):
    from app.api.v1.routes import orders as orders_route
    from app.broker.trading212 import T212RateLimitError
    from app.db.models import AppSettings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    app_settings = await db.get(AppSettings, 1)
    if app_settings is None:
        app_settings = AppSettings(
            id=1,
            auto_trading_enabled=False,
            kill_switch_active=False,
        )
        db.add(app_settings)
    else:
        app_settings.auto_trading_enabled = False
        app_settings.kill_switch_active = False
    await db.commit()

    class RateLimitedBroker:
        environment = "demo"
        base_url = "https://demo.trading212.com"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_account_summary(self):
            raise T212RateLimitError(4.0)

        async def place_market_order(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("Order submission must not be called after preflight rate limit")

    async def fake_get_broker(*args, **kwargs):
        return RateLimitedBroker()

    monkeypatch.setattr(orders_route, "get_broker", fake_get_broker)

    response = await client.post(
        "/v1/orders",
        headers=auth_headers,
        json={
            "ticker": "AAPL_US_EQ",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "time_validity": "DAY",
        },
    )

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "broker_rate_limited"


@pytest.mark.asyncio
async def test_demo_order_route_uses_normalised_trading212_cash_for_risk_checks(
    client,
    auth_headers: dict,
    db,
    monkeypatch,
):
    from app.api.v1.routes import orders as orders_route
    from app.db.models import AppSettings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    app_settings = await db.get(AppSettings, 1)
    if app_settings is None:
        app_settings = AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=False,
        )
        db.add(app_settings)
    else:
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
    await db.commit()

    class BrokerWithNestedTrading212Cash:
        environment = "demo"
        base_url = "https://demo.trading212.com"

        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_account_summary(self):
            return {
                "cash": {
                    "availableToTrade": 5000.0,
                    "blockedForPendingOrders": 0.0,
                },
                "invested": 0.0,
                "result": 0.0,
                "total": 5000.0,
                "currencyCode": "GBP",
            }

        async def place_market_order(self, ticker, quantity, time_validity="DAY"):
            self.calls.append((ticker, quantity, time_validity))
            return {
                "id": "DEMO-ROUTE-ORDER-1",
                "status": "WORKING",
                "filledQuantity": 0,
                "filledPrice": 0,
            }

    broker = BrokerWithNestedTrading212Cash()

    async def fake_get_broker(*args, **kwargs):
        return broker

    monkeypatch.setattr(orders_route, "get_broker", fake_get_broker)

    response = await client.post(
        "/v1/orders",
        headers=auth_headers,
        json={
            "ticker": "AAPL_US_EQ",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "time_validity": "DAY",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ticker"] == "AAPL_US_EQ"
    assert body["execution_environment"] == "demo"
    assert body["is_dry_run"] is False
    assert body["broker_order_id"] == "DEMO-ROUTE-ORDER-1"
    assert broker.calls
