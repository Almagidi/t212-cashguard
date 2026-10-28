"""Read-only operator summaries must use the authenticated account's readiness."""

from __future__ import annotations

import secrets
import uuid

import pytest

from app.api.v1.routes.operator import operator_status
from app.core.config import settings
from app.core.security import encrypt_field
from app.db.models import BrokerConnection, User, VenueConfig
from app.services.live_evidence import record_broker_test
from tests.integration.test_live_account_authorization import authorization_world, review_and_unlock


async def _world(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    monkeypatch.setattr(settings, "CASH_ONLY_MODE", True)
    db.add_all(
        [
            VenueConfig(venue="t212", kill_switch_active=False),
            VenueConfig(venue="kraken", kill_switch_active=False),
        ]
    )
    await db.commit()
    await review_and_unlock(db, world)
    return world


async def test_operator_possible_is_true_only_for_current_ready_owner(db, monkeypatch):
    world = await _world(db, monkeypatch)
    result = await operator_status(current_user=world.owner, db=db, audit_limit=20)
    assert result.trading212.live_readiness_status.ready_for_live is True
    assert result.live_trading_possible is True
    other = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.invalid",
        hashed_password="unused",
        is_admin=True,
        is_active=True,
    )
    db.add(other)
    await db.flush([other])
    conn = BrokerConnection(
        user_id=other.id,
        broker="trading212",
        environment="live",
        account_id="synthetic-other",
        api_key_encrypted=encrypt_field(secrets.token_urlsafe(24)),
        api_secret_encrypted=encrypt_field(secrets.token_urlsafe(24)),
        is_active=True,
    )
    db.add(conn)
    await db.flush()
    await record_broker_test(db, conn, success=True, actor=other.email)
    await db.commit()
    result = await operator_status(current_user=other, db=db, audit_limit=20)
    assert result.trading212.live_readiness_status.ready_for_live is False
    assert result.live_trading_possible is False


@pytest.mark.parametrize("mode,kill", [("live", True), ("mock", False), ("demo", False)])
async def test_operator_possible_is_false_when_mode_or_global_stop_blocks(
    db, monkeypatch, mode, kill
):
    world = await _world(db, monkeypatch)
    world.app.kill_switch_active = kill
    await db.commit()
    monkeypatch.setattr(settings, "APP_MODE", mode)
    result = await operator_status(current_user=world.owner, db=db, audit_limit=20)
    assert result.trading212.live_readiness_status.ready_for_live is False
    assert result.live_trading_possible is False


@pytest.mark.parametrize(
    "mismatch", ["owner", "broker", "environment", "inactive", "live_environment_fallback"]
)
async def test_operator_credentials_describe_selected_owner_and_supported_runtime_only(
    db, monkeypatch, mismatch
):
    world = await authorization_world(db, monkeypatch)
    conn = world.connection
    if mismatch == "owner":
        other = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4().hex}@example.invalid",
            hashed_password="unused",
            is_active=True,
        )
        db.add(other)
        await db.flush([other])
        conn.user_id = other.id
    elif mismatch == "broker":
        conn.broker = "kraken"
    elif mismatch == "environment":
        conn.environment = "demo"
    else:
        conn.is_active = False
    if mismatch == "live_environment_fallback":
        monkeypatch.setattr(settings, "T212_LIVE_API_KEY", secrets.token_urlsafe(24))
        monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", secrets.token_urlsafe(24))
    await db.commit()
    result = await operator_status(current_user=world.owner, db=db, audit_limit=20)
    assert result.safety_flags.credential_source == "none"
    assert result.safety_flags.credentials_configured is False
