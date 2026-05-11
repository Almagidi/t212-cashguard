from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AppSettings, AuditLog, Order


@pytest.mark.asyncio
async def test_policy_rejects_unknown_app_mode(monkeypatch):
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

    monkeypatch.setattr(settings, "APP_MODE", "sandbox")

    with pytest.raises(SafetyPolicyViolation, match="APP_MODE is not recognized"):
        require_broker_environment("demo", action="unit_test")


@pytest.mark.asyncio
async def test_policy_blocks_paper_mode_broker_calls(monkeypatch):
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

    monkeypatch.setattr(settings, "APP_MODE", "paper")

    with pytest.raises(SafetyPolicyViolation, match="APP_MODE=paper"):
        require_broker_environment("demo", action="unit_test")


@pytest.mark.asyncio
async def test_trading212_adapter_requires_demo_credentials_in_demo_mode(monkeypatch):
    from app.broker.trading212 import Trading212Adapter
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")

    with pytest.raises(SafetyPolicyViolation, match="demo credentials"):
        Trading212Adapter("", "", "demo")


@pytest.mark.asyncio
async def test_demo_adapter_uses_demo_url_even_when_live_credentials_exist(monkeypatch):
    from app.broker.trading212 import Trading212Adapter
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret")

    adapter = Trading212Adapter("demo-key", "demo-secret", "demo")

    assert adapter.base_url == "https://demo.trading212.com"
    assert adapter.base_url != "https://live.trading212.com"
    assert adapter.api_key == "demo-key"


@pytest.mark.asyncio
async def test_demo_adapter_http_request_uses_demo_url_and_fails_on_live(monkeypatch):
    from app.broker.trading212 import Trading212Adapter
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    seen_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_urls.append(url)
        if "live.trading212.com" in url:
            raise AssertionError(f"live Trading 212 endpoint was called: {url}")
        assert url.startswith("https://demo.trading212.com/")
        return httpx.Response(200, json={"id": "DEMO-NETWORK-1", "status": "WORKING"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        response = await adapter.place_market_order("AAPL", Decimal("1"))

    assert response["id"] == "DEMO-NETWORK-1"
    assert seen_urls == ["https://demo.trading212.com/api/v0/equity/orders/market"]


@pytest.mark.asyncio
async def test_get_broker_missing_demo_credentials_fails_safely(db, monkeypatch):
    from fastapi import HTTPException

    from app.api.deps import get_broker
    from app.core.config import settings
    from app.db.models import User

    user = (await db.execute(select(User))).scalar_one_or_none()
    if user is None:
        user = User(id=uuid.uuid4(), email="admin@test.com", hashed_password="x", is_admin=True)
        db.add(user)
        await db.flush()

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret")

    with pytest.raises(HTTPException) as exc:
        await get_broker(current_user=user, db=db)

    assert exc.value.status_code == 400
    assert "demo credentials" in str(exc.value.detail).lower()
    assert "live" not in str(exc.value.detail).lower()


class CapturingDemoBroker:
    environment = "demo"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, Decimal]] = []

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.calls.append(("market", ticker, quantity))
        if self.fail:
            raise RuntimeError("broker failed without secret material")
        return {
            "id": "DEMO-ORDER-1",
            "status": "FILLED",
            "filledQuantity": abs(float(quantity)),
            "filledPrice": 101.0,
        }


@pytest.mark.asyncio
async def test_demo_order_submission_is_audited_as_demo_attempt_and_success(db, monkeypatch):
    from app.core.config import settings
    from app.execution.engine import ExecutionEngine

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    await db.flush()

    broker = CapturingDemoBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )

    order = await engine.submit_order(order)

    assert order.execution_environment == "demo"
    assert order.broker_order_id == "DEMO-ORDER-1"
    assert broker.calls == [("market", "AAPL", Decimal("1"))]
    audits = (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
    actions = [audit.action for audit in audits]
    assert "demo_broker_order_attempt" in actions
    assert "demo_broker_order_success" in actions
    attempt = next(audit for audit in audits if audit.action == "demo_broker_order_attempt")
    assert attempt.payload["mode"] == "demo"
    assert attempt.payload["broker_environment"] == "demo"
    assert attempt.payload["no_broker_order_sent"] is False
    assert "api_key" not in str(attempt.payload).lower()


@pytest.mark.asyncio
async def test_demo_broker_failure_is_audited_without_duplicate_submit_or_secrets(db, monkeypatch):
    from app.core.config import settings
    from app.execution.engine import ExecutionEngine

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    await db.flush()

    broker = CapturingDemoBroker(fail=True)
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        is_dry_run=False,
    )

    order = await engine.submit_order(order)

    assert order.status == "error"
    assert len(broker.calls) == 1
    audits = (await db.execute(select(AuditLog))).scalars().all()
    failures = [audit for audit in audits if audit.action == "demo_broker_order_failure"]
    assert len(failures) == 1
    assert failures[0].payload["broker_environment"] == "demo"
    assert "secret" not in str(failures[0].payload).lower()


@pytest.mark.asyncio
async def test_kill_switch_blocks_demo_submit_before_broker_call(db, monkeypatch):
    from app.core.config import settings
    from app.execution.engine import ExecutionEngine
    from app.services.safety_policy import SafetyPolicyViolation

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=True))
    await db.flush()

    broker = CapturingDemoBroker()
    engine = ExecutionEngine(db, broker)
    order = Order(
        id=uuid.uuid4(),
        client_order_key="demo-kill-switch-block",
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        status="pending_intent",
        is_dry_run=False,
        execution_environment="demo",
    )
    db.add(order)
    await db.flush()

    with pytest.raises(SafetyPolicyViolation, match="Kill switch"):
        await engine.submit_order(order)

    assert broker.calls == []
    audit = (await db.execute(select(AuditLog))).scalar_one()
    assert audit.action == "demo_order_blocked_by_kill_switch"
    assert audit.payload["no_broker_order_sent"] is True
