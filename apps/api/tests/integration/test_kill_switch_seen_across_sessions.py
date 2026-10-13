"""PostgreSQL proof that a halt committed by one session stops the risk checks of another
session that read its settings earlier and is still holding them.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import AppSettings
from app.db.session import Base
from app.risk.engine import RiskEngine, RiskViolation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the two-session proof",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def sessions() -> AsyncGenerator[Any, None]:
    """Sessions configured as the application's are, on a schema of this test's own."""
    assert POSTGRES_TEST_DATABASE_URL is not None
    schema = f"kill_switch_{uuid.uuid4().hex}"
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
        async with factory() as setup:
            setup.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
            await setup.commit()
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def _set(sessions: Any, **values: bool) -> None:
    async with sessions() as operator:
        row = await operator.get(AppSettings, 1)
        for name, value in values.items():
            setattr(row, name, value)
        await operator.commit()


async def test_a_halt_committed_elsewhere_stops_a_run_that_is_already_under_way(
    sessions: Any,
) -> None:
    async with sessions() as run:
        risk = RiskEngine(run)
        held_by_the_run = await risk._get_settings()
        await risk.check_kill_switch()
        await run.commit()

        await _set(sessions, kill_switch_active=True)

        assert held_by_the_run.kill_switch_active is False, "the run's copy is not stale"
        with pytest.raises(RiskViolation) as blocked:
            await risk.check_kill_switch()
    assert blocked.value.event_type == "kill_switch_block"


async def test_auto_trading_switched_off_elsewhere_stops_a_run_that_is_already_under_way(
    sessions: Any,
) -> None:
    async with sessions() as run:
        risk = RiskEngine(run)
        held_by_the_run = await risk._get_settings()
        await risk.check_auto_trading_enabled()
        await run.commit()

        await _set(sessions, auto_trading_enabled=False)

        assert held_by_the_run.auto_trading_enabled is True, "the run's copy is not stale"
        with pytest.raises(RiskViolation) as blocked:
            await risk.check_auto_trading_enabled()
    assert blocked.value.event_type == "auto_trading_disabled"


async def test_a_run_goes_on_while_nothing_has_been_switched(sessions: Any) -> None:
    async with sessions() as run:
        risk = RiskEngine(run)
        held_by_the_run = await risk._get_settings()
        await run.commit()

        await _set(sessions, theme="light")

        await risk.check_kill_switch()
        await risk.check_auto_trading_enabled()
    assert held_by_the_run.kill_switch_active is False
