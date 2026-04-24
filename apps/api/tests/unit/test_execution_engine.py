from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.engine import ExecutionEngine


class DummyBroker:
    pass


@pytest.mark.asyncio
async def test_execution_engine_blocks_recent_duplicate_manual_intent(db):
    engine = ExecutionEngine(db, DummyBroker())

    first = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("5"),
        limit_price=Decimal("180"),
        time_validity="DAY",
        is_dry_run=False,
    )
    second = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("5"),
        limit_price=Decimal("180"),
        time_validity="DAY",
        is_dry_run=False,
    )

    assert second.id == first.id


@pytest.mark.asyncio
async def test_execution_engine_allows_distinct_recent_intent(db):
    engine = ExecutionEngine(db, DummyBroker())

    first = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("5"),
        limit_price=Decimal("180"),
        time_validity="DAY",
        is_dry_run=False,
    )
    second = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("6"),
        limit_price=Decimal("180"),
        time_validity="DAY",
        is_dry_run=False,
    )

    assert second.id != first.id
