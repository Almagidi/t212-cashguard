"""N-20: a cancellation is recorded only from the broker's final record of the order.

The broker accepting a cancel request says neither that the order ended nor how much of it
filled first. After the request is accepted the order is read by its exact id, with the same
reader reconciliation uses: a final record is written as it is (a cancellation with its fill,
or the fill that beat the cancel); anything less leaves the order exactly as it was and the
caller is told the cancellation awaits confirmation.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.db.models import Order, OrderEvent
from app.execution import engine as engine_module
from app.execution.cash_reservation import cash_promised_elsewhere
from app.execution.engine import ExecutionEngine, OrderCancellationFailed
from app.execution.state_machine import ACTIVE_ORDER_STATUSES

SENT_AT = datetime.now(UTC) - timedelta(minutes=5)
NO_SUCH_ORDER = T212APIError(404, {"code": "OrderNotFound"})


def _history_record(order: Order, **fields: Any) -> dict[str, Any]:
    sign = -1 if order.side == "sell" else 1
    record: dict[str, Any] = {
        "id": order.broker_order_id,
        "ticker": order.ticker,
        "type": order.order_type.upper(),
        "quantity": float(order.quantity) * sign,
        "status": "CANCELLED",
        "filledQuantity": 0,
    }
    record.update(fields)
    return record


class ReadAfterCancelBroker:
    """Accepts cancel requests, and answers reads with whatever the test has set."""

    environment = "demo"

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.open_answer: Any = NO_SUCH_ORDER
        self.history: Any = {"items": [], "nextPagePath": None}
        self.cancel_refusal: Exception | None = None
        self.order_reads = 0
        self.history_reads = 0

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_calls.append(broker_order_id)
        if self.cancel_refusal is not None:
            raise self.cancel_refusal

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self.order_reads += 1
        if isinstance(self.open_answer, BaseException):
            raise self.open_answer
        return self.open_answer

    async def get_historical_orders(self, cursor=None, ticker=None, limit=50) -> Any:
        self.history_reads += 1
        if isinstance(self.history, BaseException):
            raise self.history
        return self.history


async def _working(
    db,
    *,
    side: str = "buy",
    quantity: str = "10",
    status: str = "accepted",
    filled: str | None = None,
    avg_price: str | None = None,
    is_dry_run: bool = False,
    broker_order_id: str | None = "B-N20",
) -> Order:
    order = Order(
        id=uuid.uuid4(),
        client_order_key=f"n20-{uuid.uuid4()}",
        ticker="AAPL",
        side=side,
        order_type="limit",
        quantity=Decimal(quantity),
        limit_price=Decimal("100"),
        status=status,
        broker_order_id=broker_order_id,
        execution_environment="demo",
        is_dry_run=is_dry_run,
        filled_quantity=Decimal(filled) if filled is not None else None,
        avg_fill_price=Decimal(avg_price) if avg_price is not None else None,
        cash_used=Decimal("1000") if side == "buy" else None,
        submitted_at=SENT_AT,
        created_at=SENT_AT,
    )
    db.add(order)
    await db.commit()
    return order


def _unconfirmed_class() -> type[Exception]:
    # Looked up when used, so that each test fails on its own on code without it.
    return engine_module.OrderCancellationUnconfirmed


async def _events(db, order: Order, event_type: str) -> list[OrderEvent]:
    rows = await db.execute(
        select(OrderEvent)
        .where(OrderEvent.order_id == order.id, OrderEvent.event_type == event_type)
        .order_by(OrderEvent.occurred_at)
    )
    return list(rows.scalars().all())


async def _expect_unconfirmed(engine: ExecutionEngine, order: Order) -> Any:
    with pytest.raises(OrderCancellationFailed) as raised:
        await engine.cancel_order(order)
    assert isinstance(raised.value, _unconfirmed_class())
    return raised.value


# ── what the final record says is what is recorded ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_cancellation_keeps_the_fill_the_broker_reports(db, side):
    order = await _working(db, side=side)
    broker = ReadAfterCancelBroker()
    sign = -1 if side == "sell" else 1
    broker.history = {
        "items": [_history_record(order, filledQuantity=4 * sign, filledPrice=101.25)],
        "nextPagePath": None,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("4")
    assert order.avg_fill_price == Decimal("101.25")
    assert order.cancelled_at is not None
    assert broker.cancel_calls == ["B-N20"]
    (settled,) = await _events(db, order, "reconciled_status")
    assert settled.payload["filled_quantity"] == "4"
    assert settled.payload["remaining_quantity"] == "6"


@pytest.mark.asyncio
async def test_cancellation_with_no_fill_is_recorded_from_the_record(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-N20", "status": "CANCELLED", "filledQuantity": 0}

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "cancelled"
    assert (order.filled_quantity or Decimal("0")) == Decimal("0")
    assert broker.history_reads == 0
    assert len(await _events(db, order, "cancel_request_accepted")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_an_order_that_filled_before_the_cancel_is_recorded_as_filled(db, side):
    order = await _working(db, side=side)
    broker = ReadAfterCancelBroker()
    sign = -1 if side == "sell" else 1
    broker.history = {
        "items": [
            _history_record(order, status="FILLED", filledQuantity=10 * sign, filledPrice=99.5)
        ],
        "nextPagePath": None,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "filled"
    assert order.filled_quantity == Decimal("10")
    assert order.avg_fill_price == Decimal("99.5")
    assert order.cancelled_at is None
    assert order.filled_at is not None
    assert await _events(db, order, "cancelled") == []


@pytest.mark.asyncio
async def test_a_rejection_reported_after_the_cancel_is_recorded_with_its_fill(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {
        "id": "B-N20",
        "status": "REJECTED",
        "filledQuantity": 2,
        "filledPrice": 100,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "rejected"
    assert order.filled_quantity == Decimal("2")


@pytest.mark.asyncio
async def test_a_fill_already_known_here_is_not_lost_to_a_record_that_states_less(db):
    order = await _working(db, status="partially_filled", filled="3", avg_price="100")
    broker = ReadAfterCancelBroker()
    broker.open_answer = {
        "id": "B-N20",
        "status": "CANCELLED",
        "filledQuantity": 1,
        "filledPrice": 100,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("3")


# ── nothing is recorded without a final record ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("open_answer", "history", "reason"),
    [
        pytest.param(
            NO_SUCH_ORDER, {"items": [], "nextPagePath": None}, "no_broker_record", id="no-record"
        ),
        pytest.param(
            {"id": "B-N20", "status": "WORKING", "filledQuantity": 4, "filledPrice": 100},
            None,
            "still_open_at_broker",
            id="still-working",
        ),
        pytest.param(
            {"id": "B-N20", "status": "CANCELLING", "filledQuantity": 0},
            None,
            "still_open_at_broker",
            id="cancel-in-progress",
        ),
        pytest.param(httpx.ReadTimeout("read lost"), None, "broker_read_failed", id="read-timeout"),
        pytest.param(T212APIError(503, {}), None, "broker_read_failed", id="read-5xx"),
        pytest.param(
            NO_SUCH_ORDER, T212RateLimitError(60), "rate_limited", id="history-rate-limited"
        ),
        pytest.param(T212RateLimitError(60), None, "rate_limited", id="read-rate-limited"),
        pytest.param(T212AuthError(401, "no"), None, "broker_auth_failed", id="read-refused-auth"),
        pytest.param(
            NO_SUCH_ORDER, httpx.ConnectError("down"), "broker_read_failed", id="history-down"
        ),
        pytest.param(NO_SUCH_ORDER, "not a page", "history_not_understood", id="history-malformed"),
    ],
)
async def test_without_a_final_record_the_order_is_left_as_it_was(db, open_answer, history, reason):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = open_answer
    if history is not None:
        broker.history = history
    engine = ExecutionEngine(db, broker)

    refusal = await _expect_unconfirmed(engine, order)

    assert refusal.reason == reason
    order = refusal.order
    assert order.status == "accepted"
    assert order.cancelled_at is None
    assert order.filled_quantity is None
    assert order.avg_fill_price is None
    (accepted,) = await _events(db, order, "cancel_request_accepted")
    assert accepted.payload["broker_order_id"] == "B-N20"
    assert accepted.payload["outcome"] == "awaiting_confirmation"
    assert accepted.payload["reason"] == reason
    assert await _events(db, order, "cancelled") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_fields", "reason"),
    [
        pytest.param({"ticker": "MSFT"}, "ticker_differs", id="another-instrument"),
        pytest.param({"id": "B-OTHER"}, "no_broker_record", id="another-id-is-no-record"),
        pytest.param({"type": "MARKET"}, "order_type_differs", id="another-type"),
        pytest.param({"quantity": 11}, "quantity_differs", id="another-quantity"),
        pytest.param(
            {"filledQuantity": 11, "filledPrice": 100}, "filled_quantity_exceeds_order", id="more"
        ),
        pytest.param({"filledQuantity": 4}, "fill_price_missing", id="fill-without-price"),
        pytest.param(
            {"filledQuantity": 4, "filledPrice": "NaN"}, "fill_price_not_a_number", id="nan-price"
        ),
        pytest.param(
            {"filledQuantity": 4, "filledPrice": 0}, "fill_price_not_positive", id="zero-price"
        ),
        pytest.param(
            {"filledQuantity": 4, "filledPrice": -1}, "fill_price_not_positive", id="neg-price"
        ),
        pytest.param(
            {"filledQuantity": "Infinity", "filledPrice": 1},
            "filled_quantity_not_a_number",
            id="infinite-fill",
        ),
        pytest.param(
            {"filledQuantity": -4, "filledPrice": 100},
            "filled_quantity_sign_contradicts_side",
            id="negative-fill-on-a-buy",
        ),
        pytest.param({"status": "WORKING"}, "history_record_not_final", id="history-not-final"),
        pytest.param({"filledQuantity": None}, "filled_quantity_missing", id="fill-unstated"),
    ],
)
async def test_a_record_that_cannot_be_trusted_settles_nothing(db, record_fields, reason):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    record = _history_record(order, **record_fields)
    if "filledQuantity" in record_fields and record_fields["filledQuantity"] is None:
        record.pop("filledQuantity")
    broker.history = {"items": [record], "nextPagePath": None}

    refusal = await _expect_unconfirmed(ExecutionEngine(db, broker), order)

    assert refusal.reason == reason
    assert refusal.order.status == "accepted"
    assert refusal.order.filled_quantity is None


@pytest.mark.asyncio
async def test_two_history_records_that_disagree_settle_nothing(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.history = {
        "items": [
            _history_record(order, filledQuantity=4, filledPrice=100),
            _history_record(order, status="FILLED", filledQuantity=10, filledPrice=100),
        ],
        "nextPagePath": None,
    }

    refusal = await _expect_unconfirmed(ExecutionEngine(db, broker), order)

    assert refusal.reason == "records_disagree_on_status"
    assert refusal.order.status == "accepted"


@pytest.mark.asyncio
async def test_an_open_answer_and_the_history_that_disagree_settle_nothing(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-N20", "status": "CANCELLED", "filledQuantity": 4}
    broker.history = {
        "items": [_history_record(order, filledQuantity=5, filledPrice=100)],
        "nextPagePath": None,
    }

    refusal = await _expect_unconfirmed(ExecutionEngine(db, broker), order)

    assert refusal.reason == "reads_disagree"
    assert refusal.order.filled_quantity is None


@pytest.mark.asyncio
async def test_an_unconfirmed_buy_keeps_its_whole_cash_reservation(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()

    await _expect_unconfirmed(ExecutionEngine(db, broker), order)
    await db.commit()

    assert order.status in ACTIVE_ORDER_STATUSES
    promised = await cash_promised_elsewhere(
        db,
        environment="demo",
        venue=order.venue,
        account_scope=None,
        cash_read_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert promised == Decimal("1000")


# ── a repeated cancellation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_repeated_cancel_reads_instead_of_asking_again(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, order)
    await db.commit()

    # The record appears later: the second call settles from it without a second request.
    broker.history = {
        "items": [_history_record(order, filledQuantity=6, filledPrice=100.5)],
        "nextPagePath": None,
    }
    order = await engine.cancel_order(order)

    assert broker.cancel_calls == ["B-N20"]
    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("6")
    assert len(await _events(db, order, "cancel_request_accepted")) == 1


@pytest.mark.asyncio
async def test_a_repeated_cancel_with_still_no_record_writes_nothing_more(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, order)
    await db.commit()

    refusal = await _expect_unconfirmed(engine, order)
    await db.commit()

    assert refusal.reason == "no_broker_record"
    assert broker.cancel_calls == ["B-N20"]
    total = await db.execute(
        select(func.count(OrderEvent.id)).where(OrderEvent.order_id == order.id)
    )
    assert total.scalar_one() == 1


@pytest.mark.asyncio
async def test_a_repeated_cancel_asks_again_only_while_the_broker_lists_the_order_open(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-N20", "status": "WORKING", "filledQuantity": 0}
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, order)
    await db.commit()

    await _expect_unconfirmed(engine, order)

    assert broker.cancel_calls == ["B-N20", "B-N20"]
    assert len(await _events(db, order, "cancel_request_accepted")) == 2


@pytest.mark.asyncio
async def test_a_refused_cancel_request_still_changes_nothing(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.cancel_refusal = T212APIError(400, {"code": "OrderCannotBeCancelled"})

    with pytest.raises(OrderCancellationFailed) as raised:
        await ExecutionEngine(db, broker).cancel_order(order)

    assert type(raised.value) is OrderCancellationFailed
    assert raised.value.order.status == "accepted"
    assert broker.order_reads == 0
    assert len(await _events(db, order, "cancel_failed")) == 1
    assert await _events(db, order, "cancel_request_accepted") == []


# ── an order whose submission outcome is unknown ─────────────────────────────


@pytest.mark.asyncio
async def test_cancelling_an_unknown_submission_keeps_the_fill_the_record_shows(db):
    order = await _working(db, status="submission_unknown")
    broker = ReadAfterCancelBroker()
    broker.history = {
        "items": [_history_record(order, filledQuantity=2.5, filledPrice=98)],
        "nextPagePath": None,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("2.5")
    assert order.avg_fill_price == Decimal("98")


@pytest.mark.asyncio
async def test_cancelling_an_unknown_submission_without_a_record_keeps_it_unknown(db):
    order = await _working(db, status="submission_unknown")
    broker = ReadAfterCancelBroker()

    refusal = await _expect_unconfirmed(ExecutionEngine(db, broker), order)

    assert refusal.order.status == "submission_unknown"
    assert refusal.order.cancelled_at is None
    assert len(await _events(db, order, "cancel_request_accepted")) == 1


@pytest.mark.asyncio
async def test_a_repeated_cancel_of_an_unknown_submission_reads_instead_of_asking_again(db):
    order = await _working(db, status="submission_unknown")
    broker = ReadAfterCancelBroker()
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, order)
    await db.commit()

    await _expect_unconfirmed(engine, order)

    assert broker.cancel_calls == ["B-N20"]


@pytest.mark.asyncio
async def test_cancelling_an_unknown_submission_that_filled_records_the_fill(db):
    order = await _working(db, status="submission_unknown", side="sell")
    broker = ReadAfterCancelBroker()
    broker.history = {
        "items": [_history_record(order, status="FILLED", filledQuantity=-10, filledPrice=97)],
        "nextPagePath": None,
    }

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "filled"
    assert order.filled_quantity == Decimal("10")


# ── orders that never reached a broker are unchanged ─────────────────────────


@pytest.mark.asyncio
async def test_a_paper_order_is_cancelled_locally_without_a_broker(db):
    order = await _working(db, is_dry_run=True, broker_order_id=None)

    order = await ExecutionEngine(db, None).cancel_order(order)

    assert order.status == "cancelled"


@pytest.mark.asyncio
async def test_an_unsent_intent_is_cancelled_locally(db):
    order = await _working(db, status="pending_intent", broker_order_id=None)
    broker = ReadAfterCancelBroker()

    order = await ExecutionEngine(db, broker).cancel_order(order)

    assert order.status == "cancelled"
    assert broker.cancel_calls == []
    assert broker.order_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listed",
    [
        pytest.param({"status": "CANCELLING"}, id="cancelling"),
        pytest.param({"status": "REPLACING"}, id="a-status-not-known-here"),
        pytest.param({"status": ""}, id="blank-status"),
        pytest.param({}, id="no-status"),
    ],
)
async def test_a_repeated_cancel_is_not_sent_while_the_order_is_not_plainly_working(db, listed):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-N20", "filledQuantity": 0, **listed}
    engine = ExecutionEngine(db, broker)
    first = await _expect_unconfirmed(engine, order)
    await db.commit()

    second = await _expect_unconfirmed(engine, order)

    assert broker.cancel_calls == ["B-N20"], "a cancellation under way was asked for again"
    assert first.reason == second.reason == "still_open_at_broker"
    assert second.broker_status == listed.get("status", "")
    (accepted,) = await _events(db, order, "cancel_request_accepted")
    assert accepted.payload["broker_status"] == listed.get("status", "")


# ── a batch: every request first, then the reads ────────────────────────────


class SharedRateLimitBroker(ReadAfterCancelBroker):
    """Like the real adapter: a 429 sets one pause that holds every later request, a cancel
    request included. A request that would have been held is recorded, not slept on."""

    def __init__(self, *, limit_first_read: bool = False, read_delay: float = 0.0) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []
        self.held: list[tuple[str, str]] = []
        self.paused_until = 0.0
        self.limit_first_read = limit_first_read
        self.read_delay = read_delay

    def _enter(self, call: tuple[str, str]) -> None:
        if asyncio.get_running_loop().time() < self.paused_until:
            self.held.append(call)
        self.calls.append(call)

    async def cancel_order(self, broker_order_id: str) -> None:
        self._enter(("cancel", broker_order_id))

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self._enter(("read", broker_order_id))
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        if self.limit_first_read and self.order_reads == 0:
            self.order_reads += 1
            self.paused_until = asyncio.get_running_loop().time() + 60
            raise T212RateLimitError(60)
        self.order_reads += 1
        return {"id": broker_order_id, "status": "CANCELLED", "filledQuantity": 0}

    async def get_historical_orders(self, cursor=None, ticker=None, limit=50) -> Any:
        self._enter(("history", str(ticker)))
        return await super().get_historical_orders(cursor=cursor, ticker=ticker, limit=limit)


async def _three_working(db) -> list[Order]:
    return [await _working(db, broker_order_id=f"B-{name}") for name in ("A", "B", "C")]


@pytest.mark.asyncio
async def test_a_batch_sends_every_cancel_before_reading_any_order(db):
    orders = await _three_working(db)
    broker = SharedRateLimitBroker(limit_first_read=True)

    batch = await ExecutionEngine(db, broker).cancel_many(orders)

    kinds = [kind for kind, _ in broker.calls]
    assert kinds[:3] == ["cancel", "cancel", "cancel"], broker.calls
    assert broker.held == [], "a cancel request was held behind a rate-limited read"
    # The first read was rate limited: no further read is made (it would be held).
    assert kinds[3:] == ["read"]
    assert [reason for _, reason in batch.unconfirmed] == [
        "rate_limited",
        "not_read_after_a_failed_read",
        "not_read_after_a_failed_read",
    ]
    assert batch.cancelled == []
    assert all(order.status == "accepted" for order in orders)


@pytest.mark.asyncio
async def test_a_batch_settles_each_order_from_its_record(db):
    orders = await _three_working(db)
    broker = SharedRateLimitBroker()

    batch = await ExecutionEngine(db, broker).cancel_many(orders)

    assert [order.broker_order_id for order in batch.cancelled] == ["B-A", "B-B", "B-C"]
    assert batch.unconfirmed == batch.failed == batch.finished == []


@pytest.mark.asyncio
async def test_a_batch_stops_reading_when_its_time_is_spent(db):
    orders = await _three_working(db)
    broker = SharedRateLimitBroker(read_delay=0.5)
    started = asyncio.get_running_loop().time()

    batch = await ExecutionEngine(db, broker).cancel_many(orders, read_seconds=0.05)

    assert asyncio.get_running_loop().time() - started < 0.4
    assert [reason for _, reason in batch.unconfirmed] == [
        "read_time_spent",
        "not_read_after_a_failed_read",
        "not_read_after_a_failed_read",
    ]
    assert all(order.status == "accepted" for order in orders)


class PerOrderBroker(ReadAfterCancelBroker):
    """Answers the read of each order with what the test has set for that order."""

    def __init__(self, answers: dict[str, Any]) -> None:
        super().__init__()
        self.answers = answers

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self.order_reads += 1
        answer = self.answers[broker_order_id]
        if isinstance(answer, BaseException):
            raise answer
        return {"id": broker_order_id, **answer}


@pytest.mark.asyncio
async def test_a_batch_asks_once_more_about_an_order_still_working_after_an_earlier_request(db):
    order = await _working(db)
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-N20", "status": "WORKING", "filledQuantity": 0}
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, order)
    await db.commit()

    batch = await engine.cancel_many([order])
    await db.commit()

    assert broker.cancel_calls == ["B-N20", "B-N20"]
    assert broker.order_reads == 2, "the order was read again after being asked again"
    assert [reason for _, reason in batch.unconfirmed] == ["asked_again_still_working"]
    assert order.status == "accepted"
    first, again = await _events(db, order, "cancel_request_accepted")
    assert first.payload["reason"] == "still_open_at_broker"
    assert (again.payload["reason"], again.payload["broker_status"]) == (
        "asked_again_still_working",
        "WORKING",
    )


@pytest.mark.asyncio
async def test_a_batch_does_not_ask_again_once_a_read_has_failed(db):
    earlier = await _working(db, broker_order_id="B-EARLIER")
    fresh = await _working(db, broker_order_id="B-FRESH")
    broker = PerOrderBroker(
        {
            "B-EARLIER": {"status": "WORKING", "filledQuantity": 0},
            "B-FRESH": T212RateLimitError(60),
        }
    )
    engine = ExecutionEngine(db, broker)
    await _expect_unconfirmed(engine, earlier)
    await db.commit()

    batch = await engine.cancel_many([earlier, fresh])

    # The fresh order's read was rate limited: the adapter now holds a pause, so nothing
    # more is sent; the working order is reported as it is.
    assert broker.cancel_calls == ["B-EARLIER", "B-FRESH"]
    assert {o.broker_order_id: r for o, r in batch.unconfirmed} == {
        "B-EARLIER": "still_open_at_broker",
        "B-FRESH": "rate_limited",
    }


@pytest.mark.asyncio
async def test_a_local_fault_recording_one_order_keeps_the_rest_of_the_batch(db, monkeypatch):
    first = await _working(db, broker_order_id="B-1")
    second = await _working(db, broker_order_id="B-2")
    broker = PerOrderBroker(
        {
            "B-1": {"status": "CANCELLED", "filledQuantity": 4, "filledPrice": 100},
            "B-2": {"status": "CANCELLED", "filledQuantity": 0},
        }
    )
    engine = ExecutionEngine(db, broker)
    real = engine._record_final_state

    async def fails_after_changing_the_first(order, record, found):
        await real(order, record, found)
        if order.broker_order_id == "B-1":
            # The order has been changed (cancelled, 4 filled) and then the save fails.
            await db.flush()
            raise RuntimeError("local fault")

    monkeypatch.setattr(engine, "_record_final_state", fails_after_changing_the_first)

    batch = await engine.cancel_many([first, second])

    # Read as a caller would, with no refresh of its own: nothing raises, and the faulted
    # order is as it is stored, not as the failed save left it.
    assert [(o.broker_order_id, o.ticker, o.status, r) for o, r in batch.unconfirmed] == [
        ("B-1", "AAPL", "accepted", "local_error")
    ]
    assert first.filled_quantity is None
    assert batch.cancelled == [second]
    await db.commit()
    # The request that went out is still on record.
    assert len(await _events(db, first, "cancel_request_accepted")) == 1


@pytest.mark.asyncio
async def test_a_refused_re_send_stops_the_other_re_sends(db):
    one = await _working(db, broker_order_id="B-1")
    two = await _working(db, broker_order_id="B-2")
    broker = PerOrderBroker(
        {
            "B-1": {"status": "WORKING", "filledQuantity": 0},
            "B-2": {"status": "PENDING", "filledQuantity": 0},
        }
    )
    engine = ExecutionEngine(db, broker)
    for order in (one, two):
        await _expect_unconfirmed(engine, order)
    await db.commit()
    broker.cancel_refusal = T212RateLimitError(60)

    batch = await engine.cancel_many([one, two])

    # The first re-send was refused (and may have set the adapter's pause): no second.
    assert broker.cancel_calls == ["B-1", "B-2", "B-1"]
    assert batch.failed == [one]
    assert [(o.broker_order_id, r) for o, r in batch.unconfirmed] == [
        ("B-2", "still_open_at_broker")
    ]


@pytest.mark.asyncio
async def test_a_batch_reports_a_refused_request_and_an_ended_order_and_goes_on(db):
    refused, ended, working = await _three_working(db)
    ended.status = "filled"
    ended.filled_quantity = Decimal("10")
    await db.commit()
    broker = ReadAfterCancelBroker()
    broker.open_answer = {"id": "B-C", "status": "CANCELLED", "filledQuantity": 0}

    async def refuse_the_first(broker_order_id: str) -> None:
        broker.cancel_calls.append(broker_order_id)
        if broker_order_id == "B-A":
            raise T212APIError(400, {"code": "OrderCannotBeCancelled"})

    broker.cancel_order = refuse_the_first  # type: ignore[method-assign]

    batch = await ExecutionEngine(db, broker).cancel_many([refused, ended, working])

    assert batch.failed == [refused]
    assert batch.finished == [ended]
    assert batch.cancelled == [working]
    assert broker.cancel_calls == ["B-A", "B-C"]
