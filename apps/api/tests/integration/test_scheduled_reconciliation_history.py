"""An order that has ended is no longer among the broker's open orders. The scheduled
reconciler then reads the order history for the same id, and settles the order only from a
record that is about it and complete."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.db.models import OrderEvent, RiskEvent
from tests.integration.scheduled_reconciliation_cases import (
    T0,
    Brokers,
    add_connection,
    add_order,
    install_borrowed_session,
    rewind_last_attempt,
    run_task,
    scope_of,
    stored,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.integration.scheduled_reconciliation_cases import FakeBroker

pytestmark = pytest.mark.asyncio

NOT_OPEN = T212APIError(404, "not found")
ENDED_AT = "2026-10-05T14:00:03Z"


@pytest.fixture
def brokers(monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> Brokers:
    registry = Brokers()
    install_borrowed_session(monkeypatch, registry, db)
    return registry


def record(
    broker_order_id: str,
    *,
    status: str = "FILLED",
    quantity: Any = 1,
    filled: Any = 1,
    price: Any = "10.5",
    at: str | None = ENDED_AT,
    **order: Any,
) -> dict[str, Any]:
    """One history record in the nested layout the broker's DEMO history uses."""
    item: dict[str, Any] = {
        "order": {
            "id": broker_order_id,
            "ticker": "AAPL_US_EQ",
            "status": status,
            "side": "BUY",
            "type": "LIMIT",
            "quantity": quantity,
            "filledQuantity": filled,
            **order,
        }
    }
    if price is not None:
        item["fill"] = {"quantity": filled, "price": price, "filledAt": at}
    return item


def _utc(value: datetime | None) -> datetime | None:
    """A stored time as UTC, whether or not the test database kept its zone."""
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _account(db: AsyncSession, brokers: Brokers) -> tuple[str | None, FakeBroker]:
    connection = await add_connection(db, account_id="ACC-1")
    return scope_of(connection), brokers.for_user(connection.user_id)


async def _events(db: AsyncSession, order_id: uuid.UUID, event_type: str) -> list[OrderEvent]:
    result = await db.execute(
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id, OrderEvent.event_type == event_type)
        .order_by(OrderEvent.occurred_at)
    )
    return list(result.scalars().all())


async def _raised(db: AsyncSession, order_id: uuid.UUID) -> list[RiskEvent]:
    result = await db.execute(
        select(RiskEvent).where(
            RiskEvent.order_id == order_id, RiskEvent.event_type == "order_not_settling"
        )
    )
    return list(result.scalars().all())


async def test_an_order_that_left_the_open_orders_is_settled_from_history_by_its_exact_id(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="ended")
    broker.responses["ended"] = NOT_OPEN
    # Records of other orders, one with an id that only begins the same way.
    broker.history = {
        "items": [
            record("ended-2", price="99"),
            record("ended", price="10.5"),
            record("other", price="77"),
        ]
    }

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "filled"
    assert saved.filled_quantity == Decimal("1")
    assert saved.avg_fill_price == Decimal("10.5")
    assert _utc(saved.filled_at) == datetime(2026, 10, 5, 14, 0, 3, tzinfo=UTC)
    assert saved.broker_response == record("ended", price="10.5")
    assert saved.retry_count == 0
    assert summary["reconciled"] == 1
    assert "failed" not in summary
    assert "no_broker_record" not in summary
    assert broker.order_reads == ["ended"]
    assert broker.history_reads == ["None"]
    (event,) = await _events(db, order.id, "reconciled_fill")
    assert event.payload["source"] == "order_history"
    assert Decimal(event.payload["filled_quantity"]) == Decimal("1")
    assert Decimal(event.payload["avg_fill_price"]) == Decimal("10.5")
    assert broker.write_calls == []


async def test_a_sell_the_broker_writes_with_negative_quantities_is_settled(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db, account_scope=scope, broker_order_id="sold", side="sell", quantity="3"
    )
    broker.responses["sold"] = NOT_OPEN
    broker.history = {"items": [record("sold", quantity=-3, filled=-3, price=12, side="SELL")]}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "filled"
    assert saved.filled_quantity == Decimal("3")
    assert saved.avg_fill_price == Decimal("12")


async def test_a_sell_answered_with_a_negative_amount_by_the_open_orders_read_is_settled(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db, account_scope=scope, broker_order_id="sold", side="sell", quantity="3"
    )
    broker.responses["sold"] = {"status": "FILLED", "filledQuantity": -3, "filledPrice": 12}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "filled"
    assert saved.filled_quantity == Decimal("3")
    assert broker.history_reads == []
    (event,) = await _events(db, order.id, "reconciled_fill")
    assert event.payload["source"] == "open_orders"


async def test_a_negative_amount_never_settles_a_buy(db: AsyncSession, brokers: Brokers) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="bought")
    broker.responses["bought"] = {"status": "FILLED", "filledQuantity": -1, "filledPrice": 12}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.filled_quantity in (None, Decimal("0"))
    assert summary["failed"] == 1
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["outcome"] == "broker_answer_refused"
    assert wait.payload["reason"] == "filled_quantity_sign_contradicts_side"
    # A record that contradicts the order is not looked past in the history.
    assert broker.history_reads == []


async def test_a_cancelled_order_keeps_the_part_that_filled(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="part", quantity="5")
    broker.responses["part"] = NOT_OPEN
    broker.history = {
        "items": [record("part", status="CANCELLED", quantity=5, filled=2, price="10.25")]
    }

    before = datetime.now(UTC)
    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "cancelled"
    assert saved.filled_quantity == Decimal("2")
    assert saved.avg_fill_price == Decimal("10.25")
    # The fill's time is not the cancellation's: the record gives none for that.
    cancelled_at = _utc(saved.cancelled_at)
    assert cancelled_at is not None
    assert before <= cancelled_at <= datetime.now(UTC)
    (event,) = await _events(db, order.id, "reconciled_status")
    assert Decimal(event.payload["filled_quantity"]) == Decimal("2")
    assert Decimal(event.payload["remaining_quantity"]) == Decimal("3")
    assert event.payload["source"] == "order_history"


async def test_a_cancellation_that_says_nothing_filled_is_settled_unfilled(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="none")
    broker.responses["none"] = NOT_OPEN
    broker.history = {
        "items": [{"order": {"id": "none", "status": "CANCELLED", "filledQuantity": 0}}]
    }

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "cancelled"
    assert saved.filled_quantity == Decimal("0")
    assert saved.avg_fill_price is None


async def test_a_cancellation_that_does_not_say_how_much_filled_settles_nothing(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="silent")
    broker.responses["silent"] = NOT_OPEN
    broker.history = {"items": [{"order": {"id": "silent", "status": "CANCELLED"}}]}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert summary["failed"] == 1
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["reason"] == "filled_quantity_missing"


@pytest.mark.parametrize("status", ["WORKING", "EXPIRED", None])
async def test_only_the_open_orders_read_can_say_an_order_is_still_open(
    db: AsyncSession, brokers: Brokers, status: str | None
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="odd")
    broker.responses["odd"] = NOT_OPEN
    item = record("odd")
    item["order"]["status"] = status
    broker.history = {"items": [item]}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.retry_count == 1, "the wait was cleared as if the order were still open"
    assert summary["failed"] == 1
    assert summary["reconciled"] == 0
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["reason"] == "history_record_not_final"


async def test_the_history_may_complete_the_answer_but_not_tell_another_story(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="told")
    broker.responses["told"] = {"status": "FILLED", "filledQuantity": 1}
    broker.history = {"items": [record("told", status="CANCELLED", filled=0, price=None)]}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert summary["failed"] == 1
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["reason"] == "reads_disagree"


async def test_an_answer_that_is_not_a_history_page_is_not_an_empty_history(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="ended")
    broker.responses["ended"] = NOT_OPEN
    broker.history = {"items": None}

    summary = await run_task()

    assert (await stored(db, order.id)).status == "accepted"
    assert "no_broker_record" not in summary
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["reason"] == "history_not_understood"


async def test_several_fill_records_settle_one_order_and_are_all_kept(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="split", quantity="5")
    broker.responses["split"] = NOT_OPEN
    first = record("split", quantity=5, filled=5, price="100")
    first["fill"]["quantity"] = 2
    second = record("split", quantity=5, filled=5, price="110")
    second["fill"]["quantity"] = 3
    broker.history = {"items": [first, record("other"), second]}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "filled"
    assert saved.avg_fill_price == Decimal("106")
    assert saved.broker_response == {"records": [first, second]}


@pytest.mark.parametrize(
    ("page", "instrument_asked"),
    [
        ({"items": [], "nextPagePath": "/api/v0/equity/history/orders?cursor=7"}, True),
        ({"items": [record(f"newer-{index}") for index in range(50)], "nextPagePath": None}, False),
    ],
)
async def test_the_broker_saying_whether_older_records_exist_is_believed(
    db: AsyncSession, brokers: Brokers, page: dict[str, Any], instrument_asked: bool
) -> None:
    scope, broker = await _account(db, brokers)
    await add_order(db, account_scope=scope, broker_order_id="older")
    broker.responses["older"] = NOT_OPEN
    broker.history = page

    await run_task()

    assert broker.history_reads == (["None", "AAPL_US_EQ"] if instrument_asked else ["None"])


async def test_a_fill_reported_without_its_price_is_completed_from_history(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="priced")
    broker.responses["priced"] = {"id": "priced", "status": "FILLED", "filledQuantity": 1}
    broker.history = {"items": [record("priced", price="10.75")]}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "filled"
    assert saved.avg_fill_price == Decimal("10.75")
    (event,) = await _events(db, order.id, "reconciled_fill")
    assert event.payload["source"] == "order_history"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"status": "FILLED", "filledQuantity": 1}, "fill_price_missing"),
        ({"status": "FILLED", "filledQuantity": 1, "filledPrice": None}, "fill_price_missing"),
        ({"status": "FILLED"}, "filled_quantity_missing"),
        ({"status": "CANCELLED"}, "filled_quantity_missing"),
    ],
)
async def test_a_final_answer_missing_a_number_settles_nothing_when_history_has_no_record(
    db: AsyncSession, brokers: Brokers, answer: dict[str, Any], reason: str
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="bare")
    broker.responses["bare"] = answer

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.avg_fill_price is None
    assert saved.filled_at is None
    assert saved.cancelled_at is None
    assert saved.retry_count == 1
    assert summary["failed"] == 1
    assert broker.history_reads == ["None"]
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["outcome"] == "broker_answer_refused"
    assert wait.payload["reason"] == reason


@pytest.mark.parametrize(
    "price", ["Infinity", "-Infinity", "NaN", "1e999999", "1000000000000", 0, -5, "ten"]
)
async def test_a_fill_price_that_is_not_a_real_price_is_never_stored(
    db: AsyncSession, brokers: Brokers, price: Any
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="bad-price")
    broker.responses["bad-price"] = {"status": "FILLED", "filledQuantity": 1, "filledPrice": price}
    # Even a good record in the history does not excuse an answer that is plainly wrong.
    broker.history = {"items": [record("bad-price")]}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.avg_fill_price is None
    assert broker.history_reads == []
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["outcome"] == "broker_answer_refused"
    assert wait.payload["reason"] in {
        "fill_price_not_a_number",
        "fill_price_not_positive",
        "fill_price_too_large",
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"ticker": "MSFT_US_EQ"}, "ticker_differs"),
        ({"side": "SELL"}, "side_differs"),
        ({"type": "MARKET"}, "order_type_differs"),
        ({"quantity": 2, "filledQuantity": 1}, "quantity_differs"),
    ],
)
async def test_a_history_record_that_describes_another_order_settles_nothing(
    db: AsyncSession, brokers: Brokers, changes: dict[str, Any], reason: str
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="mine")
    broker.responses["mine"] = NOT_OPEN
    broker.history = {"items": [record("mine", **changes)]}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.avg_fill_price is None
    assert saved.broker_response is None
    assert summary["failed"] == 1
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["reason"] == reason


async def test_one_history_page_serves_every_order_of_a_run(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    orders = [
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=f"done-{index}",
            created_at=T0 + timedelta(seconds=index),
            ticker=f"T{index}_US_EQ",
        )
        for index in range(5)
    ]
    for index in range(5):
        broker.responses[f"done-{index}"] = NOT_OPEN
    broker.history = {
        "items": [record(f"done-{index}", ticker=f"T{index}_US_EQ") for index in range(5)]
    }

    summary = await run_task()

    assert [(await stored(db, order.id)).status for order in orders] == ["filled"] * 5
    assert summary["reconciled"] == 5
    assert broker.history_reads == ["None"]


async def test_a_full_newest_page_sends_the_search_to_the_instruments_own_history(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="older")
    broker.responses["older"] = NOT_OPEN
    asked: list[dict[str, Any]] = []

    def pages(**query: Any) -> dict[str, Any]:
        asked.append(query)
        if query.get("ticker") == "AAPL_US_EQ":
            return {"items": [record("older")]}
        return {"items": [record(f"newer-{index}") for index in range(50)]}

    broker.history = pages

    await run_task()

    assert (await stored(db, order.id)).status == "filled"
    assert asked == [{"limit": 50}, {"ticker": "AAPL_US_EQ", "limit": 50}]


async def test_a_newest_page_that_is_not_full_is_the_whole_history(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="nowhere")
    broker.responses["nowhere"] = NOT_OPEN
    broker.history = {"items": [record(f"newer-{index}") for index in range(49)]}

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.retry_count == 1
    assert broker.history_reads == ["None"]
    assert summary["no_broker_record"] == 1
    assert "failed" not in summary
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["outcome"] == "no_broker_record"
    assert wait.payload["reason"] == "no_broker_record"
    assert wait.payload["wait_seconds"] == 30


async def test_a_history_read_that_fails_is_a_failed_read_and_changes_nothing(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(db, account_scope=scope, broker_order_id="ended")
    broker.responses["ended"] = NOT_OPEN
    broker.history = T212RateLimitError(7)

    summary = await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert summary["rate_limited"] == 1
    assert summary["stopped"] == {scope: "rate_limited"}
    (wait,) = await _events(db, order.id, "reconciliation_backoff")
    assert wait.payload["outcome"] == "rate_limited"


async def test_an_order_with_no_record_does_not_count_towards_an_unreachable_broker(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    names = ["slow-1", "slow-2", "gone", "slow-3", "working"]
    for index, name in enumerate(names):
        await add_order(
            db,
            account_scope=scope,
            broker_order_id=name,
            created_at=T0 + timedelta(seconds=index),
        )
    for name in ("slow-1", "slow-2", "slow-3"):
        broker.responses[name] = TimeoutError()
    broker.responses["gone"] = NOT_OPEN

    summary = await run_task()

    # Three reads in a row that the broker did not answer would end the run for this
    # account. The answer about "gone" came in between, so the count started again.
    assert broker.order_reads == names
    assert "stopped" not in summary
    assert summary["reconciled"] == 1
    assert summary["no_broker_record"] == 1
    assert summary["failed"] == 3


async def test_a_time_the_broker_could_not_have_ended_the_order_at_is_not_kept(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    early = await add_order(db, account_scope=scope, broker_order_id="early")
    late = await add_order(
        db, account_scope=scope, broker_order_id="late", created_at=T0 + timedelta(seconds=1)
    )
    broker.responses["early"] = broker.responses["late"] = NOT_OPEN
    broker.history = {
        "items": [
            record("early", at="2026-10-05T13:59:59Z"),
            record("late", at="2999-01-01T00:00:00Z"),
        ]
    }
    before = datetime.now(UTC)

    await run_task()

    for order in (early, late):
        saved = await stored(db, order.id)
        assert saved.status == "filled"
        filled_at = _utc(saved.filled_at)
        assert filled_at is not None
        assert before <= filled_at <= datetime.now(UTC)


async def test_an_order_that_will_not_settle_is_raised_once_when_its_wait_is_the_longest(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="lost",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["lost"] = NOT_OPEN

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    assert saved.retry_count == 6
    (raised,) = await _raised(db, order.id)
    assert raised.ticker == "AAPL_US_EQ"
    assert raised.payload == {
        "severity": "critical",
        "outcome": "no_broker_record",
        "reason": "no_broker_record",
        "error_type": None,
        "attempts_in_a_row": 6,
        "broker_order_id": "lost",
        "status": "accepted",
        "side": "buy",
        "quantity": str(saved.quantity),
    }

    # It goes on being asked about, and is not raised again on every attempt.
    await rewind_last_attempt(db, order.id, 3600)
    await run_task()

    assert broker.order_reads == ["lost", "lost"]
    assert len(await _raised(db, order.id)) == 1

    # A day later it is still not settled: it is raised again.
    raised.occurred_at = datetime.now(UTC) - timedelta(hours=25)
    await db.commit()
    await rewind_last_attempt(db, order.id, 3600)
    await run_task()

    assert len(await _raised(db, order.id)) == 2


@pytest.mark.parametrize("retry_count", [6, 9])
async def test_an_order_already_at_the_longest_wait_is_raised_once_it_does_not_settle(
    db: AsyncSession, brokers: Brokers, retry_count: int
) -> None:
    # An outage may have taken the order to the longest wait first; what matters is
    # that it still does not settle once the broker answers.
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="lost",
        retry_count=retry_count,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["lost"] = NOT_OPEN

    await run_task()

    (raised,) = await _raised(db, order.id)
    assert raised.payload["outcome"] == "no_broker_record"


async def test_an_order_whose_settlement_cannot_be_saved_is_raised(
    db: AsyncSession, brokers: Brokers, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="unsaved",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["unsaved"] = {"status": "FILLED", "filledQuantity": 1, "filledPrice": 10}
    real_commit = db.commit
    commits: list[str] = []

    async def first_commit_fails() -> None:
        if not commits:
            commits.append("failed")
            raise RuntimeError("database refused the row")
        commits.append("saved")
        await real_commit()

    monkeypatch.setattr(db, "commit", first_commit_fails)

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "accepted"
    (raised,) = await _raised(db, order.id)
    assert raised.payload["outcome"] == "local_error"
    assert raised.payload["error_type"] == "RuntimeError"


async def test_an_order_is_not_raised_before_its_wait_is_the_longest(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="lost",
        retry_count=4,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["lost"] = NOT_OPEN

    await run_task()

    assert (await stored(db, order.id)).retry_count == 5
    assert await _raised(db, order.id) == []


async def test_an_unknown_submission_that_never_matches_is_raised_at_the_longest_wait(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        status="submission_unknown",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.history = {"items": []}

    await run_task()

    saved = await stored(db, order.id)
    assert saved.status == "submission_unknown"
    assert saved.broker_order_id is None
    (raised,) = await _raised(db, order.id)
    assert raised.payload["outcome"] == "unresolved"


@pytest.mark.parametrize(
    "failure", [TimeoutError(), T212RateLimitError(5), T212AuthError(401, "no")]
)
async def test_a_broker_that_cannot_be_reached_does_not_raise_its_orders_one_by_one(
    db: AsyncSession, brokers: Brokers, failure: Exception
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="unread",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["unread"] = failure

    await run_task()

    assert (await stored(db, order.id)).retry_count == 6
    assert await _raised(db, order.id) == []


async def test_a_read_the_broker_keeps_refusing_for_one_order_is_raised(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="refused",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["refused"] = T212APIError(400, "bad request")

    await run_task()

    (raised,) = await _raised(db, order.id)
    assert raised.payload["outcome"] == "broker_rejected_read"
    assert raised.payload["error_type"] == "T212APIError"
    assert broker.history_reads == []


async def test_a_refused_record_is_raised_with_its_reason_when_the_wait_is_the_longest(
    db: AsyncSession, brokers: Brokers
) -> None:
    scope, broker = await _account(db, brokers)
    order = await add_order(
        db,
        account_scope=scope,
        broker_order_id="mine",
        retry_count=5,
        last_reconciled_at=datetime.now(UTC) - timedelta(hours=1),
    )
    broker.responses["mine"] = NOT_OPEN
    broker.history = {"items": [record("mine", ticker="MSFT_US_EQ")]}

    await run_task()

    (raised,) = await _raised(db, order.id)
    assert raised.payload["outcome"] == "broker_answer_refused"
    assert raised.payload["reason"] == "ticker_differs"
    assert (await stored(db, order.id)).status == "accepted"
