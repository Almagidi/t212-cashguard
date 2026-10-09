"""Durable database identity proofs for EOD flatten operations."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import EodFlattenOperation, Strategy
from app.services.eod_flatten import _filled_quantity


def test_nonfinite_filled_quantity_is_rejected_as_ambiguous() -> None:
    order = SimpleNamespace(
        ticker="AAPL",
        filled_quantity=Decimal("NaN"),
        quantity=Decimal("2"),
        status="filled",
    )

    with pytest.raises(ValueError, match="Non-finite filled quantity"):
        _filled_quantity(order)


@pytest.mark.asyncio
async def test_operation_identity_is_unique_per_strategy_session_venue_and_ticker(db) -> None:
    strategy = Strategy(id=uuid.uuid4(), name="EOD identity", type="orb", params={})
    db.add(strategy)
    await db.flush()

    identity = {
        "operation_kind": "eod_flatten",
        "strategy_id": strategy.id,
        "venue": "t212",
        "exchange_session_date": date(2026, 7, 6),
        "ticker": "AAPL",
        "exchange": "XNYS",
        "execution_environment": "demo",
        "broker_account_scope": "trading212:demo:user:test",
        "attributable_quantity": Decimal("2"),
    }
    db.add(EodFlattenOperation(id=uuid.uuid4(), **identity))
    await db.flush()
    db.add(EodFlattenOperation(id=uuid.uuid4(), **identity))

    with pytest.raises(IntegrityError):
        await db.flush()


def test_operation_table_declares_durable_identity_and_order_constraints() -> None:
    constraints = {
        constraint.name
        for constraint in EodFlattenOperation.__table__.constraints
        if constraint.name
    }

    assert "uq_eod_flatten_operation_identity" in constraints
    assert "uq_eod_flatten_operation_order" in constraints
    assert EodFlattenOperation.__table__.c.strategy_id.nullable is False
    assert EodFlattenOperation.__table__.c.exchange_session_date.nullable is False
    assert EodFlattenOperation.__table__.c.order_id.nullable is True
