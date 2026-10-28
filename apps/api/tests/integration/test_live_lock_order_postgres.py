"""Protective lock remains final through either concurrent acknowledgement ordering."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, text

from app.db.models import AppSettings, AuditLog
from app.services.live_readiness import LiveReadinessError, LiveReadinessService
from tests.integration.test_live_account_authorization import authorization_world, review_and_unlock
from tests.integration.test_live_dispatch_postgres import live_sessions as live_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL required for real lock-order proof",
)


@pytest.mark.parametrize("first", ["lock_live", "unlock_live"])
async def test_protective_lock_cannot_be_undone_by_a_concurrent_prior_acknowledgement(
    live_sessions, monkeypatch, first
):
    async with live_sessions() as setup:
        world = await authorization_world(setup, monkeypatch)
        await review_and_unlock(setup, world)
        owner_id = world.owner.id
    second = "unlock_live" if first == "lock_live" else "lock_live"
    started = asyncio.Future()

    async def waiter():
        async with live_sessions() as db:
            started.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            try:
                await LiveReadinessService(db, owner_id=owner_id).apply_action(
                    action=second, actor="synthetic-second"
                )
                await db.commit()
                return "committed"
            except LiveReadinessError:
                await db.rollback()
                return "refused"

    async with live_sessions() as holder:
        await LiveReadinessService(holder, owner_id=owner_id).apply_action(
            action=first, actor="synthetic-first"
        )
        pending = asyncio.create_task(waiter())
        try:
            pid = await asyncio.wait_for(started, 1)
            async with live_sessions() as observer:
                # Observe an actual PostgreSQL lock wait, not a timing assumption.
                async with asyncio.timeout(1):
                    while not await observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                    ):
                        await asyncio.sleep(0.01)
            assert not pending.done()
            await holder.commit()
            outcome = await asyncio.wait_for(pending, 3)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert outcome == ("refused" if second == "unlock_live" else "committed")
    async with live_sessions() as check:
        state = await check.get(AppSettings, 1)
        assert state.live_trading_unlocked is False
        anchor = await check.get(AuditLog, state.live_readiness_audit_id)
        assert anchor.action == "live_trading_locked"
        assert not (await LiveReadinessService(check, owner_id=owner_id).evaluate())[
            "ready_for_live"
        ]
        assert (
            len(
                (
                    await check.execute(
                        select(AuditLog).where(AuditLog.action == "live_trading_locked")
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )
