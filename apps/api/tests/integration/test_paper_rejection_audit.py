"""Rejection evidence commits independently of abandoned paper transactions."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db.models import AuditLog, Order, User
from app.db.session import Base
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from tests.integration.test_paper_execution_realism import _body, _seed_user_and_settings


@pytest_asyncio.fixture
async def audit_sessions():
    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL required for independent audit transactions")
    schema = f"paper_audit_{uuid.uuid4().hex}"
    engine = create_async_engine(url, poolclass=NullPool)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(scoped, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def _cash_scenario(sessions):
    async with sessions() as db:
        user = await _seed_user_and_settings(db)
        order = await PaperExecutionEngine(db).execute(
            _body(quantity=Decimal("900"), simulation_profile="partial_fill"), user=user
        )
        await db.commit()
        return user.id, order.id


@pytest.mark.asyncio
async def test_cash_rejection_survives_rollback_without_committing_prior_economics(audit_sessions):
    user_id, order_id = await _cash_scenario(audit_sessions)
    async with audit_sessions() as outer:
        user, order = await outer.get(User, user_id), await outer.get(Order, order_id)
        engine = PaperExecutionEngine(outer)
        baseline_cash = (await engine._latest_paper_account(user)).cash
        other = await engine.execute(
            _body(ticker="UNCOMMITTED", quantity=Decimal("500")), user=user
        )
        other_id = other.id
        with pytest.raises(PaperExecutionError, match="cash guard") as failure:
            await engine.fill_partial_order(
                order, quantity=Decimal("450"), estimated_price=Decimal("100"), user=user
            )
        assert failure.value.status_code == 422
        assert outer.in_transaction()
        await outer.rollback()
    async with audit_sessions() as verify:
        user = await verify.get(User, user_id)
        assert (
            await PaperExecutionEngine(verify)._latest_paper_account(user)
        ).cash == baseline_cash
        assert await verify.get(Order, other_id) is None
        assert (await verify.get(Order, order_id)).filled_quantity == Decimal("450")
        audit = (
            await verify.execute(
                select(AuditLog).where(AuditLog.action == "paper_follow_up_fill_blocked")
            )
        ).scalar_one_or_none()
        assert audit is not None, "cash rejection evidence was lost with the caller rollback"
        assert audit.user_id is None
        assert audit.payload["audit_user_id"] == str(user_id)
        assert audit.entity_id == str(order_id)
        assert audit.payload["reason"] == "paper_cash_guard"


async def _rejection(db, user_id, action="paper_follow_up_fill_blocked"):
    await PaperExecutionEngine(db)._audit(
        action,
        actor="synthetic-operator",
        entity_type="order",
        entity_id="synthetic-order",
        user_id=user_id,
        payload={"reason": "synthetic rejection", "quantity": "1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["paper_follow_up_fill_blocked", "paper_sell_fill_blocked"])
@pytest.mark.parametrize("user_state", ["locked", "uncommitted"])
async def test_both_actions_survive_without_user_fk_deadlock(audit_sessions, action, user_state):
    async with audit_sessions() as outer:
        user = await _seed_user_and_settings(outer)
        user_id = user.id
        if user_state == "locked":
            await outer.commit()
            await outer.execute(select(User).where(User.id == user_id).with_for_update())
        async with asyncio.timeout(5):
            await _rejection(outer, user_id, action)
        assert outer.in_transaction()
        await outer.rollback()
    async with audit_sessions() as verify:
        audit = (await verify.execute(select(AuditLog))).scalar_one()
        assert audit.action == action
        assert audit.user_id is None
        assert audit.actor == "synthetic-operator"
        assert audit.entity_id == "synthetic-order"
        assert audit.payload["audit_user_id"] == str(user_id)
        assert audit.payload["reason"] == "synthetic rejection"
        assert (await verify.get(User, user_id) is None) == (user_state == "uncommitted")


@pytest.mark.asyncio
async def test_connection_bound_caller_preserves_schema_and_transaction(audit_sessions):
    engine = audit_sessions.kw["bind"]
    schema_options = engine.sync_engine.get_execution_options()
    unscoped = engine.execution_options(schema_translate_map=None)
    async with unscoped.connect() as conn:
        translated = await conn.execution_options(**schema_options)
        async with AsyncSession(translated, expire_on_commit=False) as outer:
            user = await _seed_user_and_settings(outer)
            user_id = user.id
            await _rejection(outer, user_id)
            await outer.rollback()
    async with audit_sessions() as verify:
        assert await verify.get(User, user_id) is None
        audit = (await verify.execute(select(AuditLog))).scalar_one()
        assert audit.payload["audit_user_id"] == str(user_id)


@pytest.mark.asyncio
async def test_sqlite_rejects_durable_sink_without_fallback_or_outer_commit(db):
    user = await _seed_user_and_settings(db)
    with pytest.raises(
        PaperExecutionError, match=r"synthetic rejection.*audit unavailable"
    ) as failure:
        await _rejection(db, user.id)
    assert failure.value.status_code == 503
    assert db.in_transaction()
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == 0


@pytest.mark.asyncio
async def test_postgres_static_pool_rejected_before_connection():
    engine = create_async_engine(
        "postgresql+asyncpg://invalid:synthetic@127.0.0.1:1/unused", poolclass=StaticPool
    )
    try:
        async with AsyncSession(engine) as outer:
            with pytest.raises(PaperExecutionError, match="audit unavailable") as failure:
                await _rejection(outer, uuid.uuid4())
            assert failure.value.status_code == 503
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["commit", "timeout", "cancel", "ack_lost"])
async def test_unacknowledged_sink_fails_closed_without_retry(
    audit_sessions, monkeypatch, failure_kind
):
    from app.services import paper_rejection_audit as sink

    user_id, order_id = await _cash_scenario(audit_sessions)
    started = asyncio.Event()
    attempts = []

    class InterruptedWriter(AsyncSession):
        async def commit(self):
            attempts.append(1)
            started.set()
            if failure_kind == "commit":
                raise RuntimeError("synthetic commit failure")
            if failure_kind == "ack_lost":
                await super().commit()
                raise RuntimeError("synthetic lost commit acknowledgement")
            await asyncio.Event().wait()

    monkeypatch.setattr(sink, "AsyncSession", InterruptedWriter)
    monkeypatch.setattr(sink, "AUDIT_WRITE_TIMEOUT_SECONDS", 0.1)
    async with audit_sessions() as outer:
        user, order = await outer.get(User, user_id), await outer.get(Order, order_id)
        engine = PaperExecutionEngine(outer)
        await engine.execute(_body(ticker="UNCOMMITTED", quantity=Decimal("500")), user=user)
        before_cash = (await engine._latest_paper_account(user)).cash
        task = asyncio.create_task(
            engine.fill_partial_order(
                order, quantity=Decimal("450"), estimated_price=Decimal("100"), user=user
            )
        )
        if failure_kind == "cancel":
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(
                PaperExecutionError, match=r"paper_cash_guard.*audit unavailable"
            ) as failure:
                await asyncio.wait_for(task, 5)
            assert failure.value.status_code == 503
        assert len(attempts) == 1
        assert outer.in_transaction()
        assert order.filled_quantity == Decimal("450")
        assert (await engine._latest_paper_account(user)).cash == before_cash
        await outer.rollback()
    async with audit_sessions() as verify:
        assert await verify.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "paper_follow_up_fill_blocked")
        ) == (1 if failure_kind == "ack_lost" else 0)


@pytest.mark.asyncio
async def test_success_audit_remains_owned_by_callers_transaction(audit_sessions):
    async with audit_sessions() as outer:
        user = await _seed_user_and_settings(outer)
        await PaperExecutionEngine(outer)._audit(
            "paper_follow_up_fill", actor="synthetic", user_id=user.id
        )
        await outer.rollback()
    async with audit_sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(AuditLog)) == 0


async def _pending_cash_rejection(outer, user_id, order_id):
    """Stage a financial update without taking an audit-table lock in the caller."""
    user, order = await outer.get(User, user_id), await outer.get(Order, order_id)
    engine = PaperExecutionEngine(outer)
    account = await engine._latest_paper_account(user)
    original_cash = account.cash
    account.cash = Decimal("0")
    await outer.flush()
    return engine, user, order, account, original_cash


async def _assert_pending_cash_unchanged(outer, order, account):
    assert outer.in_transaction()
    await outer.refresh(order)
    await outer.refresh(account)
    assert order.filled_quantity == Decimal("450")
    assert account.cash == Decimal("0")
    assert await outer.scalar(text("SELECT 1")) == 1


async def _assert_cash_rollback(sessions, user_id, order_id, original_cash):
    async with sessions() as verify:
        user = await verify.get(User, user_id)
        assert (
            await PaperExecutionEngine(verify)._latest_paper_account(user)
        ).cash == original_cash
        assert (await verify.get(Order, order_id)).filled_quantity == Decimal("450")


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_real_postgres_insert_lock_interruption_isolated_from_caller(
    audit_sessions, interruption
):
    user_id, order_id = await _cash_scenario(audit_sessions)
    schema = audit_sessions.kw["bind"].sync_engine.get_execution_options()["schema_translate_map"][
        None
    ]
    async with audit_sessions() as outer, audit_sessions() as blocker:
        engine, user, order, account, original_cash = await _pending_cash_rejection(
            outer, user_id, order_id
        )
        await blocker.execute(text(f'LOCK TABLE "{schema}".audit_logs IN ACCESS EXCLUSIVE MODE'))
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(
            engine.fill_partial_order(
                order, quantity=Decimal("450"), estimated_price=Decimal("100"), user=user
            )
        )
        try:
            async with audit_sessions() as observer, asyncio.timeout(5):
                while True:
                    blocked = await observer.scalar(
                        text(
                            "SELECT pid FROM pg_locks "
                            "WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)) LIMIT 1"
                        ),
                        {"blocker": blocker_pid},
                    )
                    if blocked is not None:
                        break
                    assert not task.done(), "writer never entered the real PostgreSQL INSERT wait"
                    await asyncio.sleep(0.01)
            if interruption == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            else:
                with pytest.raises(PaperExecutionError, match="audit unavailable") as failure:
                    await asyncio.wait_for(task, 5)
                assert failure.value.status_code == 503
            await _assert_pending_cash_unchanged(outer, order, account)
            await blocker.rollback()
            # NOWAIT is discriminating: a cancelled writer retaining its INSERT lock fails.
            async with audit_sessions() as probe:
                await probe.execute(
                    text(f'LOCK TABLE "{schema}".audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT')
                )
                assert (
                    await probe.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(AuditLog.action == "paper_follow_up_fill_blocked")
                    )
                    == 0
                )
                await probe.rollback()
            await outer.rollback()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await _assert_cash_rollback(audit_sessions, user_id, order_id, original_cash)
    async with audit_sessions() as healthy:
        async with asyncio.timeout(5):
            await _rejection(healthy, user_id)
        await healthy.rollback()
    async with audit_sessions() as verify:
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "paper_follow_up_fill_blocked")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_exhausted_single_connection_pool_fails_without_releasing_caller(
    audit_sessions, monkeypatch
):
    from app.services import paper_rejection_audit as sink

    user_id, order_id = await _cash_scenario(audit_sessions)
    original_engine = audit_sessions.kw["bind"]
    pooled = create_async_engine(original_engine.url, pool_size=1, max_overflow=0, pool_timeout=30)
    scoped = pooled.execution_options(**original_engine.sync_engine.get_execution_options())
    monkeypatch.setattr(sink, "AUDIT_WRITE_TIMEOUT_SECONDS", 0.1)
    try:
        async with AsyncSession(scoped, expire_on_commit=False) as outer:
            engine, user, order, account, original_cash = await _pending_cash_rejection(
                outer, user_id, order_id
            )
            assert pooled.pool.checkedout() == 1
            with pytest.raises(PaperExecutionError, match="audit unavailable") as failure:
                await asyncio.wait_for(
                    engine.fill_partial_order(
                        order, quantity=Decimal("450"), estimated_price=Decimal("100"), user=user
                    ),
                    5,
                )
            assert failure.value.status_code == 503
            assert pooled.pool.checkedout() == 1
            await _assert_pending_cash_unchanged(outer, order, account)
            await outer.rollback()
        assert pooled.pool.checkedout() == 0
        async with scoped.connect() as probe, asyncio.timeout(5):
            assert await probe.scalar(text("SELECT 1")) == 1
    finally:
        await pooled.dispose()
    await _assert_cash_rollback(audit_sessions, user_id, order_id, original_cash)


@pytest.mark.asyncio
async def test_missing_audit_table_fails_without_aborting_callers_economics(audit_sessions):
    user_id, order_id = await _cash_scenario(audit_sessions)
    schema = audit_sessions.kw["bind"].sync_engine.get_execution_options()["schema_translate_map"][
        None
    ]
    async with audit_sessions() as setup:
        await setup.execute(text(f'DROP TABLE "{schema}".audit_logs'))
        await setup.commit()
    async with audit_sessions() as outer:
        engine, user, order, account, original_cash = await _pending_cash_rejection(
            outer, user_id, order_id
        )
        with pytest.raises(PaperExecutionError, match="audit unavailable") as failure:
            await engine.fill_partial_order(
                order, quantity=Decimal("450"), estimated_price=Decimal("100"), user=user
            )
        assert failure.value.status_code == 503
        await _assert_pending_cash_unchanged(outer, order, account)
        await outer.rollback()
    await _assert_cash_rollback(audit_sessions, user_id, order_id, original_cash)
