from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import AppSettings, AuditLog, BrokerConnection, Order, RiskEvent


@pytest.mark.asyncio
async def test_daily_reset_never_auto_resumes_auto_trading_after_kill_switch(db):
    from app.workers.tasks import run_daily_reset_once

    settings = AppSettings(
        id=1,
        auto_trading_enabled=False,
        kill_switch_active=True,
        live_trading_unlocked=False,
    )
    db.add(settings)
    db.add(
        RiskEvent(
            event_type="kill_switch_on",
            message="Kill switch activated by daily_loss_monitor",
            payload={"actor": "daily_loss_monitor"},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()

    summary = await run_daily_reset_once(db)

    assert summary["reset"] is True
    refreshed = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    assert refreshed.kill_switch_active is True
    assert refreshed.auto_trading_enabled is False
    audits = (await db.execute(select(AuditLog))).scalars().all()
    assert "daily_reset_manual_recovery_required" in {audit.action for audit in audits}
    assert "daily_reset_auto_enable" not in {audit.action for audit in audits}


@pytest.mark.asyncio
async def test_broker_policy_blocks_live_adapter_when_live_flag_disabled(monkeypatch):
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    with pytest.raises(SafetyPolicyViolation, match="LIVE_TRADING_ENABLED"):
        require_broker_environment("live", action="unit_test")


@pytest.mark.asyncio
async def test_broker_policy_blocks_demo_to_live_fallback(monkeypatch):
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    with pytest.raises(SafetyPolicyViolation, match="demo mode"):
        require_broker_environment("live", action="unit_test")


@pytest.mark.asyncio
async def test_trading212_adapter_uses_policy_and_blocks_live_in_mock_mode(monkeypatch):
    from app.broker.trading212 import Trading212Adapter
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    with pytest.raises(SafetyPolicyViolation, match="APP_MODE=mock"):
        Trading212Adapter("key", "secret", "live")


@pytest.mark.asyncio
async def test_trading212_adapter_rejects_unknown_environment(monkeypatch):
    from app.broker.trading212 import Trading212Adapter
    from app.core.config import settings
    from app.services.safety_policy import SafetyPolicyViolation

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    with pytest.raises(SafetyPolicyViolation, match="explicitly demo or live"):
        Trading212Adapter("key", "secret", "sandbox")


@pytest.mark.asyncio
async def test_get_broker_selects_connection_for_current_runtime_only(
    db,
    admin_token,
    monkeypatch,
):
    from app.api.deps import get_broker
    from app.core.config import settings
    from app.core.security import encrypt_field
    from app.db.models import User

    user = (await db.execute(select(User))).scalar_one()
    live_conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="trading212",
        environment="live",
        api_key_encrypted=encrypt_field("live-key"),
        api_secret_encrypted=encrypt_field("live-secret"),
        is_active=True,
    )
    demo_conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="trading212",
        environment="demo",
        api_key_encrypted=encrypt_field("demo-key"),
        api_secret_encrypted=encrypt_field("demo-secret"),
        is_active=True,
    )
    db.add_all([live_conn, demo_conn])
    await db.commit()

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    broker = await get_broker(current_user=user, db=db)

    assert broker.environment == "demo"


@pytest.mark.asyncio
async def test_execution_engine_blocks_submit_when_kill_switch_active(db):
    from app.execution.engine import ExecutionEngine
    from app.services.safety_policy import SafetyPolicyViolation

    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=True,
            live_trading_unlocked=False,
        )
    )
    order = Order(
        id=uuid.uuid4(),
        client_order_key="dry-run-kill-switch-block",
        ticker="SAFE",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        status="pending_intent",
        is_dry_run=True,
        execution_environment="dry_run",
        expected_fill_price=Decimal("100"),
    )
    db.add(order)
    await db.commit()

    engine = ExecutionEngine(db, broker=object())

    with pytest.raises(SafetyPolicyViolation, match="Kill switch"):
        await engine.submit_order(order)

    assert order.status == "pending_intent"
