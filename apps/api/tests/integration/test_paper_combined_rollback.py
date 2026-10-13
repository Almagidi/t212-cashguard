"""Combined paper conservation/halt evidence across rollback and cancellation.

Rejection evidence survives rollback; economic rows remain transactional.
All data uses the disposable PostgreSQL schema provided by the halt proof fixture.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update

from app.db.models import (
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    BrokerConnection,
    Order,
    OrderEvent,
    PositionSnapshot,
    Trade,
    User,
)
from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
from tests.integration.test_paper_follow_up_halt import (
    _partial,
    _wait_for_blocker,
)
from tests.integration.test_paper_follow_up_halt import (
    sessions as sessions,  # shared disposable PostgreSQL fixture
)


async def _ledger(db):
    """Read persisted values, not mutable ORM identities or only row counts."""
    state = {}
    for model in (Order, OrderEvent, Trade, PositionSnapshot, BrokerAccountSnapshot, AuditLog):
        rows = await db.execute(select(model.__table__).order_by(model.id))
        state[model.__tablename__] = deepcopy([dict(row) for row in rows.mappings()])
    return state


async def _fill(db, user_id, order_id):
    user = await db.get(User, user_id)
    order = await db.get(Order, order_id)
    return await PaperExecutionEngine(db).fill_partial_order(
        order, quantity=Decimal("1"), estimated_price=Decimal("100"), user=user
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_successful_follow_up_rollback_restores_ledger_then_retry_applies_once(
    sessions, side
):
    async with sessions() as setup:
        user, order = await _partial(setup, side)
        user_id, order_id = user.id, order.id
        await setup.commit()
        before = await _ledger(setup)
        account_before = await PaperExecutionEngine(setup)._latest_paper_account(user)
        cash_before = account_before.cash

    async with sessions() as aborted:
        filled = await _fill(aborted, user_id, order_id)
        assert filled.filled_quantity == Decimal("3")
        assert await _ledger(aborted) != before
        await aborted.rollback()

    async with sessions() as verify:
        assert await _ledger(verify) == before

    async with sessions() as retry:
        filled = await _fill(retry, user_id, order_id)
        assert filled.filled_quantity == Decimal("3")
        assert filled.remaining_quantity == Decimal("1")
        await retry.commit()

    async with sessions() as verify:
        after = await _ledger(verify)
        for table in ("positions_snapshots", "broker_accounts_snapshots", "order_events"):
            assert len(after[table]) == len(before[table]) + 1
        assert len(after["trades"]) == len(before["trades"]) + (side == "sell")
        user = await verify.get(User, user_id)
        engine = PaperExecutionEngine(verify)
        account = await engine._latest_paper_account(user)
        position = (await engine._latest_paper_positions(user))["REALISM"]
        expected_delta = Decimal("-100.12002000") if side == "buy" else Decimal("99.88002000")
        assert account.cash == cash_before + expected_delta
        assert account.cash >= 0
        assert position.quantity == (Decimal("3") if side == "buy" else Decimal("1"))
        assert position.quantity_available == position.quantity
        fill_audits = [
            row for row in after["audit_logs"] if row["action"] == "paper_follow_up_fill"
        ]
        assert len(fill_audits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("lock_kind", ["account", "halt"])
async def test_cancelled_lock_wait_leaves_no_effects_and_releases_prior_locks(
    sessions, side, lock_kind
):
    async with sessions() as setup:
        user, order = await _partial(setup, side)
        user_id, order_id = user.id, order.id
        await setup.commit()
        before = await _ledger(setup)

    fill_pid = asyncio.get_running_loop().create_future()

    async def contender():
        async with sessions() as db:
            fill_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            try:
                await _fill(db, user_id, order_id)
                await db.commit()
            except asyncio.CancelledError:
                await db.rollback()
                raise

    async with sessions() as blocker:
        statement = (
            select(BrokerConnection.id).where(BrokerConnection.user_id == user_id)
            if lock_kind == "account"
            else select(AppSettings.id).where(AppSettings.id == 1)
        )
        await blocker.execute(statement.with_for_update())
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(contender())
        try:
            await _wait_for_blocker(
                sessions, await asyncio.wait_for(fill_pid, 5), blocker_pid, task
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            # Cancellation has completed while the blocker is still held.
            async with sessions() as verify:
                assert await _ledger(verify) == before
            await blocker.rollback()
            # Proves no account/order/halt row lock was leaked by the cancelled session.
            async with sessions() as retry:
                async with asyncio.timeout(5):
                    filled = await _fill(retry, user_id, order_id)
                    await retry.commit()
                assert filled.filled_quantity == Decimal("3")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_rejection_audit_survives_caller_rollback_without_economic_effects(sessions, side):
    async with sessions() as setup:
        user, order = await _partial(setup, side)
        user_id, order_id = user.id, order.id
        await setup.execute(
            update(AppSettings).where(AppSettings.id == 1).values(kill_switch_active=True)
        )
        await setup.commit()
        before = await _ledger(setup)

    async with sessions() as caller:
        with pytest.raises(PaperExecutionError, match=r"[Kk]ill switch"):
            await _fill(caller, user_id, order_id)
        pending = await _ledger(caller)
        blocked = [
            row for row in pending["audit_logs"] if row["action"] == "paper_follow_up_fill_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["entity_id"] == str(order_id)
        await caller.rollback()

    async with sessions() as verify:
        after = await _ledger(verify)
        for table in before:
            if table != "audit_logs":
                assert after[table] == before[table]
        durable = [
            row for row in after["audit_logs"] if row["action"] == "paper_follow_up_fill_blocked"
        ]
        assert len(after["audit_logs"]) == len(before["audit_logs"]) + 1
        assert len(durable) == 1
        assert durable[0]["entity_id"] == str(order_id)
        assert durable[0]["payload"]["audit_user_id"] == str(user_id)
