"""A partial paper order grants no exemption from the current halt state."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    Order,
    OrderEvent,
    PositionSnapshot,
    Trade,
    User,
)
from app.db.session import Base
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from app.risk.engine import activate_kill_switch
from tests.integration.test_paper_execution_realism import _body, _seed_user_and_settings


async def _partial(db, side):
    user = await _seed_user_and_settings(db)
    engine = PaperExecutionEngine(db)
    if side == "sell":
        await engine.execute(_body(quantity=Decimal("4")), user=user)
    order = await engine.execute(
        _body(side=side, quantity=Decimal("4"), simulation_profile="partial_fill"), user=user
    )
    return user, order


async def _state(db, order):
    await db.flush()
    await db.refresh(order)
    counts = tuple(
        [
            await db.scalar(select(func.count()).select_from(model))
            for model in (Order, OrderEvent, Trade, PositionSnapshot, BrokerAccountSnapshot)
        ]
    )
    return (
        counts,
        order.status,
        order.filled_quantity,
        order.fee_amount,
        order.avg_fill_price,
        order.broker_response,
    )


async def _assert_blocked(db, user, order, reason):
    before = await _state(db, order)
    with pytest.raises(PaperExecutionError, match=reason):
        await PaperExecutionEngine(db).fill_partial_order(
            order, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
        )
    assert await _state(db, order) == before
    audits = (
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "paper_follow_up_fill_blocked")
            )
        )
        .scalars()
        .all()
    )
    assert audits[-1].entity_id == str(order.id)
    assert audits[-1].payload["no_broker_order_sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("state", ["halted", "missing"])
async def test_follow_up_requires_initialized_unhalted_safety_state(db, side, state):
    user, order = await _partial(db, side)
    settings = await db.get(AppSettings, 1)
    if state == "halted":
        settings.kill_switch_active = True
        reason = "[Kk]ill switch"
    else:
        await db.delete(settings)
        reason = "safety settings"
    await db.flush()
    await _assert_blocked(db, user, order, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_unhalted_follow_up_preserves_existing_fill_behavior(db, side):
    user, order = await _partial(db, side)
    await PaperExecutionEngine(db).fill_partial_order(
        order, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
    )
    assert order.status == "partially_filled"
    assert order.filled_quantity == Decimal("3")
    assert order.remaining_quantity == Decimal("1")
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "paper_follow_up_fill_blocked")
        )
        == 0
    )


@pytest_asyncio.fixture
async def sessions():
    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL required for PostgreSQL halt serialization")
    schema = f"paper_halt_{uuid.uuid4().hex}"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_postgres_follow_up_reads_halt_despite_cached_false_settings(sessions, side):
    async with sessions() as db:
        user, order = await _partial(db, side)
        stale = await db.get(AppSettings, 1)
        await db.commit()
        async with sessions() as control:
            await activate_kill_switch(control, actor="synthetic-operator")
        assert stale.kill_switch_active is False
        await _assert_blocked(db, user, order, "[Kk]ill switch")
        await db.commit()
    async with sessions() as verify:
        assert (await verify.get(Order, order.id)).filled_quantity == Decimal("2")
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "paper_follow_up_fill_blocked")
            )
            == 1
        )


async def _wait_for_blocker(sessions, contender, blocker, task):
    async with sessions() as observer:
        async with asyncio.timeout(5):
            while True:
                pids = await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": contender}
                )
                if blocker in pids:
                    return
                assert not task.done(), "operation bypassed the safety-state lock"
                await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_postgres_halt_started_first_blocks_waiting_fill(sessions):
    async with sessions() as setup:
        user, order = await _partial(setup, "buy")
        user_id, order_id = user.id, order.id
        await setup.commit()
    fill_pid = asyncio.get_running_loop().create_future()

    async def fill():
        async with sessions() as db:
            user, order = await db.get(User, user_id), await db.get(Order, order_id)
            fill_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            await _assert_blocked(db, user, order, "[Kk]ill switch")
            await db.commit()

    async with sessions() as control:
        await control.execute(
            update(AppSettings).where(AppSettings.id == 1).values(kill_switch_active=True)
        )
        control_pid = await control.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(fill())
        try:
            await _wait_for_blocker(
                sessions, await asyncio.wait_for(fill_pid, 5), control_pid, task
            )
            await control.commit()
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_postgres_accepted_fill_finishes_before_concurrent_halt_commits(sessions):
    async with sessions() as setup:
        user, order = await _partial(setup, "buy")
        user_id, order_id = user.id, order.id
        await setup.commit()
    control_pid = asyncio.get_running_loop().create_future()

    async def halt():
        async with sessions() as control:
            control_pid.set_result(await control.scalar(text("SELECT pg_backend_pid()")))
            await activate_kill_switch(control, actor="synthetic-operator")

    async with sessions() as db:
        user, order = await db.get(User, user_id), await db.get(Order, order_id)
        await PaperExecutionEngine(db).fill_partial_order(
            order, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
        )
        fill_pid = await db.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(halt())
        try:
            await _wait_for_blocker(
                sessions, await asyncio.wait_for(control_pid, 5), fill_pid, task
            )
            await db.commit()
            await asyncio.wait_for(task, 5)
            await _assert_blocked(db, user, order, "[Kk]ill switch")
            await db.commit()
            assert order.filled_quantity == Decimal("3")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
