"""N-20 at the callers: every way of cancelling reports what the broker's record says.

One run meets three orders: one cancelled after part of it filled, one whose cancellation the
broker took but has not yet recorded, and one that filled before the cancel could take effect.
None of them may be reported as a plain cancellation, and the second must stay open.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.api.v1.routes import emergency as emergency_route
from app.api.v1.routes import orders as orders_route
from app.broker.trading212 import T212APIError
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order
from app.execution.engine import ExecutionEngine
from app.services.system_control import SystemControlService
from app.workers import tasks
from tests.integration.scheduled_reconciliation_cases import (
    Brokers,
    add_connection,
    install_borrowed_session,
)
from tests.integration.test_emergency_operations import EmergencyBroker, _order

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

LONG_AGO = datetime.now(UTC) - timedelta(hours=3)
ADMIN = SimpleNamespace(email="admin@example.test", id=uuid.uuid4())

# What the broker's record says once each cancel request has been taken.
RECORDS: dict[str, Any] = {
    "broker-AAA": {"status": "CANCELLED", "filledQuantity": 4, "filledPrice": 10.5},
    "broker-BBB": T212APIError(404, {"code": "OrderNotFound"}),
    "broker-CCC": {"status": "FILLED", "filledQuantity": 10, "filledPrice": 10},
}


class RecordedOutcomeBroker(EmergencyBroker):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []
        self.calls: list[str] = []

    async def cancel_order(self, broker_order_id: str) -> dict[str, str]:
        self.calls.append("cancel")
        return await super().cancel_order(broker_order_id)

    async def get_order_by_id(self, broker_order_id: str) -> dict[str, Any]:
        self.calls.append("read")
        self.reads.append(broker_order_id)
        record = RECORDS[broker_order_id]
        if isinstance(record, BaseException):
            raise record
        return {"id": broker_order_id, **record}

    async def get_historical_orders(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": [], "nextPagePath": None}


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)


async def _three_orders(db: AsyncSession) -> None:
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    db.add_all(
        [
            _order(
                ticker=name,
                status="accepted",
                order_type="limit",
                quantity=Decimal("10"),
                limit_price=Decimal("10"),
                broker_order_id=f"broker-{name}",
                created_at=LONG_AGO,
                submitted_at=LONG_AGO,
            )
            for name in ("AAA", "BBB", "CCC")
        ]
    )
    await db.commit()


async def _orders(db: AsyncSession) -> dict[str, Order]:
    rows = await db.execute(select(Order).execution_options(populate_existing=True))
    return {order.ticker: order for order in rows.scalars().all()}


async def _assert_recorded_from_the_broker(db: AsyncSession) -> None:
    orders = await _orders(db)
    assert orders["AAA"].status == "cancelled"
    assert orders["AAA"].filled_quantity == Decimal("4")
    assert orders["AAA"].avg_fill_price == Decimal("10.5")
    assert orders["BBB"].status == "accepted", "an unconfirmed cancellation was called final"
    assert orders["BBB"].filled_quantity is None
    assert orders["CCC"].status == "filled"
    assert orders["CCC"].filled_quantity == Decimal("10")


async def _audit(db: AsyncSession, action: str) -> AuditLog:
    return (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalar_one()


# ── one order at a time ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticker", "status_code", "body", "action"),
    [
        pytest.param(
            "AAA",
            200,
            {"cancelled": True, "filled_quantity": "4"},
            "order_cancelled",
            id="cancelled-after-a-partial-fill",
        ),
        pytest.param(
            "BBB",
            202,
            {
                "cancelled": False,
                "awaiting_confirmation": True,
                "status": "accepted",
                "reason": "no_broker_record",
                "requires_reconciliation": True,
            },
            "order_cancel_unconfirmed",
            id="awaiting-confirmation",
        ),
        pytest.param(
            "CCC",
            409,
            {"cancelled": False, "status": "filled", "filled_quantity": "10"},
            "order_cancel_too_late",
            id="filled-before-the-cancel",
        ),
    ],
)
async def test_the_cancel_route_answers_with_what_the_record_says(
    db: AsyncSession, demo: None, ticker, status_code, body, action
) -> None:
    await _three_orders(db)
    order = (await _orders(db))[ticker]
    broker = RecordedOutcomeBroker()

    answer = await orders_route.cancel_order(
        order_id=order.id, current_user=ADMIN, db=db, broker=broker
    )
    await db.commit()

    if isinstance(answer, dict):
        got_status, got = 200, answer
    else:
        got_status, got = answer.status_code, json.loads(answer.body)
    assert got_status == status_code
    assert {key: got[key] for key in body} == body
    assert got["order_id"] == str(order.id)
    audit = await _audit(db, action)
    assert audit.entity_id == str(order.id)


# ── many at once ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_reports_each_outcome_on_its_own(db: AsyncSession, demo: None) -> None:
    await _three_orders(db)
    broker = RecordedOutcomeBroker()

    answer = await orders_route.cancel_all_pending(current_user=ADMIN, db=db, broker=broker)
    await db.commit()

    # The answer names the orders that need a look, not only how many.
    (finished,) = answer.pop("already_finished_orders")
    assert (finished["ticker"], finished["status"], finished["filled_quantity"]) == (
        "CCC",
        "filled",
        "10",
    )
    (partial,) = answer.pop("cancelled_after_partial_fill")
    assert (partial["ticker"], partial["filled_quantity"], partial["quantity"]) == (
        "AAA",
        "4",
        "10",
    )
    (unconfirmed,) = answer.pop("awaiting_confirmation_orders")
    assert (unconfirmed["ticker"], unconfirmed["status"]) == ("BBB", "accepted")
    assert answer == {
        "cancelled": 1,
        "failed": 0,
        "already_finished": 1,
        "awaiting_confirmation": 1,
    }
    # Every cancel request went out before any order was read back.
    assert broker.calls == ["cancel"] * 3 + ["read"] * 3
    await _assert_recorded_from_the_broker(db)
    audit = await _audit(db, "cancel_all_pending")
    assert [entry["ticker"] for entry in audit.payload["awaiting_confirmation_orders"]] == ["BBB"]
    assert [
        (entry["ticker"], entry["status"]) for entry in audit.payload["already_finished_orders"]
    ] == [("CCC", "filled")]
    (partial,) = audit.payload["cancelled_after_partial_fill"]
    assert (partial["ticker"], partial["filled_quantity"], partial["quantity"]) == (
        "AAA",
        "4",
        "10",
    )


@pytest.mark.asyncio
async def test_a_second_cancel_all_reads_and_does_not_ask_again(
    db: AsyncSession, demo: None
) -> None:
    await _three_orders(db)
    broker = RecordedOutcomeBroker()
    await orders_route.cancel_all_pending(current_user=ADMIN, db=db, broker=broker)
    await db.commit()

    again = await orders_route.cancel_all_pending(current_user=ADMIN, db=db, broker=broker)
    await db.commit()

    # Only the unconfirmed order was still pending; it is read, not cancelled a second time.
    assert again.pop("awaiting_confirmation_orders")[0]["ticker"] == "BBB"
    assert again == {"cancelled": 0, "failed": 0, "awaiting_confirmation": 1}
    assert sorted(broker.cancelled_order_ids) == ["broker-AAA", "broker-BBB", "broker-CCC"]
    assert broker.reads.count("broker-BBB") == 2


@pytest.mark.asyncio
async def test_the_emergency_cancel_all_is_not_a_success_while_any_is_unconfirmed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _three_orders(db)
    broker = RecordedOutcomeBroker()

    async def the_broker(_self: SystemControlService, _purpose: str) -> EmergencyBroker:
        return broker

    monkeypatch.setattr(SystemControlService, "_get_broker", the_broker)

    result = await emergency_route.emergency_cancel_all(current_user=ADMIN, db=db)
    await db.commit()

    assert result.success is False
    # Every cancel request went out before any order was read back.
    assert broker.calls == ["cancel"] * 3 + ["read"] * 3
    assert result.message == (
        "Cancelled 1 pending orders. 1 had already finished (CCC filled)."
        " 1 cancelled after a partial fill (AAA 4 of 10)."
        " 1 cancellation request(s) accepted by the broker and awaiting confirmation (BBB):"
        " those orders remain OPEN until their outcome is read."
    )
    await _assert_recorded_from_the_broker(db)
    audit = await _audit(db, "emergency_cancel_all")
    assert audit.payload["awaiting_confirmation_count"] == 1
    assert audit.payload["cancelled_count"] == 1


@pytest.mark.asyncio
async def test_the_timed_out_sweep_reports_each_outcome_on_its_own(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await add_connection(db)
    await _three_orders(db)
    brokers = Brokers()
    broker = RecordedOutcomeBroker()
    brokers.by_user[connection.user_id] = broker  # type: ignore[assignment]
    install_borrowed_session(monkeypatch, brokers, db)

    summary = await tasks.cancel_timed_out_orders.run()

    assert summary["cancelled"] == 1
    assert summary["already_finished"] == 1
    assert summary["awaiting_confirmation"] == 1
    assert "failed" not in summary
    # Every cancel request went out before any order was read back.
    assert broker.calls == ["cancel"] * 3 + ["read"] * 3
    await _assert_recorded_from_the_broker(db)


@pytest.mark.asyncio
async def test_cancel_all_reports_an_order_it_could_not_record_and_keeps_the_rest(
    db: AsyncSession, demo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _three_orders(db)
    broker = RecordedOutcomeBroker()
    real = ExecutionEngine._record_final_state

    async def fails_after_changing_aaa(self, order, record, found):
        await real(self, order, record, found)
        if order.ticker == "AAA":
            await self.db.flush()
            raise RuntimeError("local fault")

    monkeypatch.setattr(ExecutionEngine, "_record_final_state", fails_after_changing_aaa)

    answer = await orders_route.cancel_all_pending(current_user=ADMIN, db=db, broker=broker)
    await db.commit()

    unconfirmed = {entry["ticker"]: entry for entry in answer["awaiting_confirmation_orders"]}
    assert (unconfirmed["AAA"]["status"], unconfirmed["AAA"]["reason"]) == (
        "accepted",
        "local_error",
    )
    assert answer["already_finished"] == 1
    orders = await _orders(db)
    assert orders["AAA"].status == "accepted"
    assert orders["AAA"].filled_quantity is None
    assert orders["CCC"].status == "filled"
