"""Synthetic expected-safety probes; failures are findings, never a release pass."""

import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, BrokerConnection, Order, User
from app.services.live_readiness import LiveReadinessService
from app.services.safety_policy import SafetyPolicyViolation, require_order_submission_allowed
from app.services.telegram_control import TelegramControlService
from tests import network_guard


async def world(db, monkeypatch):
    # In-memory monkeypatch only: no real account, configuration, key or endpoint.
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(
        TelegramControlService,
        "status_payload",
        staticmethod(
            lambda: {
                "bot_configured": True,
                "alert_chat_configured": True,
                "webhook_secret_configured": True,
                "control_enabled": True,
            }
        ),
    )
    now = datetime.now(UTC)
    users = [
        User(
            id=uuid.uuid4(),
            email=f"fixture-{n}@example.invalid",
            hashed_password="unused",
            is_active=True,
            is_admin=True,
        )
        for n in ["a", "b"]
    ]
    db.add_all(users)
    evidence = {
        k: now.isoformat()
        for k in [
            "demo_validated_at",
            "broker_test_verified_at",
            "telegram_test_verified_at",
            "kill_switch_tested_at",
            "live_unlock_acknowledged_at",
        ]
    }
    db.add(
        AppSettings(
            id=1,
            kill_switch_active=False,
            auto_trading_enabled=False,
            live_trading_unlocked=True,
            extra={"live_readiness": evidence},
        )
    )
    old = BrokerConnection(
        id=uuid.uuid4(),
        user_id=users[0].id,
        broker="trading212",
        environment="live",
        api_key_encrypted="unused",
        api_secret_encrypted="unused",
        is_active=True,
        last_test_at=now - timedelta(days=2),
        last_test_ok=False,
        account_id="fixture-account-a",
        account_currency="USD",
        updated_at=now - timedelta(minutes=10),
    )
    newer = BrokerConnection(
        id=uuid.uuid4(),
        user_id=users[1].id,
        broker="trading212",
        environment="live",
        api_key_encrypted="unused",
        api_secret_encrypted="unused",
        is_active=True,
        last_test_at=now,
        last_test_ok=True,
        account_id="fixture-account-b",
        account_currency="USD",
        updated_at=now,
    )
    db.add_all([old, newer])
    await db.commit()
    return users, old, newer


def test_no_egress_guard_negative_control():
    with network_guard.ATTEMPTS.expected(), pytest.raises(network_guard.ExternalNetworkBlocked):
        socket.getaddrinfo("example.invalid", 443)


async def test_readiness_must_not_use_another_owners_fresh_broker(db, monkeypatch):
    users, old, newer = await world(db, monkeypatch)
    status = await LiveReadinessService(db).evaluate()
    # Matches the authenticated dependency's actual owner/environment selection.
    selected = (
        await db.execute(
            select(BrokerConnection).where(
                BrokerConnection.user_id == users[0].id,
                BrokerConnection.is_active.is_(True),
                BrokerConnection.environment == "live",
            )
        )
    ).scalar_one()
    assert selected.id == old.id and selected.last_test_ok is False
    assert newer.user_id != selected.user_id
    assert status["ready_for_live"] is False, (
        "Global readiness accepted another owner/account while selected owner broker test fails/stales"
    )


async def test_final_live_submission_gate_must_bind_readiness_to_actual_account(db, monkeypatch):
    await world(db, monkeypatch)
    order = Order(
        id=uuid.uuid4(),
        ticker="FIXTURE_US_EQ",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        limit_price=Decimal("10"),
        is_dry_run=False,
        execution_environment="live",
        broker_account_scope="fixture-account-a",
        status="pending_intent",
        client_order_key="synthetic-probe",
    )
    with pytest.raises(SafetyPolicyViolation):
        await require_order_submission_allowed(db, order=order, broker_environment="live")


def test_live_auto_provider_must_not_accept_a_mock_fallback(monkeypatch):
    import secrets

    from app.market_data import get_live_provider
    from app.services.startup_validation import assert_startup_safe

    values = {
        "APP_MODE": "live",
        "LIVE_TRADING_ENABLED": True,
        "T212_ENVIRONMENT": "live",
        "MARKET_DATA_PROVIDER": "auto",
        "MOCK_MARKET_PROFILE": "default",
        "ALPACA_API_KEY": "",
        "ALPACA_API_SECRET": "",
        "POLYGON_API_KEY": "",
        "SECRET_KEY": secrets.token_hex(32),
        "MASTER_KEY": secrets.token_hex(32),
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "T212_LIVE_API_KEY": secrets.token_urlsafe(24),
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    refused = False
    try:
        assert_startup_safe()
    except RuntimeError:
        refused = True
    assert refused, (
        "Live startup validator accepted auto provider with no real data credentials, resolving to mock"
    )
    with pytest.raises(RuntimeError):
        get_live_provider()
