"""PostgreSQL concurrency proof for order and scheduled-signal creation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Order, OrderEvent, Signal, Strategy
from app.execution.engine import ExecutionEngine
from app.services.strategy_runner import StrategyRunner

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")


def test_signal_decision_key_is_stable_per_bar_and_changes_for_later_bar() -> None:
    strategy_id = uuid.uuid4()
    first_bar = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    later_bar = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    inputs = {
        "strategy_id": strategy_id,
        "ticker": "idem",
        "side": "buy",
        "signal_type": "entry",
    }

    first = StrategyRunner._signal_decision_key(**inputs, bar_time=first_bar)
    repeated = StrategyRunner._signal_decision_key(**inputs, bar_time=first_bar)
    later = StrategyRunner._signal_decision_key(**inputs, bar_time=later_bar)

    assert first == repeated
    assert first != later


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_concurrent_identical_order_create_returns_one_intent() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy_id = uuid.uuid4()
    signal_id = uuid.uuid4()

    async with sessions() as setup:
        setup.add(Strategy(id=strategy_id, name="Idempotency", type="orb", params={}))
        setup.add(
            Signal(
                id=signal_id,
                strategy_id=strategy_id,
                ticker="IDEM",
                side="buy",
                signal_type="entry",
                status="pending",
            )
        )
        await setup.commit()

    async def attempt() -> uuid.UUID:
        async with sessions() as session:
            order = await ExecutionEngine(session, broker=None).create_order_intent(
                ticker="IDEM",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                signal_id=signal_id,
                limit_price=Decimal("100"),
                is_dry_run=True,
            )
            await session.commit()
            return order.id

    try:
        order_ids = await asyncio.gather(attempt(), attempt())
        assert order_ids[0] == order_ids[1]
        async with sessions() as verify:
            order_count = await verify.scalar(
                select(func.count()).select_from(Order).where(Order.signal_id == signal_id)
            )
            event_count = await verify.scalar(
                select(func.count())
                .select_from(OrderEvent)
                .join(Order, Order.id == OrderEvent.order_id)
                .where(Order.signal_id == signal_id, OrderEvent.event_type == "intent_created")
            )
            assert order_count == 1
            assert event_count == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(Order).where(Order.signal_id == signal_id))
            await cleanup.execute(delete(Signal).where(Signal.id == signal_id))
            await cleanup.execute(delete(Strategy).where(Strategy.id == strategy_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_concurrent_identical_signal_create_returns_one_signal() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy_id = uuid.uuid4()
    decision_key = f"decision-{uuid.uuid4()}"

    async with sessions() as setup:
        setup.add(Strategy(id=strategy_id, name="Signal Idempotency", type="orb", params={}))
        await setup.commit()

    async def attempt() -> tuple[uuid.UUID, bool]:
        async with sessions() as session:
            signal, created = await StrategyRunner(session)._create_signal_once(
                Signal(
                    id=uuid.uuid4(),
                    strategy_id=strategy_id,
                    decision_key=decision_key,
                    ticker="IDEM",
                    side="buy",
                    signal_type="entry",
                    status="pending",
                    generated_at=datetime(2026, 1, 2, 15, 0, tzinfo=UTC),
                )
            )
            await session.commit()
            return signal.id, created

    try:
        results = await asyncio.gather(attempt(), attempt())
        assert {created for _, created in results} == {False, True}
        assert len({signal_id for signal_id, _ in results}) == 1
        async with sessions() as verify:
            count = await verify.scalar(
                select(func.count()).select_from(Signal).where(Signal.decision_key == decision_key)
            )
            assert count == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(Signal).where(Signal.strategy_id == strategy_id))
            await cleanup.execute(delete(Strategy).where(Strategy.id == strategy_id))
            await cleanup.commit()
        await engine.dispose()
