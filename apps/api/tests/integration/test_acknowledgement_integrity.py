"""PostgreSQL proof that a broker's immediate answer to an order request is never taken on
trust.

Every test goes through the real ``create_order_intent`` and ``submit_order``. What was
saved is read back through a separate database session, so only committed state counts.
The broker is an in-memory double; nothing leaves this machine.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.models import (
    Alert,
    AppSettings,
    AuditLog,
    Order,
    OrderEvent,
    OrderSubmissionAttempt,
    RiskEvent,
)
from app.execution.engine import ExecutionEngine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the acknowledgement proof",
    ),
    pytest.mark.asyncio,
]

MISSING = object()
QUANTITY = Decimal("2")


class AnsweringBroker:
    """Answers one order request with a scripted acknowledgement, and by-id reads from a
    second script. Counts every request so a second send cannot go unnoticed."""

    environment = "demo"
    account_scope = "trading212:demo:account:acknowledgement-test"

    def __init__(self, acknowledgement: Any) -> None:
        self.acknowledgement = acknowledgement
        self.posts: list[tuple[str, Decimal]] = []
        self.by_id: dict[str, Any] = {}
        self.reads: list[str] = []
        self.before_answering: Any = None

    async def place_market_order(self, ticker: str, quantity: Decimal, time_validity: str = "DAY"):
        self.posts.append((ticker, quantity))
        if self.before_answering is not None:
            await self.before_answering()
        if isinstance(self.acknowledgement, BaseException):
            raise self.acknowledgement
        return self.acknowledgement

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self.reads.append(broker_order_id)
        answer = self.by_id[broker_order_id]
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    async def get_historical_orders(self, **_kwargs: Any) -> Any:
        return {"items": [], "nextPagePath": None}


def filled_ack(quantity: Any = 2, price: Any = 50.25, order_id: Any = "BRK-1") -> dict[str, Any]:
    answer: dict[str, Any] = {"status": "FILLED"}
    for key, value in (("id", order_id), ("filledQuantity", quantity), ("filledPrice", price)):
        if value is not MISSING:
            answer[key] = value
    return answer


class Saved:
    """What a fresh session finds for one test's ticker."""

    def __init__(self, sessions: Any, ticker: str) -> None:
        self.sessions = sessions
        self.ticker = ticker

    async def order(self) -> Order:
        async with self.sessions() as session:
            return (
                await session.execute(select(Order).where(Order.ticker == self.ticker))
            ).scalar_one()

    async def attempt(self) -> OrderSubmissionAttempt:
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(OrderSubmissionAttempt)
                    .join(Order, Order.id == OrderSubmissionAttempt.order_id)
                    .where(Order.ticker == self.ticker)
                )
            ).scalar_one()

    async def events(self, event_type: str) -> list[OrderEvent]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(OrderEvent)
                        .join(Order, Order.id == OrderEvent.order_id)
                        .where(Order.ticker == self.ticker, OrderEvent.event_type == event_type)
                    )
                )
                .scalars()
                .all()
            )

    async def alerts(self) -> list[Alert]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(Alert).where(Alert.title == f"Order outcome unknown: {self.ticker}")
                    )
                )
                .scalars()
                .all()
            )

    async def audit_rows(self) -> list[AuditLog]:
        order = await self.order()
        async with self.sessions() as session:
            return list(
                (await session.execute(select(AuditLog).where(AuditLog.entity_id == str(order.id))))
                .scalars()
                .all()
            )

    async def risk_events(self, event_type: str) -> list[RiskEvent]:
        async with self.sessions() as session:
            return list(
                (
                    await session.execute(
                        select(RiskEvent).where(
                            RiskEvent.ticker == self.ticker, RiskEvent.event_type == event_type
                        )
                    )
                )
                .scalars()
                .all()
            )


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Saved, None]:
    assert POSTGRES_TEST_DATABASE_URL is not None
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ticker = f"ACK{uuid.uuid4().hex[:8].upper()}"

    async with sessions() as setup:
        app_settings = await setup.get(AppSettings, 1)
        created_settings = app_settings is None
        if app_settings is None:
            app_settings = AppSettings(id=1)
            setup.add(app_settings)
        previous = (app_settings.auto_trading_enabled, app_settings.kill_switch_active)
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
        await setup.commit()

    try:
        yield Saved(sessions, ticker)
    finally:
        async with sessions() as cleanup:
            order_ids = (
                (await cleanup.execute(select(Order.id).where(Order.ticker == ticker)))
                .scalars()
                .all()
            )
            if order_ids:
                text_ids = [str(order_id) for order_id in order_ids]
                await cleanup.execute(
                    delete(OrderSubmissionAttempt).where(
                        OrderSubmissionAttempt.order_id.in_(order_ids)
                    )
                )
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(RiskEvent).where(RiskEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id.in_(text_ids)))
                await cleanup.execute(delete(Order).where(Order.id.in_(order_ids)))
            await cleanup.execute(
                delete(Alert).where(Alert.title == f"Order outcome unknown: {ticker}")
            )
            if created_settings:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            else:
                restored = await cleanup.get(AppSettings, 1)
                restored.auto_trading_enabled, restored.kill_switch_active = previous
            await cleanup.commit()
        await engine.dispose()


async def _submit(world: Saved, broker: AnsweringBroker, *, side: str = "buy") -> None:
    """One process: create the intent, send it, and end without any further commit."""
    async with world.sessions() as session:
        engine = ExecutionEngine(session, broker)
        order = await engine.create_order_intent(
            ticker=world.ticker,
            side=side,
            order_type="market",
            quantity=QUANTITY,
            is_dry_run=False,
            estimated_price=Decimal("50"),
        )
        await engine.submit_order(order)


async def _assert_kept_unresolved(
    world: Saved, broker: AnsweringBroker, *, broker_order_id: str | None
) -> OrderSubmissionAttempt:
    order = await world.order()
    assert order.status == "submission_unknown", "an untrusted answer finalised the order"
    assert order.broker_order_id == broker_order_id
    assert order.filled_quantity is None
    assert order.avg_fill_price is None
    assert order.filled_at is None
    assert order.cancelled_at is None
    assert order.rejected_at is None
    assert order.broker_response is None, "an untrusted answer was stored as the broker's word"
    attempt = await world.attempt()
    assert attempt.outcome == "ambiguous"
    assert attempt.dispatch_committed_at is not None
    assert len(await world.risk_events("submission_unknown")) == 1
    assert len(await world.alerts()) == 1, "nobody was told about the unresolved order"
    unknown_audits = [
        row.payload
        for row in await world.audit_rows()
        if (row.payload or {}).get("decision") == "unknown"
    ]
    assert len(unknown_audits) == 1
    assert unknown_audits[0]["no_broker_order_sent"] is False, "an order may exist at the broker"
    assert len(await world.events("dispatch_committed")) == 1
    assert len(await world.events("submission_outcome_unknown")) == 1
    assert len(broker.posts) == 1
    return attempt


# --- the reported defect: quantity 2 answered FILLED with 3, -1 or 1 ----------------------


@pytest.mark.parametrize(
    ("reported", "reason"),
    [
        (3, "filled_quantity_exceeds_order"),
        (-1, "filled_quantity_sign_contradicts_side"),
        (1, "filled_quantity_below_order"),
    ],
)
async def test_a_filled_answer_with_the_wrong_quantity_does_not_finalise_the_order(
    world: Saved, reported: int, reason: str
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=reported))

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    assert attempt.outcome_evidence["reason"] == reason
    assert attempt.outcome_evidence["broker_order_id"] == "BRK-1"


async def test_a_complete_and_consistent_filled_answer_is_recorded_as_a_fill(world: Saved) -> None:
    broker = AnsweringBroker(filled_ack(quantity=2, price=50.25))

    await _submit(world, broker)

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")
    assert order.avg_fill_price == Decimal("50.25")
    assert order.broker_order_id == "BRK-1"
    assert order.remaining_quantity == Decimal("0")
    assert (await world.attempt()).outcome == "accepted"
    assert await world.risk_events("submission_unknown") == []
    assert len(broker.posts) == 1


# --- every other way a FILLED answer can fail to describe this order's fill ----------------


@pytest.mark.parametrize(
    ("reported", "reason"),
    [
        (0, "filled_without_a_fill"),
        (0.0, "filled_without_a_fill"),
        (MISSING, "filled_quantity_missing"),
        (None, "filled_quantity_missing"),
        ("", "filled_quantity_not_a_number"),
        ("two", "filled_quantity_not_a_number"),
        (float("nan"), "filled_quantity_not_a_number"),
        (float("inf"), "filled_quantity_not_a_number"),
        (float("-inf"), "filled_quantity_not_a_number"),
        ("NaN", "filled_quantity_not_a_number"),
        ("Infinity", "filled_quantity_not_a_number"),
        (True, "filled_quantity_not_a_number"),
        ([2], "filled_quantity_not_a_number"),
        ({"value": 2}, "filled_quantity_not_a_number"),
        (2.0000001, "filled_quantity_exceeds_order"),
        (1.9999999, "filled_quantity_below_order"),
        (-2, "filled_quantity_sign_contradicts_side"),
        (10**30, "filled_quantity_exceeds_order"),
    ],
)
async def test_no_malformed_filled_quantity_finalises_a_buy(
    world: Saved, reported: Any, reason: str
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=reported))

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    assert attempt.outcome_evidence["reason"] == reason


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        (MISSING, "fill_price_missing"),
        (None, "fill_price_missing"),
        (0, "fill_price_not_positive"),
        (-50.25, "fill_price_not_positive"),
        ("", "fill_price_not_a_number"),
        ("market", "fill_price_not_a_number"),
        (float("nan"), "fill_price_not_a_number"),
        (float("inf"), "fill_price_not_a_number"),
        (False, "fill_price_not_a_number"),
    ],
)
async def test_a_fill_is_not_recorded_with_a_made_up_price(
    world: Saved, price: Any, reason: str
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=2, price=price))

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    assert attempt.outcome_evidence["reason"] == reason


@pytest.mark.parametrize("reported", [2, 2.0, "2", "2.00000000", -2, "-2"])
async def test_a_sell_filled_in_full_is_recorded_whichever_sign_the_broker_uses(
    world: Saved, reported: Any
) -> None:
    """Sell requests carry a negative quantity; the sign of the answer is not verified
    against the broker. The amount is stored positive, as the order's own quantity is."""
    broker = AnsweringBroker(filled_ack(quantity=reported))

    await _submit(world, broker, side="sell")

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")
    assert order.avg_fill_price == Decimal("50.25")


@pytest.mark.parametrize("reported", [-3, 3, -1, 1, 0])
async def test_a_sell_is_not_finalised_by_a_wrong_amount_of_either_sign(
    world: Saved, reported: int
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=reported))

    await _submit(world, broker, side="sell")

    await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")


# --- broker order identity ----------------------------------------------------------------


@pytest.mark.parametrize(
    "order_id", [MISSING, None, "", "   ", 12.5, True, ["BRK-1"], {"id": "BRK-1"}, "x" * 101]
)
async def test_an_answer_without_a_usable_order_id_is_never_trusted_and_no_id_is_kept(
    world: Saved, order_id: Any
) -> None:
    broker = AnsweringBroker(filled_ack(order_id=order_id))

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id=None)
    assert attempt.outcome_evidence["reason"] == "order_id_not_usable"
    assert "broker_order_id" not in attempt.outcome_evidence


@pytest.mark.parametrize(("order_id", "stored"), [("BRK-1", "BRK-1"), (48850886521, "48850886521")])
async def test_a_text_or_whole_number_order_id_is_kept_as_text(
    world: Saved, order_id: Any, stored: str
) -> None:
    broker = AnsweringBroker(filled_ack(order_id=order_id))

    await _submit(world, broker)

    assert (await world.order()).broker_order_id == stored


@pytest.mark.parametrize("answer", [None, "FILLED", 7, ["FILLED"], b"{}"])
async def test_an_answer_that_is_not_an_object_leaves_the_order_unresolved(
    world: Saved, answer: Any
) -> None:
    broker = AnsweringBroker(answer)

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id=None)
    assert attempt.outcome_evidence["reason"] == "not_an_object"


# --- a final status that hides a fill ------------------------------------------------------


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
@pytest.mark.parametrize("reported", [1, 2, -1, 0.5, "1", float("nan"), "some"])
async def test_a_cancelled_or_rejected_answer_that_reports_a_fill_is_not_final(
    world: Saved, status: str, reported: Any
) -> None:
    """Final orders are never reconciled again, so a fill hidden behind a final status
    would be lost. The order is left for reconciliation by its broker order id instead."""
    broker = AnsweringBroker({"id": "BRK-1", "status": status, "filledQuantity": reported})

    await _submit(world, broker)

    await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")


@pytest.mark.parametrize(
    ("status", "final", "outcome"),
    [("CANCELLED", "cancelled", "accepted"), ("REJECTED", "rejected", "rejected")],
)
@pytest.mark.parametrize("reported", [MISSING, None, 0, 0.0, "0"])
async def test_a_cancelled_or_rejected_answer_with_no_fill_is_final_as_before(
    world: Saved, status: str, final: str, outcome: str, reported: Any
) -> None:
    answer: dict[str, Any] = {"id": "BRK-1", "status": status}
    if reported is not MISSING:
        answer["filledQuantity"] = reported
    broker = AnsweringBroker(answer)

    await _submit(world, broker)

    order = await world.order()
    assert order.status == final
    assert order.filled_quantity is None
    assert (await world.attempt()).outcome == outcome


@pytest.mark.parametrize("status", ["WORKING", "PENDING", "NEW", "LOCAL", "", "SOMETHING_NEW"])
@pytest.mark.parametrize("reported", [MISSING, 0, 1, 2, 5, -1, "nonsense"])
async def test_an_answer_that_is_not_final_keeps_the_order_open_for_reconciliation(
    world: Saved, status: str, reported: Any
) -> None:
    """Nothing final is written, so nothing here can hide: the order stays active and the
    reconciler asks about it by id, where quantities are checked."""
    answer: dict[str, Any] = {"id": "BRK-1", "status": status}
    if reported is not MISSING:
        answer["filledQuantity"] = reported
    broker = AnsweringBroker(answer)

    await _submit(world, broker)

    order = await world.order()
    assert order.status == "accepted"
    assert order.filled_quantity is None
    assert order.broker_order_id == "BRK-1"
    assert (await world.attempt()).outcome == "accepted"


# --- recovery: read-only, by broker order id, never a second send --------------------------


async def test_an_untrusted_answer_is_recovered_by_reading_the_order_and_never_sent_again(
    world: Saved,
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=3))
    await _submit(world, broker)
    await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")

    async with world.sessions() as later:
        engine = ExecutionEngine(later, broker)
        # The same request arrives again: it finds the original order and cannot send it.
        same = await engine.create_order_intent(
            ticker=world.ticker,
            side="buy",
            order_type="market",
            quantity=QUANTITY,
            is_dry_run=False,
            estimated_price=Decimal("50"),
        )
        assert same.id == (await world.order()).id
        with pytest.raises(ValueError, match="submission_unknown"):
            await engine.submit_order(same)

        # The broker still tells the same contradictory story: nothing changes.
        broker.by_id["BRK-1"] = filled_ack(quantity=3)
        await engine.reconcile_order(same)
        await later.commit()
    assert (await world.order()).status == "submission_unknown"

    async with world.sessions() as later:
        order = (
            await later.execute(select(Order).where(Order.ticker == world.ticker))
        ).scalar_one()
        broker.by_id["BRK-1"] = filled_ack(quantity=2, price=50.5)
        await ExecutionEngine(later, broker).reconcile_order(order)
        await later.commit()

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")
    assert order.avg_fill_price == Decimal("50.5")
    assert broker.reads == ["BRK-1", "BRK-1"]
    assert len(broker.posts) == 1, "the order was sent to the broker a second time"
    assert (await world.attempt()).outcome == "ambiguous", "the first outcome was rewritten"


async def test_a_fill_hidden_behind_cancelled_is_recorded_when_the_order_is_read(
    world: Saved,
) -> None:
    broker = AnsweringBroker({"id": "BRK-1", "status": "CANCELLED", "filledQuantity": 1})
    await _submit(world, broker)

    async with world.sessions() as later:
        order = (
            await later.execute(select(Order).where(Order.ticker == world.ticker))
        ).scalar_one()
        broker.by_id["BRK-1"] = {
            "id": "BRK-1",
            "status": "CANCELLED",
            "filledQuantity": 1,
            "filledPrice": 50.0,
        }
        await ExecutionEngine(later, broker).reconcile_order(order)
        await later.commit()

    order = await world.order()
    assert order.status == "cancelled"
    assert order.filled_quantity == Decimal("1"), "the partial fill was lost"
    assert len(broker.posts) == 1


# --- an answer about another order (N-22) --------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"ticker": "SOME_OTHER_EQ"}, "ticker_differs"),
        ({"side": "SELL"}, "side_differs"),
        ({"type": "LIMIT"}, "order_type_differs"),
        ({"quantity": 3}, "quantity_differs"),
        ({"quantity": -2}, "quantity_sign_contradicts_side"),
        ({"quantity": "plenty"}, "quantity_not_a_number"),
    ],
)
@pytest.mark.parametrize("status", ["FILLED", "WORKING", "REJECTED"])
async def test_an_answer_that_says_it_is_about_another_order_is_not_adopted(
    world: Saved, claims: dict[str, Any], reason: str, status: str
) -> None:
    answer = {**filled_ack(quantity=2, price=50.25), "status": status, **claims}
    if status != "FILLED":
        answer["filledQuantity"] = 0
    broker = AnsweringBroker(answer)

    await _submit(world, broker)

    # The id the broker gave belongs to an order that is not this one: it is kept as
    # evidence, never as the order's identity, so nothing can settle or cancel by it.
    attempt = await _assert_kept_unresolved(world, broker, broker_order_id=None)
    assert attempt.outcome_evidence["reason"] == reason
    assert attempt.outcome_evidence["error_type"] == "UntrustedRecord"
    assert attempt.outcome_evidence["named_broker_order_id"] == "BRK-1"
    assert "broker_order_id" not in attempt.outcome_evidence


async def test_an_answer_that_agrees_with_the_order_in_every_stated_detail_is_adopted(
    world: Saved,
) -> None:
    broker = AnsweringBroker(
        {
            **filled_ack(quantity=2, price=50.25),
            "ticker": world.ticker.lower(),
            "side": "Buy",
            "type": "MARKET",
            "quantity": 2.0,
        }
    )

    await _submit(world, broker)

    order = await world.order()
    assert order.status == "filled"
    assert order.broker_order_id == "BRK-1"
    assert await world.risk_events("submission_unknown") == []


async def test_a_sell_answered_with_its_negative_quantity_is_adopted(world: Saved) -> None:
    broker = AnsweringBroker(
        {**filled_ack(quantity=-2, price=50.25), "ticker": world.ticker, "quantity": -2}
    )

    await _submit(world, broker, side="sell")

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")


async def test_an_order_named_for_another_instrument_cannot_be_settled_or_cancelled_by_that_id(
    world: Saved,
) -> None:
    broker = AnsweringBroker({**filled_ack(), "ticker": "SOME_OTHER_EQ"})
    await _submit(world, broker)
    await _assert_kept_unresolved(world, broker, broker_order_id=None)
    # A later answer for that id which no longer names the instrument would have been
    # adopted as a fill of this order if the id had been kept.
    broker.by_id["BRK-1"] = filled_ack()

    async with world.sessions() as later:
        order = (
            await later.execute(select(Order).where(Order.ticker == world.ticker))
        ).scalar_one()
        engine = ExecutionEngine(later, broker)
        await engine.reconcile_order(order)
        await later.commit()

    order = await world.order()
    assert order.status == "submission_unknown"
    assert order.broker_order_id is None
    assert order.avg_fill_price is None
    assert broker.reads == [], "the other order's id was read as if it were this order's"
    assert len(broker.posts) == 1


async def test_a_refused_sell_is_recovered_by_reading_the_order(world: Saved) -> None:
    broker = AnsweringBroker(filled_ack(quantity=1))
    await _submit(world, broker, side="sell")
    await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")

    async with world.sessions() as later:
        order = (
            await later.execute(select(Order).where(Order.ticker == world.ticker))
        ).scalar_one()
        broker.by_id["BRK-1"] = filled_ack(quantity=2, price=49.5)
        await ExecutionEngine(later, broker).reconcile_order(order)
        await later.commit()

    order = await world.order()
    assert order.status == "filled"
    assert order.filled_quantity == Decimal("2")
    assert len(broker.posts) == 1


async def test_nothing_from_a_refused_answer_is_stored_anywhere(world: Saved) -> None:
    marker = "MARK-7741-do-not-store"
    broker = AnsweringBroker(
        {
            "id": "BRK-1",
            "status": "FILLED",
            "filledQuantity": marker,
            "filledPrice": marker,
            "note": marker,
        }
    )

    await _submit(world, broker)

    await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    order = await world.order()
    attempt = await world.attempt()
    stored = [
        order.error_message,
        order.broker_response,
        order.execution_quality_notes,
        attempt.outcome_evidence,
        *[event.payload for event in await world.events("submission_outcome_unknown")],
        *[event.payload for event in await world.risk_events("submission_unknown")],
        *[event.message for event in await world.risk_events("submission_unknown")],
        *[alert.payload for alert in await world.alerts()],
        *[alert.message for alert in await world.alerts()],
        *[row.payload for row in await world.audit_rows()],
    ]
    assert marker not in repr(stored)


# --- transactions: the evidence of a sent request survives whatever happens next -----------


class DatabaseDown(Exception):
    pass


async def test_a_failed_save_of_a_good_answer_leaves_the_sent_request_on_record(
    world: Saved,
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=2))

    async with world.sessions() as session:
        engine = ExecutionEngine(session, broker)
        order = await engine.create_order_intent(
            ticker=world.ticker,
            side="buy",
            order_type="market",
            quantity=QUANTITY,
            is_dry_run=False,
            estimated_price=Decimal("50"),
        )
        real_commit = session.commit
        commits: list[str] = []

        async def commit() -> None:
            # The first commit records the dispatch; the second would record the answer.
            if commits:
                raise DatabaseDown("the answer could not be saved")
            await real_commit()
            commits.append("dispatch")

        session.commit = commit  # type: ignore[method-assign]
        with pytest.raises(DatabaseDown):
            await engine.submit_order(order)

    order = await world.order()
    assert order.status == "submission_unknown"
    assert order.filled_quantity is None
    assert order.broker_order_id is None
    attempt = await world.attempt()
    assert attempt.outcome is None
    assert attempt.dispatch_committed_at is not None
    assert len(broker.posts) == 1


async def test_a_good_answer_that_cannot_be_saved_once_is_kept_as_unresolved_with_its_id(
    world: Saved,
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=2))

    async with world.sessions() as session:
        engine = ExecutionEngine(session, broker)
        order = await engine.create_order_intent(
            ticker=world.ticker,
            side="buy",
            order_type="market",
            quantity=QUANTITY,
            is_dry_run=False,
            estimated_price=Decimal("50"),
        )
        real_commit = session.commit
        commits: list[str] = []

        async def commit() -> None:
            commits.append("asked")
            if len(commits) == 2:
                raise DatabaseDown("the answer could not be saved")
            await real_commit()

        session.commit = commit  # type: ignore[method-assign]
        await engine.submit_order(order)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    assert attempt.outcome_evidence == {
        "error_type": "DatabaseDown",
        "http_status": None,
        "broker_order_id": "BRK-1",
    }


@pytest.mark.parametrize("price", ["1e15", "1e9999999", 5e11])
async def test_a_fill_price_the_database_cannot_hold_does_not_lose_the_order(
    world: Saved, price: Any
) -> None:
    """The answer passes every check and then cannot be stored (the price, or the slippage
    worked out from it, does not fit its column). The order must not be left half-recorded
    or without its broker order id."""
    broker = AnsweringBroker(filled_ack(quantity=2, price=price))

    await _submit(world, broker)

    attempt = await _assert_kept_unresolved(world, broker, broker_order_id="BRK-1")
    assert attempt.outcome_evidence["broker_order_id"] == "BRK-1"
    assert "reason" not in attempt.outcome_evidence
    assert str(price) not in repr(attempt.outcome_evidence)


async def test_a_run_cancelled_while_the_answer_is_being_saved_writes_no_fill(
    world: Saved,
) -> None:
    broker = AnsweringBroker(filled_ack(quantity=2))
    row_held = asyncio.Event()
    release = asyncio.Event()
    holders: list[asyncio.Future[None]] = []

    async def hold_the_row() -> None:
        async with world.sessions() as other:
            held = await other.execute(
                text("SELECT id FROM orders WHERE ticker = :ticker FOR UPDATE"),
                {"ticker": world.ticker},
            )
            assert held.first() is not None, "the dispatched order was not there to lock"
            row_held.set()
            await release.wait()
            await other.rollback()

    async def another_writer_takes_the_row() -> None:
        # Runs while the request is with the broker: the dispatch is already committed.
        holders.append(asyncio.ensure_future(hold_the_row()))
        async with asyncio.timeout(10):
            await row_held.wait()

    broker.before_answering = another_writer_takes_the_row
    submission = asyncio.ensure_future(_submit(world, broker))
    try:
        async with asyncio.timeout(10):
            await row_held.wait()
        # The answer has arrived; recording it is waiting for the row.
        await asyncio.sleep(0.3)
        assert not submission.done(), "the answer was recorded without waiting for the row"
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
    finally:
        release.set()
        await asyncio.gather(*holders, return_exceptions=True)

    order = await world.order()
    assert order.status == "submission_unknown"
    assert order.filled_quantity is None
    assert (await world.attempt()).outcome is None
    assert len(broker.posts) == 1


async def test_an_untrusted_answer_does_not_disturb_an_order_reconciliation_already_settled(
    world: Saved, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciliation settles the order from broker history while the request is still in
    flight; the request then returns a contradictory answer."""
    monkeypatch.setattr(ExecutionEngine, "UNKNOWN_SUBMISSION_MIN_AGE", timedelta(0))
    broker = AnsweringBroker(filled_ack(quantity=3, order_id="BRK-LATE"))

    async def settle_from_history() -> None:
        async with world.sessions() as reconciler:
            order = (
                await reconciler.execute(select(Order).where(Order.ticker == world.ticker))
            ).scalar_one()
            attempt = (
                await reconciler.execute(
                    select(OrderSubmissionAttempt).where(
                        OrderSubmissionAttempt.order_id == order.id
                    )
                )
            ).scalar_one()

            class History(AnsweringBroker):
                async def get_historical_orders(self, **_kwargs: Any) -> Any:
                    return {
                        "items": [
                            {
                                "id": "BRK-HISTORY",
                                "ticker": world.ticker,
                                "status": "FILLED",
                                "type": "MARKET",
                                "quantity": 2.0,
                                "filledQuantity": 2.0,
                                "filledPrice": 50.25,
                                "createdAt": attempt.dispatch_committed_at.isoformat(),
                            }
                        ]
                    }

            await ExecutionEngine(reconciler, History(None)).reconcile_unknown_submission(order)
            await reconciler.commit()

    broker.before_answering = settle_from_history

    await _submit(world, broker)

    order = await world.order()
    assert order.status == "filled"
    assert order.broker_order_id == "BRK-HISTORY"
    assert order.filled_quantity == Decimal("2")
    attempt = await world.attempt()
    assert attempt.outcome == "ambiguous"
    assert attempt.outcome_evidence["reason"] == "filled_quantity_exceeds_order"
    assert len(await world.events("late_submission_outcome")) == 1
    conflicts = await world.risk_events("submission_identity_conflict")
    assert len(conflicts) == 1, "two broker order ids for one request must be raised"
    assert len(broker.posts) == 1
