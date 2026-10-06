"""Transactional and PostgreSQL concurrency proof for order transitions."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import NullPool

from app.db.models import AuditLog, Order, OrderEvent
from app.execution.engine import ExecutionEngine
from app.execution.state_machine import (
    InvalidOrderTransition,
    transition_order_status_with_evidence,
)

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")


def _order(*, order_id: uuid.UUID | None = None, key: str | None = None) -> Order:
    return Order(
        id=order_id or uuid.uuid4(),
        client_order_key=key or f"atomic-{uuid.uuid4()}",
        ticker="ATOMIC",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        status="accepted",
        venue="paper",
        is_dry_run=True,
    )


@pytest.mark.asyncio
async def test_transition_status_event_and_audit_roll_back_together(db: AsyncSession) -> None:
    order = _order()
    order_id = order.id
    db.add(order)
    await db.commit()

    transition_order_status_with_evidence(
        db,
        order,
        "filled",
        event_type="atomic_fill",
        reason="deterministic test fill",
        actor="test_worker",
        correlation_id="rollback-proof",
    )
    await db.flush()
    await db.rollback()

    reloaded = await db.get(Order, order_id, populate_existing=True)
    event_count = await db.scalar(
        select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order_id)
    )
    audit_count = await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity_id == str(order_id),
            AuditLog.action == "order_status_transitioned",
        )
    )
    assert reloaded is not None and reloaded.status == "accepted"
    assert event_count == 0
    assert audit_count == 0


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_concurrent_transition_allows_exactly_one_commit() -> None:
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    order_id = uuid.uuid4()
    correlation_id = f"race-{order_id}"

    async with sessions() as setup:
        setup.add(_order(order_id=order_id, key=f"atomic-race-{order_id}"))
        await setup.commit()

    ready = 0
    both_loaded = asyncio.Event()

    async def attempt(actor: str) -> str:
        nonlocal ready
        async with sessions() as session:
            order = await session.get(Order, order_id)
            assert order is not None
            ready += 1
            if ready == 2:
                both_loaded.set()
            await both_loaded.wait()
            transition_order_status_with_evidence(
                session,
                order,
                "filled",
                event_type="concurrent_fill",
                reason="same broker fill observed concurrently",
                actor=actor,
                correlation_id=correlation_id,
            )
            try:
                await session.commit()
            except StaleDataError:
                await session.rollback()
                return "stale"
            return "committed"

    try:
        assert sorted(await asyncio.gather(attempt("worker-a"), attempt("worker-b"))) == [
            "committed",
            "stale",
        ]
        async with sessions() as verify:
            order = await verify.get(Order, order_id)
            event_count = await verify.scalar(
                select(func.count())
                .select_from(OrderEvent)
                .where(OrderEvent.order_id == order_id, OrderEvent.event_type == "concurrent_fill")
            )
            audit_count = await verify.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.entity_id == str(order_id),
                    AuditLog.action == "order_status_transitioned",
                )
            )
            assert order is not None and order.status == "filled" and order.version == 2
            assert event_count == 1
            assert audit_count == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id == str(order_id)))
            await cleanup.execute(delete(Order).where(Order.id == order_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the concurrency proof",
)
@pytest.mark.asyncio
async def test_postgres_concurrent_cancel_calls_broker_exactly_once() -> None:
    class CountingBroker:
        environment = "demo"

        def __init__(self) -> None:
            self.cancel_calls = 0

        async def cancel_order(self, _broker_order_id: str) -> None:
            self.cancel_calls += 1

    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    order_id = uuid.uuid4()
    broker = CountingBroker()

    async with sessions() as setup:
        order = _order(order_id=order_id, key=f"atomic-cancel-{order_id}")
        order.broker_order_id = f"broker-{order_id}"
        order.is_dry_run = False
        setup.add(order)
        await setup.commit()

    ready = 0
    both_loaded = asyncio.Event()

    async def attempt() -> str:
        nonlocal ready
        async with sessions() as session:
            order = await session.get(Order, order_id)
            assert order is not None
            ready += 1
            if ready == 2:
                both_loaded.set()
            await both_loaded.wait()
            try:
                await ExecutionEngine(session, broker).cancel_order(order)
                await session.commit()
            except InvalidOrderTransition:
                await session.rollback()
                return "terminal"
            except StaleDataError:
                await session.rollback()
                return "stale"
            return "committed"

    try:
        assert sorted(await asyncio.gather(attempt(), attempt())) == ["committed", "committed"]
        assert broker.cancel_calls == 1
        async with sessions() as verify:
            event_count = await verify.scalar(
                select(func.count())
                .select_from(OrderEvent)
                .where(OrderEvent.order_id == order_id, OrderEvent.event_type == "cancelled")
            )
            assert event_count == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id == str(order_id)))
            await cleanup.execute(delete(Order).where(Order.id == order_id))
            await cleanup.commit()
        await engine.dispose()
