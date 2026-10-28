"""Synthetic account/evidence gates; never constructs or calls a real broker."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.broker.provider import BrokerProviderRequest, trading212_account_scope
from app.core.config import settings
from app.core.security import encrypt_field
from app.db.models import AppSettings, AuditLog, BrokerConnection, User
from app.services.live_evidence import record_broker_test
from app.services.live_readiness import LiveReadinessService
from app.services.telegram_control import TelegramControlService


async def authorization_world(db, monkeypatch):
    for key, value in {
        "APP_MODE": "live",
        "LIVE_TRADING_ENABLED": True,
        "MARKET_DATA_PROVIDER": "alpaca",
        "ALPACA_API_KEY": secrets.token_urlsafe(24),
        "ALPACA_API_SECRET": secrets.token_urlsafe(24),
    }.items():
        monkeypatch.setattr(settings, key, value)
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
    owner = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.invalid",
        hashed_password="unused",
        is_admin=True,
        is_active=True,
    )
    app = AppSettings(
        id=1,
        kill_switch_active=False,
        auto_trading_enabled=False,
        live_trading_unlocked=False,
        extra={"unrelated": {"preserve": True}},
    )
    key, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=owner.id,
        broker="trading212",
        environment="live",
        account_id="synthetic-account-a",
        account_currency="USD",
        api_key_encrypted=encrypt_field(key),
        api_secret_encrypted=encrypt_field(secret),
        is_active=True,
    )
    db.add(owner)
    await db.flush([owner])
    db.add_all([app, conn])
    await db.flush()
    await record_broker_test(db, conn, success=True, actor=owner.email)
    await db.commit()
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
        user_id=owner.id,
        account_id=conn.account_id,
    )
    transport = SimpleNamespace(
        provider_request=request,
        account_scope=trading212_account_scope(request),
        api_key=key,
        api_secret=secret,
        environment="live",
    )
    service = LiveReadinessService(db, owner_id=owner.id)
    return SimpleNamespace(
        owner=owner, app=app, connection=conn, service=service, transport=transport
    )


async def review_and_unlock(db, world):
    for action in [
        "record_demo_validation",
        "record_broker_test",
        "record_telegram_test",
        "record_kill_switch_test",
        "unlock_live",
    ]:
        await world.service.apply_action(action=action, actor=world.owner.email)
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is True
    # A status check on this reused service may take locks; end the fixture transaction.
    await db.commit()


async def test_explicit_owner_test_and_bound_reviews_can_authorize(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    assert world.app.extra == {"unrelated": {"preserve": True}}
    head = await db.get(AuditLog, world.app.live_readiness_audit_id)
    assert head.payload["binding"]["owner_id"] == str(world.owner.id)
    assert head.payload["broker_test_id"] == str(world.connection.last_test_audit_id)


async def test_legacy_unbound_json_and_timestamp_never_authorize(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    world.app.live_readiness_audit_id = None
    world.app.extra = {
        "live_readiness": {
            k: datetime.now(UTC).isoformat()
            for k in [
                "demo_validated_at",
                "broker_test_verified_at",
                "telegram_test_verified_at",
                "kill_switch_tested_at",
                "live_unlock_acknowledged_at",
            ]
        }
    }
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_active", False),
        ("last_test_ok", False),
        ("account_id", None),
        ("account_id", "different-account"),
        ("environment", "demo"),
        ("broker", "kraken"),
    ],
)
async def test_changed_connection_state_cannot_reuse_evidence(db, monkeypatch, field, value):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    setattr(world.connection, field, value)
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False


async def test_credential_rotation_invalidates_pending_review(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    world.connection.api_key_encrypted = encrypt_field(secrets.token_urlsafe(24))
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False


async def test_new_test_identity_requires_review_even_at_same_timestamp(db, monkeypatch):
    import app.services.live_evidence as evidence

    world = await authorization_world(db, monkeypatch)
    fixed = datetime.now(UTC) - timedelta(seconds=1)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return fixed

    monkeypatch.setattr(evidence, "datetime", Frozen)
    await record_broker_test(db, world.connection, success=True, actor=world.owner.email)
    await review_and_unlock(db, world)
    reviewed = world.connection.last_test_audit_id
    await record_broker_test(db, world.connection, success=False, actor=world.owner.email)
    await record_broker_test(db, world.connection, success=True, actor=world.owner.email)
    await db.commit()
    assert world.connection.last_test_audit_id != reviewed
    result = await world.service.evaluate()
    assert (
        next(c for c in result["checks"] if c["key"] == "live_broker_test_recent")["status"]
        == "pass"
    )
    assert result["ready_for_live"] is False


async def test_unrelated_extra_replacement_cannot_restore_or_erase_authority(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    head = world.app.live_readiness_audit_id
    await db.execute(
        update(AppSettings).where(AppSettings.id == 1).values(extra={"stale_writer": True})
    )
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is True
    assert world.app.live_readiness_audit_id == head
    await world.service.apply_action(action="lock_live", actor=world.owner.email)
    await db.commit()
    await db.execute(
        update(AppSettings)
        .where(AppSettings.id == 1)
        .values(extra={"live_readiness": {"ready": True}})
    )
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False


async def test_global_lock_survives_malformed_head(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    head = await db.get(AuditLog, world.app.live_readiness_audit_id)
    head.payload = {"corrupt": True}
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False
    await LiveReadinessService(db).apply_action(action="lock_live", actor="synthetic-global-stop")
    await db.commit()
    assert world.app.live_trading_unlocked is False


async def test_re_review_does_not_rearm_an_older_unlock(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    await world.service.apply_action(action="record_demo_validation", actor=world.owner.email)
    await db.commit()
    status = await world.service.evaluate()
    assert status["eligible_for_unlock"] is True
    assert status["ready_for_live"] is False


async def test_role_revocation_refuses_current_evidence(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    world.owner.is_admin = False
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False


async def test_default_no_principal_never_authorizes_or_unlocks(db, monkeypatch):
    from app.services.live_readiness import LiveReadinessError

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    service = LiveReadinessService(db)
    assert (await service.evaluate())["ready_for_live"] is False
    with pytest.raises(LiveReadinessError):
        await service.apply_action(action="unlock_live", actor="unscoped-synthetic")


async def test_complete_snapshot_survives_pruned_unanchored_tail(db, monkeypatch):
    from sqlalchemy import delete

    from app.services.live_evidence import unanchored_audit_condition

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    await db.execute(delete(AuditLog).where(unanchored_audit_condition()))
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is True
    rows = list((await db.execute(select(AuditLog))).scalars())
    assert {row.id for row in rows} == {
        world.app.live_readiness_audit_id,
        world.connection.last_test_audit_id,
    }


@pytest.mark.parametrize(
    "mutation",
    ["naive", "future", "schema_float", "wrong_actor_owner", "bad_reference", "bad_test_reference"],
)
async def test_malformed_current_snapshot_is_closed_but_stop_remains_available(
    db, monkeypatch, mutation
):
    from copy import deepcopy

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    head = await db.get(AuditLog, world.app.live_readiness_audit_id)
    data = deepcopy(head.payload)
    if mutation == "naive":
        data["evidence"]["demo_validated_at"] = datetime.now().isoformat()
    elif mutation == "future":
        data["recorded_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif mutation == "schema_float":
        data["schema"] = 2.0
    elif mutation == "wrong_actor_owner":
        head.user_id = None
    elif mutation == "bad_reference":
        data["references"]["demo_validated_at"] = None
    else:
        data["test_references"]["broker_test_verified_at"] = None
    await db.execute(update(AuditLog).where(AuditLog.id == head.id).values(payload=data))
    await db.commit()
    assert (await world.service.evaluate())["ready_for_live"] is False
    await LiveReadinessService(db).apply_action(action="lock_live", actor="synthetic-global-stop")
    await db.commit()
    assert world.app.live_trading_unlocked is False


async def test_different_owner_cannot_borrow_reviews(db, monkeypatch):
    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    other = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.invalid",
        hashed_password="unused",
        is_admin=True,
        is_active=True,
    )
    db.add(other)
    conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=other.id,
        broker="trading212",
        environment="live",
        account_id="synthetic-account-b",
        api_key_encrypted=encrypt_field(secrets.token_urlsafe(24)),
        api_secret_encrypted=encrypt_field(secrets.token_urlsafe(24)),
        is_active=True,
    )
    db.add(conn)
    await db.flush()
    await record_broker_test(db, conn, success=True, actor=other.email)
    await db.commit()
    assert (await LiveReadinessService(db, owner_id=other.id).evaluate())["ready_for_live"] is False
    # A's exact binding still succeeds; no "newest connection" substitution.
    assert (await world.service.evaluate())["ready_for_live"] is True


async def test_broker_test_cas_rejects_rotated_or_disconnected_row(db, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.routes.broker import _connection_revision, _unchanged_after_test

    world = await authorization_world(db, monkeypatch)
    revision = _connection_revision(world.connection)
    world.connection.is_active = False
    await db.commit()
    with pytest.raises(HTTPException) as raised:
        await _unchanged_after_test(db, world.owner.id, "live", revision)
    assert raised.value.status_code == 409


async def test_bound_dispatch_and_market_changes_refuse_entries_preserve_sells(db, monkeypatch):
    from decimal import Decimal

    from app.db.models import Order, OrderEvent
    from app.services.live_dispatch import (
        LiveDispatchError,
        authorize_dispatch,
        capture_intent_context,
    )

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    order = Order(
        id=uuid.uuid4(),
        ticker="SYNTHETIC",
        client_order_key=uuid.uuid4().hex,
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        limit_price=Decimal("10"),
        is_dry_run=False,
        status="pending_intent",
        venue="t212",
        execution_environment="live",
        broker_account_scope=world.transport.account_scope,
    )
    db.add(order)
    await db.flush()
    context = await capture_intent_context(db, world.transport, side="buy")
    event = OrderEvent(order_id=order.id, event_type="live_account_bound", payload=context)
    db.add(event)
    await db.commit()
    authorization = await authorize_dispatch(db, world.transport, order)
    assert authorization["binding"]["owner_id"] == str(world.owner.id)
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
    with pytest.raises(LiveDispatchError):
        await authorize_dispatch(db, world.transport, order)
    order.side = "sell"
    event.payload = await capture_intent_context(db, world.transport, side="sell")
    await db.commit()
    assert (await authorize_dispatch(db, world.transport, order))["market_source"] is None
    await world.service.apply_action(action="lock_live", actor=world.owner.email)
    await db.commit()
    with pytest.raises(LiveDispatchError):
        await authorize_dispatch(db, world.transport, order)


async def test_immediate_new_intent_submission_needs_no_intervening_commit(db, monkeypatch):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from app.execution.engine import ExecutionEngine

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    world.transport.place_limit_order = AsyncMock(
        return_value={"id": "SYNTHETIC-ACK", "status": "WORKING"}
    )
    engine = ExecutionEngine(db, world.transport)
    order = await engine.create_order_intent(
        ticker="SYNTHETIC",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        limit_price=Decimal("10"),
        is_dry_run=False,
    )
    await engine.submit_order(order)
    world.transport.place_limit_order.assert_awaited_once()
    assert order.broker_order_id == "SYNTHETIC-ACK"


async def test_adapter_account_context_and_credentials_cannot_be_rebound(db, monkeypatch):
    from dataclasses import replace

    from app.services.live_account import LiveAccountError, binding_for_adapter

    world = await authorization_world(db, monkeypatch)
    await binding_for_adapter(db, world.transport)
    request = world.transport.provider_request
    for change in [
        {"user_id": uuid.uuid4()},
        {"account_id": "wrong-account"},
        {"environment": "demo"},
        {"broker_id": "kraken"},
    ]:
        world.transport.provider_request = replace(request, **change)
        with pytest.raises(LiveAccountError):
            await binding_for_adapter(db, world.transport)
    world.transport.provider_request = request
    world.transport.api_key = secrets.token_urlsafe(24)
    with pytest.raises(LiveAccountError):
        await binding_for_adapter(db, world.transport)


async def test_evaluation_clock_is_taken_after_current_rows_are_acquired(db, monkeypatch):
    import app.services.live_readiness as readiness

    world = await authorization_world(db, monkeypatch)
    await review_and_unlock(db, world)
    acquired = False
    original = world.service._get_settings

    async def acquire():
        nonlocal acquired
        result = await original()
        acquired = True
        return result

    class AfterLocks(datetime):
        @classmethod
        def now(cls, tz=None):
            assert acquired
            return datetime.now(tz)

    monkeypatch.setattr(world.service, "_get_settings", acquire)
    monkeypatch.setattr(readiness, "datetime", AfterLocks)
    assert (await world.service.evaluate())["ready_for_live"] is True
