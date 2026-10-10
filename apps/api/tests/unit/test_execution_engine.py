from __future__ import annotations

from datetime import UTC, timedelta
from datetime import datetime as _dt
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

import app.services.execution_quality as _eq
from app.broker.protocols import BrokerSubmissionNotTransmitted, BrokerSubmissionRejected
from app.db.models import (
    Alert,
    AppSettings,
    AuditLog,
    Order,
    OrderEvent,
    OrderSubmissionAttempt,
    RiskEvent,
)
from app.execution.engine import (
    SUBMISSION_UNKNOWN_MESSAGE,
    ExecutionEngine,
    OrderCancellationFailed,
)
from app.execution.state_machine import (
    ACTIVE_ORDER_STATUSES,
    InvalidOrderTransition,
    transition_order_status,
)


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


class CancelErrorBroker(WorkingBroker):
    async def cancel_order(self, broker_order_id):
        raise RuntimeError("Authorization: Bearer super-secret-token")


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


class StaleFilledOnReconcileBroker(FilledOnReconcileBroker):
    async def get_order_by_id(self, broker_order_id):
        response = await super().get_order_by_id(broker_order_id)
        return {**response, "filledQuantity": 5, "filledPrice": 101}


class PartiallyFilledThenCancelledBroker(WorkingBroker):
    async def get_order_by_id(self, broker_order_id):
        return {
            "id": broker_order_id,
            "status": "CANCELLED",
            "filledQuantity": 7,
            "filledPrice": 101,
        }


class PartiallyFilledThenRejectedBroker(PartiallyFilledThenCancelledBroker):
    async def get_order_by_id(self, broker_order_id):
        response = await super().get_order_by_id(broker_order_id)
        return {**response, "status": "REJECTED"}


class StalePartialThenCancelledBroker(PartiallyFilledThenCancelledBroker):
    async def get_order_by_id(self, broker_order_id):
        response = await super().get_order_by_id(broker_order_id)
        return {**response, "filledQuantity": 5, "filledPrice": 101}


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
async def test_execution_engine_stable_operation_identity_survives_terminal_order(db):
    engine = ExecutionEngine(db, DummyBroker())
    operation_identity = "eod_flatten:strategy-1:t212:2026-07-06:AAPL"

    first = await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=True,
        stable_operation_identity=operation_identity,
    )
    first.status = "filled"
    first.filled_quantity = first.quantity
    await db.flush()

    replay = await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=True,
        stable_operation_identity=operation_identity,
    )

    assert replay.id == first.id
    assert replay.client_order_key == first.client_order_key


@pytest.mark.asyncio
async def test_execution_engine_distinct_operation_identities_do_not_deduplicate(db):
    engine = ExecutionEngine(db, DummyBroker())

    first = await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=True,
        stable_operation_identity="eod_flatten:strategy-1:t212:2026-07-06:AAPL",
    )
    second = await engine.create_order_intent(
        ticker="AAPL",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=True,
        stable_operation_identity="eod_flatten:strategy-1:t212:2026-07-07:AAPL",
    )

    assert second.id != first.id
    assert second.client_order_key != first.client_order_key


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
async def test_submit_order_unclassified_broker_failure_is_unknown_not_terminal(db):
    engine = ExecutionEngine(db, ErrorBroker())
    order = await engine.create_order_intent(
        ticker="AMZN",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    assert order.status == "submission_unknown"
    assert order.error_message == SUBMISSION_UNKNOWN_MESSAGE
    event = (
        await db.execute(
            select(OrderEvent).where(OrderEvent.event_type == "submission_outcome_unknown")
        )
    ).scalar_one()
    assert event.payload["error_type"] == "RuntimeError"
    assert "Broker unavailable" not in str(event.payload)


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
async def test_reconcile_order_fills_active_partial_remainder(db):
    engine = ExecutionEngine(db, FilledOnReconcileBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    transition_order_status(order, "partially_filled")
    order.filled_quantity = Decimal("5")
    order.avg_fill_price = Decimal("100")
    _eq.apply_order_execution_quality(order)

    order = await engine.reconcile_order(order)

    assert order.status == "filled"
    assert order.filled_quantity == Decimal("10")
    assert order.remaining_quantity == Decimal("0")
    assert order.avg_fill_price == Decimal("101.5")
    assert order.slippage_pct == Decimal("1.5000")
    assert order.slippage_value == Decimal("15.0000")
    assert "pending" not in order.execution_quality_notes


@pytest.mark.asyncio
async def test_reconcile_stale_filled_response_cannot_regress_partial_quantity(db):
    engine = ExecutionEngine(db, StaleFilledOnReconcileBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    transition_order_status(order, "partially_filled")
    order.filled_quantity = Decimal("7")
    order.avg_fill_price = Decimal("100")

    order = await engine.reconcile_order(order)

    assert order.status == "partially_filled"
    assert order.filled_quantity == Decimal("7")
    assert order.avg_fill_price == Decimal("100")
    assert order.remaining_quantity == Decimal("3")


@pytest.mark.asyncio
async def test_reconcile_terminal_partial_applies_broker_final_fill(db):
    engine = ExecutionEngine(db, PartiallyFilledThenCancelledBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    transition_order_status(order, "partially_filled")
    order.filled_quantity = Decimal("5")
    order.avg_fill_price = Decimal("100")

    order = await engine.reconcile_order(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("7")
    assert order.avg_fill_price == Decimal("101")
    assert order.remaining_quantity == Decimal("3")
    assert order.slippage_pct == Decimal("1.0000")
    assert order.slippage_value == Decimal("7.0000")
    assert "pending" not in order.execution_quality_notes
    event = (
        await db.execute(
            select(OrderEvent).where(
                OrderEvent.order_id == order.id, OrderEvent.event_type == "reconciled_status"
            )
        )
    ).scalar_one()
    assert event.payload["filled_quantity"] == "7"
    assert event.payload["remaining_quantity"] == "3"


@pytest.mark.asyncio
async def test_reconcile_rejected_partial_is_terminal_and_atomic(db):
    engine = ExecutionEngine(db, PartiallyFilledThenRejectedBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    transition_order_status(order, "partially_filled")
    order.filled_quantity = Decimal("5")
    order.avg_fill_price = Decimal("100")

    order = await engine.reconcile_order(order)

    assert order.status == "rejected"
    assert order.filled_quantity == Decimal("7")
    assert order.avg_fill_price == Decimal("101")
    assert order.remaining_quantity == Decimal("3")
    assert order.broker_response["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_reconcile_stale_terminal_partial_keeps_quantity_and_price_together(db):
    engine = ExecutionEngine(db, StalePartialThenCancelledBroker())
    order = await engine.create_order_intent(
        ticker="META",
        side="buy",
        order_type="market",
        quantity=Decimal("10"),
        estimated_price=Decimal("100"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    transition_order_status(order, "partially_filled")
    order.filled_quantity = Decimal("7")
    order.avg_fill_price = Decimal("100")

    order = await engine.reconcile_order(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("7")
    assert order.avg_fill_price == Decimal("100")
    assert order.slippage_value == Decimal("0.0000")


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
async def test_cancel_order_failure_records_safe_error_without_claiming_cancelled(db):
    engine = ExecutionEngine(db, CancelErrorBroker())
    order = await engine.create_order_intent(
        ticker="SPY",
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)

    with pytest.raises(OrderCancellationFailed) as exc_info:
        await engine.cancel_order(order)
    order = exc_info.value.order

    assert order.status == "error"
    assert order.cancelled_at is None
    assert order.error_message == "Broker request failed with RuntimeError."
    event = (
        await db.execute(select(OrderEvent).where(OrderEvent.event_type == "cancel_failed"))
    ).scalar_one()
    assert "super-secret-token" not in str(event.payload)


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


# ── Unit 1: ambiguous broker submissions ──────────────────────────────────────


class TimeoutAfterTransmissionBroker:
    """Accepts the order at the "broker", then the response is lost."""

    environment = "demo"

    def __init__(self):
        self.posts: list[dict] = []
        self.cancel_calls: list[str] = []
        self.history: list[dict] = []
        self.history_calls = 0

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.posts.append({"ticker": ticker, "quantity": quantity})
        raise httpx.ReadTimeout("response lost after the broker accepted the order")

    async def cancel_order(self, broker_order_id):
        self.cancel_calls.append(broker_order_id)

    async def get_historical_orders(self, cursor=None, ticker=None, limit=50):
        self.history_calls += 1
        return {"items": list(self.history), "nextPagePath": None}


class NotTransmittedBroker(TimeoutAfterTransmissionBroker):
    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.posts.append({"ticker": ticker, "quantity": quantity})
        raise BrokerSubmissionNotTransmitted(error_type="ConnectError")


class RefusingBroker(TimeoutAfterTransmissionBroker):
    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.posts.append({"ticker": ticker, "quantity": quantity})
        raise BrokerSubmissionRejected(error_type="HTTPStatus", http_status=422)


class UnusableAcknowledgementBroker(TimeoutAfterTransmissionBroker):
    def __init__(self, response):
        super().__init__()
        self.response = response

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        self.posts.append({"ticker": ticker, "quantity": quantity})
        return self.response


@pytest.fixture
def reconcile_immediately(monkeypatch):
    """Let reconciliation look at a dispatch straight away instead of after the minimum age."""
    monkeypatch.setattr(ExecutionEngine, "UNKNOWN_SUBMISSION_MIN_AGE", timedelta(0))


async def _unknown_order(db, broker, *, ticker="AAPL", side="buy", quantity="2"):
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker=ticker,
        side=side,
        order_type="market",
        quantity=Decimal(quantity),
        is_dry_run=False,
        estimated_price=Decimal("50"),
    )
    return engine, await engine.submit_order(order)


def _history_item(order, **overrides):
    signed = -float(order.quantity) if order.side == "sell" else float(order.quantity)
    item = {
        "id": "BROKER-77",
        "ticker": order.ticker,
        "status": "FILLED",
        "type": "MARKET",
        "quantity": signed,
        "filledQuantity": float(order.quantity),
        "filledPrice": 50.5,
        "createdAt": order.submitted_at.isoformat(),
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_timeout_after_transmission_stays_active_and_is_never_terminal(db):
    broker = TimeoutAfterTransmissionBroker()
    _, order = await _unknown_order(db, broker)

    assert order.status == "submission_unknown"
    assert order.status in ACTIVE_ORDER_STATUSES
    assert order.rejected_at is None
    assert order.error_message == SUBMISSION_UNKNOWN_MESSAGE
    assert len(broker.posts) == 1

    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.order_id == order.id
    assert attempt.outcome == "ambiguous"
    assert attempt.outcome_evidence == {"error_type": "ReadTimeout", "http_status": None}
    assert attempt.expected_cost == Decimal("100")
    assert len(attempt.request_fingerprint) == 64

    risk_event = (
        await db.execute(select(RiskEvent).where(RiskEvent.event_type == "submission_unknown"))
    ).scalar_one()
    assert risk_event.order_id == order.id
    assert risk_event.payload["severity"] == "critical"
    assert risk_event.payload["automatic_resubmission"] is False
    assert "response lost" not in str(risk_event.payload)

    alert = (
        await db.execute(select(Alert).where(Alert.alert_type == "submission_unknown"))
    ).scalar_one()
    assert alert.severity == "critical"

    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "demo_broker_order_failure"))
    ).scalar_one()
    assert audit.payload["decision"] == "unknown"
    assert audit.payload["no_broker_order_sent"] is False


@pytest.mark.asyncio
async def test_unknown_submission_is_never_resubmitted(db):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)

    with pytest.raises(ValueError, match="submission_unknown"):
        await engine.submit_order(order)

    assert len(broker.posts) == 1
    assert len((await db.execute(select(OrderSubmissionAttempt))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unknown_submission_blocks_an_identical_intent_beyond_the_lookback(db):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    # Older than the ordinary duplicate window.
    order.created_at = _dt.now(UTC) - timedelta(seconds=engine.DUPLICATE_LOOKBACK_SECONDS * 10)
    await db.commit()

    again = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=False,
        estimated_price=Decimal("50"),
    )

    assert again.id == order.id
    assert len((await db.execute(select(Order))).scalars().all()) == 1
    assert len(broker.posts) == 1


@pytest.mark.asyncio
async def test_dispatch_evidence_is_committed_before_the_broker_is_called(db, monkeypatch):
    timeline: list[str] = []
    real_commit = db.commit

    async def recording_commit():
        timeline.append("commit")
        await real_commit()

    class RecordingBroker(WorkingBroker):
        async def place_market_order(self, ticker, quantity, time_validity="DAY"):
            attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
            persisted = (await db.execute(select(Order))).scalar_one()
            timeline.append(f"post:{persisted.status}:{attempt.outcome}")
            return await super().place_market_order(ticker, quantity, time_validity)

    monkeypatch.setattr(db, "commit", recording_commit)
    engine = ExecutionEngine(db, RecordingBroker())
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )
    order = await engine.submit_order(order)

    assert timeline == ["commit", "post:submission_unknown:None", "commit"]
    assert order.status == "accepted"
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "accepted"
    assert attempt.outcome_evidence == {"broker_order_id": "B-WRK", "broker_status": "WORKING"}


@pytest.mark.asyncio
async def test_failure_to_commit_the_dispatch_evidence_means_no_broker_call(db, monkeypatch):
    broker = TimeoutAfterTransmissionBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )

    async def failing_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await engine.submit_order(order)

    assert broker.posts == []


@pytest.mark.asyncio
async def test_request_that_was_not_transmitted_ends_in_error(db, monkeypatch):
    monkeypatch.setattr(_eq, "_infer_terminal_time", lambda _o, _s: _dt.now(UTC))
    broker = NotTransmittedBroker()
    _, order = await _unknown_order(db, broker)

    assert order.status == "error"
    assert order.error_message == "Broker request was not transmitted; no order was sent."
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "not_transmitted"
    assert (await db.execute(select(RiskEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_definitive_broker_refusal_ends_in_rejected(db):
    broker = RefusingBroker()
    _, order = await _unknown_order(db, broker)

    assert order.status == "rejected"
    assert order.rejected_at is not None
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "rejected"
    assert attempt.outcome_evidence == {"error_type": "HTTPStatus", "http_status": 422}
    assert (await db.execute(select(RiskEvent))).scalars().all() == []


@pytest.mark.parametrize(
    ("response", "expected_broker_order_id"),
    [
        ({"status": "WORKING"}, None),
        ({"id": "", "status": "WORKING"}, None),
        (["not", "an", "object"], None),
        ({"id": "B-9", "status": "FILLED", "filledQuantity": "not-a-number"}, "B-9"),
    ],
    ids=["no-id", "empty-id", "not-an-object", "unparseable-fill-keeps-id"],
)
@pytest.mark.asyncio
async def test_unusable_acknowledgement_leaves_the_order_unknown(
    db, response, expected_broker_order_id
):
    broker = UnusableAcknowledgementBroker(response)
    _, order = await _unknown_order(db, broker)

    assert order.status == "submission_unknown"
    assert order.broker_order_id == expected_broker_order_id
    assert order.filled_quantity is None
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "ambiguous"


@pytest.mark.asyncio
async def test_unknown_order_type_fails_before_anything_is_dispatched(db, monkeypatch):
    monkeypatch.setattr(_eq, "_infer_terminal_time", lambda _o, _s: _dt.now(UTC))
    broker = TimeoutAfterTransmissionBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="iceberg", quantity=Decimal("1"), is_dry_run=False
    )
    order = await engine.submit_order(order)

    assert order.status == "error"
    assert broker.posts == []
    assert (await db.execute(select(OrderSubmissionAttempt))).scalars().all() == []


@pytest.mark.asyncio
async def test_cancelling_an_unknown_submission_keeps_it_unknown(db):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)

    with pytest.raises(OrderCancellationFailed) as raised:
        await engine.cancel_order(order)

    assert raised.value.order.status == "submission_unknown"
    assert raised.value.order.cancelled_at is None
    assert broker.cancel_calls == []
    event = (
        await db.execute(
            select(OrderEvent).where(OrderEvent.event_type == "cancel_blocked_unknown_submission")
        )
    ).scalar_one()
    assert event.from_status == event.to_status == "submission_unknown"


@pytest.mark.asyncio
async def test_unknown_submission_resolves_from_one_matching_history_record(
    db, reconcile_immediately
):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    broker.history = [_history_item(order)]

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "filled"
    assert order.broker_order_id == "BROKER-77"
    assert order.filled_quantity == Decimal("2")
    assert order.avg_fill_price == Decimal("50.5")
    assert len(broker.posts) == 1
    event = (
        await db.execute(
            select(OrderEvent).where(OrderEvent.event_type == "unknown_submission_resolved")
        )
    ).scalar_one()
    assert event.from_status == "submission_unknown"
    assert event.payload["broker_order_id"] == "BROKER-77"


@pytest.mark.parametrize(
    "history_builder",
    [
        lambda _order: [],
        lambda order: [_history_item(order), _history_item(order, id="BROKER-78")],
        lambda order: [_history_item(order, ticker="MSFT")],
        lambda order: [_history_item(order, quantity=float(order.quantity) + 1)],
        lambda order: [_history_item(order, quantity=-float(order.quantity))],
        lambda order: [_history_item(order, type="LIMIT")],
        lambda order: [
            _history_item(order, createdAt=(order.submitted_at - timedelta(hours=3)).isoformat())
        ],
        lambda order: [_history_item(order, createdAt=None)],
    ],
    ids=[
        "no-record",
        "two-records",
        "other-ticker",
        "other-quantity",
        "other-side",
        "other-order-type",
        "created-long-before-dispatch",
        "no-creation-time",
    ],
)
@pytest.mark.asyncio
async def test_unknown_submission_stays_unresolved_without_exactly_one_match(
    db, history_builder, reconcile_immediately
):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    broker.history = history_builder(order)

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "submission_unknown"
    assert order.broker_order_id is None
    assert order.last_reconciled_at is not None
    assert broker.history_calls == 1
    assert len(broker.posts) == 1


@pytest.mark.asyncio
async def test_history_record_already_linked_to_another_order_is_not_adopted(
    db, reconcile_immediately
):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    other = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("49"),
        is_dry_run=True,
    )
    other.broker_order_id = "BROKER-77"
    await db.commit()
    broker.history = [_history_item(order)]

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "submission_unknown"
    assert order.broker_order_id is None


@pytest.mark.asyncio
async def test_history_failure_during_reconciliation_changes_nothing(db, reconcile_immediately):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)

    async def failing_history(cursor=None, ticker=None, limit=50):
        raise httpx.ReadTimeout("history unavailable")

    broker.get_historical_orders = failing_history

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "submission_unknown"
    assert len(broker.posts) == 1


@pytest.mark.asyncio
async def test_unknown_submission_keeps_its_expected_cash_hold(db):
    from app.db.repositories.order_repo import OrderRepository

    broker = TimeoutAfterTransmissionBroker()
    _, order = await _unknown_order(db, broker)

    assert order.cash_used == Decimal("100")
    assert order.id in {pending.id for pending in await OrderRepository(db).list_pending()}
    assert await OrderRepository(db).has_active_for_ticker_side("AAPL", "buy")


@pytest.mark.asyncio
async def test_reconciliation_waits_while_the_request_may_still_be_in_flight(db):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    broker.history = [_history_item(order)]

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "submission_unknown"
    assert broker.history_calls == 0


@pytest.mark.asyncio
async def test_sell_submission_resolves_from_a_negative_quantity_record(db, reconcile_immediately):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker, side="sell")
    broker.history = [_history_item(order)]
    assert broker.history[0]["quantity"] == -2.0

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")


@pytest.mark.parametrize(
    ("offset", "expected_status"),
    [
        (-timedelta(minutes=2), "filled"),
        (-timedelta(minutes=2, seconds=1), "submission_unknown"),
        (timedelta(minutes=15), "filled"),
        (timedelta(minutes=15, seconds=1), "submission_unknown"),
    ],
    ids=["earliest", "too-early", "latest", "too-late"],
)
@pytest.mark.asyncio
async def test_history_match_window_boundaries(db, reconcile_immediately, offset, expected_status):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    broker.history = [
        _history_item(order, createdAt=(attempt.dispatch_committed_at + offset).isoformat())
    ]

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == expected_status


@pytest.mark.asyncio
async def test_limit_price_mismatch_in_the_broker_record_is_not_a_match(db, reconcile_immediately):
    broker = TimeoutAfterTransmissionBroker()

    async def place_limit_order(ticker, quantity, limit_price, time_validity="DAY"):
        broker.posts.append({"ticker": ticker})
        raise httpx.ReadTimeout("response lost")

    broker.place_limit_order = place_limit_order
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("49.5"),
        is_dry_run=False,
    )
    order = await engine.submit_order(order)
    broker.history = [_history_item(order, type="LIMIT", limitPrice=51.0)]

    order = await engine.reconcile_unknown_submission(order)
    assert order.status == "submission_unknown"

    broker.history = [_history_item(order, type="LIMIT", limitPrice=49.5)]
    order = await engine.reconcile_unknown_submission(order)
    assert order.status == "filled"


@pytest.mark.asyncio
async def test_one_broker_record_that_fits_two_unknown_orders_resolves_neither(
    db, reconcile_immediately, monkeypatch
):
    broker = TimeoutAfterTransmissionBroker()
    engine, first = await _unknown_order(db, broker)
    # A second identical order from another process, also left unknown.
    second = Order(
        client_order_key="second-unknown-order",
        ticker=first.ticker,
        side=first.side,
        order_type=first.order_type,
        quantity=first.quantity,
        status="pending_intent",
        is_dry_run=False,
        venue="t212",
        execution_environment="demo",
    )
    db.add(second)
    await db.flush()
    transition_order_status(second, "submission_unknown")
    second.submitted_at = first.submitted_at
    db.add(
        OrderSubmissionAttempt(
            order_id=second.id,
            request_fingerprint="1" * 64,
            dispatch_committed_at=first.submitted_at,
        )
    )
    await db.commit()
    broker.history = [_history_item(first)]

    first = await engine.reconcile_unknown_submission(first)
    second = await engine.reconcile_unknown_submission(second)

    assert first.status == "submission_unknown"
    assert second.status == "submission_unknown"
    assert first.broker_order_id is None
    assert second.broker_order_id is None


@pytest.mark.asyncio
async def test_full_history_page_that_does_not_reach_the_window_is_unresolved(
    db, reconcile_immediately, monkeypatch
):
    monkeypatch.setattr(ExecutionEngine, "UNKNOWN_SUBMISSION_HISTORY_LIMIT", 2)
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    later = (order.submitted_at + timedelta(minutes=30)).isoformat()
    broker.history = [_history_item(order), _history_item(order, id="LATER", createdAt=later)]

    order = await engine.reconcile_unknown_submission(order)
    assert order.status == "submission_unknown"

    # Once the page reaches back before the window, the single match can be trusted.
    earlier = (order.submitted_at - timedelta(hours=1)).isoformat()
    broker.history = [_history_item(order), _history_item(order, id="OLD", createdAt=earlier)]
    order = await engine.reconcile_unknown_submission(order)
    assert order.status == "filled"


@pytest.mark.asyncio
async def test_cancelled_record_keeps_its_partial_fill(db, reconcile_immediately):
    broker = TimeoutAfterTransmissionBroker()
    engine, order = await _unknown_order(db, broker)
    broker.history = [
        _history_item(order, status="CANCELLED", filledQuantity=0.5, filledPrice=50.1)
    ]

    order = await engine.reconcile_unknown_submission(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("0.5")
    assert order.avg_fill_price == Decimal("50.1")


@pytest.mark.asyncio
async def test_reconciliation_raises_the_alarm_for_an_order_left_by_a_dead_process(
    db, reconcile_immediately
):
    engine = ExecutionEngine(db, TimeoutAfterTransmissionBroker())
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("2"), is_dry_run=False
    )
    # What a process killed straight after the dispatch commit leaves behind.
    transition_order_status(order, "submission_unknown")
    order.submitted_at = _dt.now(UTC) - timedelta(minutes=5)
    db.add(
        OrderSubmissionAttempt(
            order_id=order.id,
            request_fingerprint="2" * 64,
            dispatch_committed_at=order.submitted_at,
        )
    )
    await db.commit()

    await engine.reconcile_unknown_submission(order)
    await engine.reconcile_unknown_submission(order)

    events = (
        (await db.execute(select(RiskEvent).where(RiskEvent.event_type == "submission_unknown")))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].order_id == order.id
    alerts = (
        (await db.execute(select(Alert).where(Alert.alert_type == "submission_unknown")))
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_unknown_order_with_a_broker_id_becomes_accepted_when_the_broker_says_working(db):
    class WorkingAfterUnusableAck(UnusableAcknowledgementBroker):
        async def get_order_by_id(self, broker_order_id):
            return {"id": broker_order_id, "status": "WORKING"}

    broker = WorkingAfterUnusableAck(
        {"id": "B-9", "status": "FILLED", "filledQuantity": "not-a-number"}
    )
    engine, order = await _unknown_order(db, broker)
    assert order.status == "submission_unknown"
    assert order.broker_order_id == "B-9"

    order = await engine.reconcile_order(order)

    assert order.status == "accepted"
    assert order.error_message is None


@pytest.mark.asyncio
async def test_late_acknowledgement_does_not_overwrite_an_order_resolved_meanwhile(db):
    class ResolvedWhileInFlightBroker(WorkingBroker):
        async def place_market_order(self, ticker, quantity, time_validity="DAY"):
            # Reconciliation in another process settles the order before the answer arrives.
            persisted = (await db.execute(select(Order))).scalar_one()
            transition_order_status(persisted, "filled")
            persisted.broker_order_id = "FROM-HISTORY"
            persisted.filled_quantity = persisted.quantity
            await db.commit()
            return await super().place_market_order(ticker, quantity, time_validity)

    engine = ExecutionEngine(db, ResolvedWhileInFlightBroker())
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )
    order = await engine.submit_order(order)

    assert order.status == "filled"
    assert order.broker_order_id == "FROM-HISTORY"
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome == "accepted"
    event = (
        await db.execute(
            select(OrderEvent).where(OrderEvent.event_type == "late_submission_outcome")
        )
    ).scalar_one()
    assert event.from_status == event.to_status == "filled"


@pytest.mark.asyncio
async def test_alert_failure_leaves_the_unknown_order_durable_and_readable(db, monkeypatch):
    from app.services.alert_service import AlertService

    async def failing_send(self, **kwargs):
        raise RuntimeError("alert store unavailable")

    monkeypatch.setattr(AlertService, "send", failing_send)
    broker = TimeoutAfterTransmissionBroker()
    _, order = await _unknown_order(db, broker)

    assert order.status == "submission_unknown"
    assert order.ticker == "AAPL"
    risk_event = (
        await db.execute(select(RiskEvent).where(RiskEvent.event_type == "submission_unknown"))
    ).scalar_one()
    assert risk_event.order_id == order.id


@pytest.mark.asyncio
async def test_failure_while_recording_the_outcome_leaves_the_order_unknown(db, monkeypatch):
    broker = TimeoutAfterTransmissionBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )
    real_commit = db.commit
    commits = 0

    async def second_commit_fails():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("database unavailable")
        await real_commit()

    monkeypatch.setattr(db, "commit", second_commit_fails)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await engine.submit_order(order)
    monkeypatch.setattr(db, "commit", real_commit)
    await db.rollback()

    persisted = (await db.execute(select(Order))).scalar_one()
    assert persisted.status == "submission_unknown"
    assert len(broker.posts) == 1
    with pytest.raises(ValueError, match="submission_unknown"):
        await engine.submit_order(persisted)
    assert len(broker.posts) == 1


@pytest.mark.asyncio
async def test_cancelled_task_after_dispatch_leaves_the_order_unknown(db):
    import asyncio

    class CancelledMidFlightBroker(TimeoutAfterTransmissionBroker):
        async def place_market_order(self, ticker, quantity, time_validity="DAY"):
            self.posts.append({"ticker": ticker})
            raise asyncio.CancelledError

    broker = CancelledMidFlightBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )

    with pytest.raises(asyncio.CancelledError):
        await engine.submit_order(order)

    persisted = (await db.execute(select(Order))).scalar_one()
    assert persisted.status == "submission_unknown"
    attempt = (await db.execute(select(OrderSubmissionAttempt))).scalar_one()
    assert attempt.outcome is None


@pytest.mark.parametrize(
    ("order_type", "prices"),
    [
        ("limit", {}),
        ("stop", {}),
        ("stop_limit", {"limit_price": Decimal("10")}),
        ("stop_limit", {"stop_price": Decimal("10")}),
    ],
)
@pytest.mark.asyncio
async def test_order_missing_a_required_price_fails_before_dispatch(
    db, monkeypatch, order_type, prices
):
    monkeypatch.setattr(_eq, "_infer_terminal_time", lambda _o, _s: _dt.now(UTC))
    broker = TimeoutAfterTransmissionBroker()
    engine = ExecutionEngine(db, broker)
    order = await engine.create_order_intent(
        ticker="AAPL",
        side="buy",
        order_type=order_type,
        quantity=Decimal("1"),
        is_dry_run=False,
        **prices,
    )
    order = await engine.submit_order(order)

    assert order.status == "error"
    assert broker.posts == []
    assert (await db.execute(select(OrderSubmissionAttempt))).scalars().all() == []


@pytest.mark.asyncio
async def test_late_acknowledgement_naming_another_broker_order_raises_a_risk_event(db):
    class AdoptedThenAcknowledgedBroker(WorkingBroker):
        async def place_market_order(self, ticker, quantity, time_validity="DAY"):
            persisted = (await db.execute(select(Order))).scalar_one()
            transition_order_status(persisted, "filled")
            persisted.broker_order_id = "ADOPTED-FROM-HISTORY"
            await db.commit()
            return await super().place_market_order(ticker, quantity, time_validity)

    engine = ExecutionEngine(db, AdoptedThenAcknowledgedBroker())
    order = await engine.create_order_intent(
        ticker="AAPL", side="buy", order_type="market", quantity=Decimal("1"), is_dry_run=False
    )
    order = await engine.submit_order(order)

    assert order.status == "filled"
    assert order.broker_order_id == "ADOPTED-FROM-HISTORY"
    conflict = (
        await db.execute(
            select(RiskEvent).where(RiskEvent.event_type == "submission_identity_conflict")
        )
    ).scalar_one()
    assert conflict.payload["adopted_broker_order_id"] == "ADOPTED-FROM-HISTORY"
    assert conflict.payload["acknowledged_broker_order_id"] == "B-WRK"
