"""PostgreSQL proof that generic edits cannot race promotion mutations."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import NullPool

from app.api.schemas import StrategyUpdate
from app.api.v1.routes.strategies import update_strategy
from app.db.models import AppSettings, AuditLog, Strategy
from app.services.strategy_promotion import StrategyPromotionService

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the promotion concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_stale_params_writer_cannot_restore_promotion() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy_id = uuid.uuid4()

    async with sessions() as setup:
        setup.add(
            Strategy(
                id=strategy_id,
                name="Stale promotion writer proof",
                type="orb",
                params={
                    "min_rvol": 1.5,
                    "promotion": {
                        "live_approved_at": "2026-08-23T00:00:00+00:00",
                        "live_approved_by": "ops",
                    },
                },
            )
        )
        await setup.commit()

    try:
        async with sessions() as stale_session:
            stale_strategy = (
                await stale_session.execute(select(Strategy).where(Strategy.id == strategy_id))
            ).scalar_one()

            async with sessions() as promotion_session:
                promoted_strategy = (
                    await promotion_session.execute(
                        select(Strategy).where(Strategy.id == strategy_id).with_for_update()
                    )
                ).scalar_one()
                promotion = dict(promoted_strategy.params["promotion"])
                promotion["live_approved_at"] = None
                promotion["live_approved_by"] = None
                promoted_strategy.params = {
                    **promoted_strategy.params,
                    "promotion": promotion,
                }
                await promotion_session.commit()

            stale_strategy.params = {
                **stale_strategy.params,
                "todays_watchlist": ["AAPL"],
            }
            with pytest.raises(StaleDataError):
                await stale_session.commit()
            await stale_session.rollback()

        async with sessions() as verify:
            strategy = (
                await verify.execute(select(Strategy).where(Strategy.id == strategy_id))
            ).scalar_one()
            assert strategy.params["promotion"]["live_approved_at"] is None
            assert strategy.params["promotion"]["live_approved_by"] is None
            assert "todays_watchlist" not in strategy.params
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(Strategy).where(Strategy.id == strategy_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the promotion concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_generic_update_serializes_with_promotion_revocation() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy_id = uuid.uuid4()
    generic_update_holds_lock = asyncio.Event()
    release_generic_update = asyncio.Event()
    settings_created = False

    async with sessions() as setup:
        if await setup.get(AppSettings, 1) is None:
            setup.add(
                AppSettings(
                    id=1,
                    auto_trading_enabled=False,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                )
            )
            settings_created = True
        setup.add(
            Strategy(
                id=strategy_id,
                name="Promotion concurrency proof",
                type="orb",
                params={
                    "min_rvol": 1.5,
                    "promotion": {
                        "live_approved_at": "2026-08-23T00:00:00+00:00",
                        "live_approved_by": "ops",
                    },
                },
            )
        )
        await setup.commit()

    async def hold_generic_update() -> None:
        async with sessions() as session:
            await update_strategy(
                strategy_id,
                StrategyUpdate(params={"min_rvol": 2.0}),
                SimpleNamespace(email="editor@example.test"),
                session,
            )
            generic_update_holds_lock.set()
            await release_generic_update.wait()
            await session.commit()

    async def revoke_live_promotion() -> None:
        await generic_update_holds_lock.wait()
        async with sessions() as session:
            await StrategyPromotionService(session).apply_action(
                strategy_id=strategy_id,
                action="revoke_live_promotion",
                actor="ops@example.test",
            )
            await session.commit()

    generic_task = asyncio.create_task(hold_generic_update())
    revoke_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(generic_update_holds_lock.wait(), timeout=5)
        revoke_task = asyncio.create_task(revoke_live_promotion())
        await asyncio.sleep(0.2)
        assert not revoke_task.done(), "revocation must wait for the generic update's row lock"

        release_generic_update.set()
        await asyncio.wait_for(asyncio.gather(generic_task, revoke_task), timeout=10)

        async with sessions() as verify:
            strategy = (
                await verify.execute(select(Strategy).where(Strategy.id == strategy_id))
            ).scalar_one()
            assert strategy.params["min_rvol"] == 2.0
            assert strategy.params["promotion"]["live_approved_at"] is None
            assert strategy.params["promotion"]["live_approved_by"] is None
    finally:
        release_generic_update.set()
        if not generic_task.done():
            generic_task.cancel()
        if revoke_task is not None and not revoke_task.done():
            revoke_task.cancel()
        await asyncio.gather(
            generic_task,
            *(task for task in [revoke_task] if task is not None),
            return_exceptions=True,
        )
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AuditLog).where(
                    AuditLog.entity_type == "strategy",
                    AuditLog.entity_id == str(strategy_id),
                )
            )
            await cleanup.execute(delete(Strategy).where(Strategy.id == strategy_id))
            if settings_created:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            await cleanup.commit()
        await engine.dispose()
