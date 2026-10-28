"""Real PostgreSQL locks/FKs with isolated schemas and in-memory broker transport."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.broker.provider import BrokerProviderCredentials, create_trading212_provider_adapter
from app.core.security import encrypt_field
from app.db.models import AppSettings, AuditLog, BrokerConnection, OrderSubmissionAttempt, User
from app.db.session import Base
from app.execution.engine import ExecutionEngine
from app.services.live_evidence import unanchored_audit_condition
from app.services.live_readiness import LiveReadinessService
from tests.integration.test_live_account_authorization import authorization_world, review_and_unlock

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL required for real lock/FK proof",
)


@pytest_asyncio.fixture
async def live_sessions():
    # Created and removed only inside the explicitly supplied disposable test DB.
    engine = create_async_engine(os.environ["POSTGRES_TEST_DATABASE_URL"], poolclass=NullPool)
    schema = "live_gate_" + uuid.uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    async with isolated.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(isolated, expire_on_commit=False, autoflush=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def test_atomic_head_rollback_protected_anchors_and_retention(live_sessions, monkeypatch):
    async with live_sessions() as db:
        world = await authorization_world(db, monkeypatch)
        await review_and_unlock(db, world)
        owner_id = world.owner.id
        old_id = world.app.live_readiness_audit_id
        await world.service.apply_action(action="record_demo_validation", actor=world.owner.email)
        changed_id = world.app.live_readiness_audit_id
        assert changed_id != old_id
        await db.rollback()
        app = await db.get(AppSettings, 1)
        assert app.live_readiness_audit_id == old_id
        assert await db.get(AuditLog, changed_id) is None
        conn = (await db.execute(select(BrokerConnection))).scalar_one()
        for anchored in [old_id, conn.last_test_audit_id]:
            with pytest.raises(IntegrityError):
                await db.execute(delete(AuditLog).where(AuditLog.id == anchored))
            await db.rollback()
        await db.execute(delete(AuditLog).where(unanchored_audit_condition()))
        await db.commit()
        assert (await LiveReadinessService(db, owner_id=owner_id).evaluate())[
            "ready_for_live"
        ] is True


async def test_global_stop_does_not_wait_for_owner_lock(live_sessions, monkeypatch):
    async with live_sessions() as setup:
        world = await authorization_world(setup, monkeypatch)
        await review_and_unlock(setup, world)
        owner_id = world.owner.id
    async with live_sessions() as holder:
        await holder.execute(select(User).where(User.id == owner_id).with_for_update())

        async def stop():
            async with live_sessions() as stopper:
                await LiveReadinessService(stopper, owner_id=owner_id).apply_action(
                    action="lock_live", actor="synthetic-stop"
                )
                await stopper.commit()

        await asyncio.wait_for(stop(), timeout=1.5)
        await holder.rollback()
    async with live_sessions() as check:
        assert (await check.get(AppSettings, 1)).live_trading_unlocked is False


@pytest.mark.parametrize("order_type", ["market", "limit", "stop", "stop_limit"])
async def test_real_transport_exact_request_permit_positive(live_sessions, monkeypatch, order_type):
    async with live_sessions() as db:
        world = await authorization_world(db, monkeypatch)
        await review_and_unlock(db, world)
        posts = []

        async def handle(request):
            posts.append(request)
            return httpx.Response(200, json={"id": "SYNTHETIC-ACK", "status": "WORKING"})

        adapter = create_trading212_provider_adapter(
            world.transport.provider_request,
            BrokerProviderCredentials(
                api_key=world.transport.api_key, api_secret=world.transport.api_secret
            ),
            app_mode="live",
            live_trading_enabled=True,
        )
        adapter._transport_credentials = (world.transport.api_key, world.transport.api_secret)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="https://synthetic.invalid"
        ) as client:
            adapter._client = client
            engine = ExecutionEngine(db, adapter)
            order = await engine.create_order_intent(
                ticker="SYNTHETIC",
                side="buy",
                order_type=order_type,
                quantity=Decimal("1"),
                limit_price=Decimal("10") if order_type in {"limit", "stop_limit"} else None,
                stop_price=Decimal("9") if order_type in {"stop", "stop_limit"} else None,
                is_dry_run=False,
            )
            await engine.submit_order(order)
            assert len(posts) == 1
            assert posts[0].url.path == f"/api/v0/equity/orders/{order_type}"
            assert order.broker_order_id == "SYNTHETIC-ACK"
        adapter._client = None


@pytest.mark.parametrize(
    "change", ["disconnect", "rotation", "failed_test", "kill", "lock_then_reunlock", "mock_source"]
)
async def test_revocation_during_rate_limit_wait_yields_zero_http_sends(
    live_sessions, monkeypatch, change
):
    from app.core.config import settings
    from app.services.live_evidence import record_broker_test

    async with live_sessions() as db:
        world = await authorization_world(db, monkeypatch)
        await review_and_unlock(db, world)
        owner_id, connection_id = world.owner.id, world.connection.id
        posts = []

        async def handle(request):
            posts.append(request)
            return httpx.Response(200, json={"id": "MUST-NOT-SEND", "status": "WORKING"})

        adapter = create_trading212_provider_adapter(
            world.transport.provider_request,
            BrokerProviderCredentials(
                api_key=world.transport.api_key, api_secret=world.transport.api_secret
            ),
            app_mode="live",
            live_trading_enabled=True,
        )
        adapter._transport_credentials = (world.transport.api_key, world.transport.api_secret)

        async def change_after_wait():
            async with live_sessions() as other:
                connection = await other.get(BrokerConnection, connection_id)
                if change == "disconnect":
                    connection.is_active = False
                elif change == "rotation":
                    connection.api_key_encrypted = encrypt_field(uuid.uuid4().hex)
                elif change == "failed_test":
                    await record_broker_test(
                        other, connection, success=False, actor="synthetic-test"
                    )
                elif change == "kill":
                    await other.execute(
                        update(AppSettings)
                        .where(AppSettings.id == 1)
                        .values(kill_switch_active=True)
                    )
                elif change == "lock_then_reunlock":
                    service = LiveReadinessService(other, owner_id=owner_id)
                    await service.apply_action(action="lock_live", actor="synthetic-stop")
                    await other.commit()
                    for action in [
                        "record_demo_validation",
                        "record_broker_test",
                        "record_telegram_test",
                        "record_kill_switch_test",
                        "unlock_live",
                    ]:
                        await service.apply_action(action=action, actor="synthetic-review")
                else:
                    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")
                await other.commit()

        monkeypatch.setattr(adapter, "_check_rate_limit", change_after_wait)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="https://synthetic.invalid"
        ) as client:
            adapter._client = client
            engine = ExecutionEngine(db, adapter)
            order = await engine.create_order_intent(
                ticker="SYNTHETIC",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                limit_price=Decimal("10"),
                is_dry_run=False,
            )
            await engine.submit_order(order)
            assert posts == []
            assert order.status == "error"
            assert order.broker_order_id is None
            assert order.filled_quantity in (None, Decimal("0"))
            attempt = (
                await db.execute(
                    select(OrderSubmissionAttempt).where(
                        OrderSubmissionAttempt.order_id == order.id
                    )
                )
            ).scalar_one()
            assert attempt.outcome == "not_transmitted"
        adapter._client = None


async def test_broker_test_result_cannot_approve_a_rotated_connection(live_sessions, monkeypatch):
    from fastapi import HTTPException

    import app.broker.provider as providers
    from app.api.v1.routes.broker import test_connection as run_test

    async with live_sessions() as db:
        world = await authorization_world(db, monkeypatch)
        await review_and_unlock(db, world)
        connection_id, original_test = world.connection.id, world.connection.last_test_audit_id

        class OfflineTest:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def test_connection(self):
                async with live_sessions() as rotating:
                    await rotating.execute(
                        update(BrokerConnection)
                        .where(BrokerConnection.id == connection_id)
                        .values(api_key_encrypted=encrypt_field(uuid.uuid4().hex))
                    )
                    await rotating.commit()
                return {
                    "is_ok": True,
                    "account_id": "synthetic-account-a",
                    "currency": "USD",
                    "error": None,
                }

        monkeypatch.setattr(
            providers, "create_trading212_provider_adapter", lambda *_args, **_kwargs: OfflineTest()
        )
        with pytest.raises(HTTPException) as raised:
            await run_test(current_user=world.owner, db=db)
        assert raised.value.status_code == 409
        await db.rollback()
        current = await db.get(BrokerConnection, connection_id)
        assert current.last_test_audit_id == original_test
