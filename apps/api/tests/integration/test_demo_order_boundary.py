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
