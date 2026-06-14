from __future__ import annotations

from datetime import UTC
from datetime import datetime as _dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.services.execution_quality as _eq
from app.db.models import Alert, AppSettings
from app.execution.engine import ExecutionEngine
from app.execution.state_machine import InvalidOrderTransition


class DummyBroker:
    environment = "demo"


class FilledBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {
            "id": "BROKER-FILL-1",
            "status": "FILLED",
            "filledQuantity": abs(float(quantity)),
            "filledPrice": 101.0,
            "timeValidity": time_validity,
        }


class RejectedBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {"id": "B-REJ", "status": "REJECTED", "filledQuantity": 0, "filledPrice": 0}


class CancelledBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {"id": "B-CAN", "status": "CANCELLED", "filledQuantity": 0, "filledPrice": 0}


class WorkingBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {"id": "B-WRK", "status": "WORKING", "filledQuantity": 0, "filledPrice": 0}

    async def get_order_by_id(self, broker_order_id):
        return {"id": broker_order_id, "status": "WORKING", "filledQuantity": 0, "filledPrice": 0}

    async def cancel_order(self, broker_order_id):
        pass


class FilledOnReconcileBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        return {"id": "B-FOR", "status": "WORKING", "filledQuantity": 0, "filledPrice": 0}

    async def get_order_by_id(self, broker_order_id):
        return {
            "id": broker_order_id,
            "status": "FILLED",
            "filledQuantity": 10.0,
            "filledPrice": 101.5,
        }

    async def cancel_order(self, broker_order_id):
        pass


class ErrorBroker:
    environment = "demo"

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        raise RuntimeError("Broker unavailable")


class LimitBroker:
    environment = "demo"

    async def place_limit_order(self, ticker, quantity, limit_price, time_validity="DAY"):
        return {"id": "B-LIM", "status": "WORKING", "filledQuantity": 0, "filledPrice": 0}


@pytest_asyncio.fixture(autouse=True)
async def execution_policy_ready(db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=True,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    await db.flush()


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


@pytest.mark.asyncio
async def test_execution_engine_records_execution_quality_and_slippage_alert(db):
    engine = ExecutionEngine(db, FilledBroker())

    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)

    assert order.status == "filled"
    assert order.execution_environment == "demo"
    assert order.expected_fill_price == Decimal("100")
    assert order.slippage_pct == Decimal("1.0000")
    assert order.slippage_value == Decimal("10.0000")
    assert order.execution_quality_score == Decimal("82.00")
    assert order.execution_quality_grade == "good"
    assert order.submitted_at is not None
    assert order.first_ack_at is not None
    assert order.filled_at is not None
    assert order.broker_latency_ms is not None
    assert order.fill_latency_ms is not None

    alert = (
        await db.execute(select(Alert).where(Alert.alert_type == "abnormal_slippage"))
    ).scalar_one()
    assert alert.payload["order_id"] == str(order.id)


# ── submit_order branch coverage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_order_dry_run_fills_without_broker_call(db):
    engine = ExecutionEngine(db, DummyBroker())
    order = await engine.create_order_intent(
        ticker="TSLA",
        side="buy",
        order_type="market",
        quantity=Decimal("5"),
        estimated_price=Decimal("200"),
        is_dry_run=True,
    )
    order = await engine.submit_order(order)
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("5")
    assert order.broker_latency_ms == 0


@pytest.mark.asyncio
async def test_submit_order_rejected_by_broker(db):
    engine = ExecutionEngine(db, RejectedBroker())
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "rejected"
    assert order.rejected_at is not None


@pytest.mark.asyncio
async def test_submit_order_cancelled_by_broker(db):
    engine = ExecutionEngine(db, CancelledBroker())
    order = await engine.create_order_intent(
        ticker="MSFT",
        side="buy",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "cancelled"
    assert order.cancelled_at is not None


@pytest.mark.asyncio
async def test_submit_order_working_becomes_accepted(db):
    engine = ExecutionEngine(db, WorkingBroker())
    order = await engine.create_order_intent(
        ticker="GOOG",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "accepted"
    assert order.broker_order_id == "B-WRK"


@pytest.mark.asyncio
async def test_submit_order_broker_error_sets_error_status(db, monkeypatch):
    # _infer_terminal_time accesses order.updated_at for "error" status; that column
    # is expired in the aiosqlite test DB after the pre-broker flush.  Patch it to
    # avoid the lazy-load so we can exercise the rest of the error path.
    monkeypatch.setattr(_eq, "_infer_terminal_time", lambda _o, _s: _dt.now(UTC))

    engine = ExecutionEngine(db, ErrorBroker())
    order = await engine.create_order_intent(
        ticker="AMZN",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "error"
    assert "Broker unavailable" in order.error_message


@pytest.mark.asyncio
async def test_submit_order_non_pending_intent_raises_value_error(db):
    engine = ExecutionEngine(db, WorkingBroker())
    order = await engine.create_order_intent(
        ticker="NVDA",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)  # → accepted
    with pytest.raises(ValueError, match="Cannot submit order"):
        await engine.submit_order(order)


@pytest.mark.asyncio
async def test_submit_limit_order_becomes_accepted(db):
    engine = ExecutionEngine(db, LimitBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("300"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "accepted"
    assert order.broker_order_id == "B-LIM"


# ── reconcile_order branch coverage ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_order_fills_accepted(db):
    engine = ExecutionEngine(db, FilledOnReconcileBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)  # → accepted (WORKING)
    assert order.status == "accepted"
    order = await engine.reconcile_order(order)
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("10")
    assert order.avg_fill_price == Decimal("101.5")


@pytest.mark.asyncio
async def test_reconcile_order_skips_already_filled(db):
    engine = ExecutionEngine(db, FilledBroker())
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("5"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)  # FilledBroker → filled immediately
    assert order.status == "filled"
    order = await engine.reconcile_order(order)  # no-op: not in accepted/submitted
    assert order.status == "filled"


# ── cancel_order coverage ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_order_sets_cancelled_status(db):
    engine = ExecutionEngine(db, WorkingBroker())
    order = await engine.create_order_intent(
        ticker="SPY",
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)  # → accepted
    order = await engine.cancel_order(order)
    assert order.status == "cancelled"
    assert order.cancelled_at is not None


@pytest.mark.asyncio
async def test_cancel_order_rejects_already_filled_order(db):
    engine = ExecutionEngine(db, FilledBroker())
    order = await engine.create_order_intent(
        ticker="SPY",
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)

    with pytest.raises(InvalidOrderTransition):
        await engine.cancel_order(order)

    assert order.status == "filled"
    assert order.cancelled_at is None


# ── client_order_key idempotency ──────────────────────────────────────────────


def test_client_order_key_deterministic_for_signal():
    engine = ExecutionEngine(None, None)
    sig_id = "signal-abc-123"
    k1 = engine._make_client_order_key("AAPL", "buy", sig_id)
    k2 = engine._make_client_order_key("AAPL", "buy", sig_id)
    assert k1 == k2


def test_client_order_key_differs_by_ticker():
    engine = ExecutionEngine(None, None)
    sig_id = "sig-1"
    assert engine._make_client_order_key("AAPL", "buy", sig_id) != engine._make_client_order_key(
        "MSFT", "buy", sig_id
    )


def test_client_order_key_differs_with_different_salt():
    engine = ExecutionEngine(None, None)
    k1 = engine._make_client_order_key("AAPL", "buy", None, salt="abc")
    k2 = engine._make_client_order_key("AAPL", "buy", None, salt="xyz")
    assert k1 != k2
