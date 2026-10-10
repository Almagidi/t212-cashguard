"""Manual-order API behaviour when the broker's answer to a submission is lost."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, Order, OrderSubmissionAttempt

ORDER = {
    "ticker": "AAPL_US_EQ",
    "side": "buy",
    "order_type": "market",
    "quantity": "0.01",
    "time_validity": "DAY",
}


class LostResponseBroker:
    environment = "demo"
    base_url = "https://demo.trading212.com"

    def __init__(self):
        self.posts = []
        self.cancel_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_account_summary(self):
        return {
            "cash": {"availableToTrade": 5000.0, "blockedForPendingOrders": 0.0},
            "invested": 0.0,
            "result": 0.0,
            "total": 5000.0,
            "currencyCode": "GBP",
        }

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.posts.append((ticker, quantity))
        raise httpx.ReadTimeout("response lost after the broker accepted the order")

    async def cancel_order(self, broker_order_id):
        self.cancel_calls.append(broker_order_id)


@pytest_asyncio.fixture
async def lost_response_broker(db, monkeypatch):
    from app.api.deps import get_broker
    from app.api.v1.routes import orders as orders_route
    from app.main import app

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_ORDER_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    app_settings = await db.get(AppSettings, 1)
    if app_settings is None:
        db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    else:
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
    await db.commit()

    broker = LostResponseBroker()

    async def fake_get_broker(*args, **kwargs):
        return broker

    monkeypatch.setattr(orders_route, "get_broker", fake_get_broker)
    app.dependency_overrides[get_broker] = lambda: broker
    yield broker
    app.dependency_overrides.pop(get_broker, None)


@pytest.mark.asyncio
async def test_lost_broker_response_is_reported_as_unknown_not_as_failure(
    client, auth_headers: dict, db, lost_response_broker
):
    response = await client.post("/v1/orders", headers=auth_headers, json=ORDER)

    # 202: dispatched and recorded, but not confirmed as placed.
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "submission_unknown"
    assert body["broker_order_id"] is None
    assert "response lost" not in response.text
    assert len(lost_response_broker.posts) == 1
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "ambiguous"


@pytest.mark.asyncio
async def test_repeating_the_request_does_not_send_a_second_order(
    client, auth_headers: dict, db, lost_response_broker
):
    first = await client.post("/v1/orders", headers=auth_headers, json=ORDER)
    second = await client.post("/v1/orders", headers=auth_headers, json=ORDER)

    assert first.status_code == 202
    # The unknown order is still active, so the risk gate refuses a second one outright.
    assert second.status_code == 422
    assert "Duplicate order blocked" in second.json()["detail"]
    assert len(lost_response_broker.posts) == 1
    assert len((await db.execute(select(Order))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_cancelling_an_unknown_submission_reports_that_reconciliation_is_needed(
    client, auth_headers: dict, db, lost_response_broker
):
    created = await client.post("/v1/orders", headers=auth_headers, json=ORDER)
    order_id = created.json()["id"]

    response = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth_headers)

    assert response.status_code == 502
    assert response.json() == {
        "cancelled": False,
        "order_id": order_id,
        "status": "submission_unknown",
        "requires_reconciliation": True,
    }
    assert lost_response_broker.cancel_calls == []
    order = (await db.execute(select(Order))).scalar_one()
    assert order.status == "submission_unknown"
    assert order.cancelled_at is None


@pytest.mark.asyncio
async def test_unknown_submission_is_listed_with_its_status(
    client, auth_headers: dict, lost_response_broker
):
    await client.post("/v1/orders", headers=auth_headers, json=ORDER)

    listing = await client.get("/v1/orders", headers=auth_headers)

    assert listing.status_code == 200
    payload = listing.json()
    orders = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
    assert [order["status"] for order in orders] == ["submission_unknown"]


@pytest.mark.asyncio
async def test_duplicate_that_slips_past_the_risk_gate_is_refused_not_resent(
    client, auth_headers: dict, db, lost_response_broker, monkeypatch
):
    from app.risk.engine import RiskEngine

    first = await client.post("/v1/orders", headers=auth_headers, json=ORDER)
    assert first.status_code == 202

    # Simulate the race in which a second identical request has already passed the
    # risk gate's duplicate check when the first one is recorded.
    async def no_duplicate_check(self, *args, **kwargs):
        return None

    monkeypatch.setattr(RiskEngine, "check_duplicate_order", no_duplicate_check)
    second = await client.post("/v1/orders", headers=auth_headers, json=ORDER)

    assert second.status_code == 409
    assert "submission_unknown" in second.json()["detail"]
    assert len(lost_response_broker.posts) == 1
    assert len((await db.execute(select(Order))).scalars().all()) == 1
