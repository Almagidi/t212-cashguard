"""The emergency paths finish their job and say what happened.

Cancel-all (service, route and the timed-out sweep) must go on past an order that ended while
the batch was running; an emergency flatten that the safety policy does not allow must answer
with a refusal, not an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

from app.api.v1.routes import emergency as emergency_route
from app.api.v1.routes import orders as orders_route
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order, OrderEvent, TelegramControlRequest
from app.execution.state_machine import InvalidOrderTransition, require_already_finished
from app.services.system_control import FlattenSummary, SystemControlService
from app.services.telegram_control import TelegramControlService
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


class EndingBroker(EmergencyBroker):
    """While the first cancellation is under way, another of the listed orders ends, as
    when reconciliation records its fill."""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__()
        self.db = db
        self.ended_meanwhile: str | None = None
        self.account_scope: str | None = None

    async def cancel_order(self, broker_order_id: str) -> dict[str, str]:
        if self.ended_meanwhile is None:
            other = (
                await self.db.execute(
                    text(
                        "SELECT id, broker_order_id FROM orders WHERE status = 'accepted' "
                        "AND broker_order_id != :this ORDER BY ticker LIMIT 1"
                    ),
                    {"this": broker_order_id},
                )
            ).one()
            await self.db.execute(
                text("UPDATE orders SET status = 'filled' WHERE id = :id"), {"id": other.id}
            )
            self.ended_meanwhile = other.broker_order_id
        return await super().cancel_order(broker_order_id)


async def _three_working_orders(db: AsyncSession) -> None:
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    db.add_all(
        [
            _order(
                ticker=name,
                status="accepted",
                order_type="limit",
                limit_price=Decimal("10"),
                broker_order_id=f"broker-{name}",
                created_at=LONG_AGO,
                submitted_at=LONG_AGO,
            )
            for name in ("AAA", "BBB", "CCC")
        ]
    )
    await db.commit()


async def _statuses(db: AsyncSession) -> dict[str, str]:
    rows = await db.execute(
        select(Order.broker_order_id, Order.status).execution_options(populate_existing=True)
    )
    return dict(rows.all())


def _assert_the_rest_were_cancelled(broker: EndingBroker, statuses: dict[str, str]) -> None:
    assert broker.ended_meanwhile is not None, "no order ended meanwhile; nothing is proved"
    others = sorted(set(statuses) - {broker.ended_meanwhile})
    assert statuses[broker.ended_meanwhile] == "filled"
    assert [statuses[name] for name in others] == ["cancelled", "cancelled"]
    assert sorted(broker.cancelled_order_ids) == others, "an ended order was cancelled again"


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)


async def _service(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, broker: EmergencyBroker
) -> SystemControlService:
    service = SystemControlService(db)

    async def the_broker(_purpose: str) -> EmergencyBroker:
        return broker

    monkeypatch.setattr(service, "_get_broker", the_broker)
    return service


# ── Cancel-all goes on past an order that has just ended ──────────────────────


@pytest.mark.asyncio
async def test_emergency_cancel_all_cancels_the_rest_when_one_order_ended_meanwhile(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _three_working_orders(db)
    broker = EndingBroker(db)
    service = await _service(db, monkeypatch, broker)

    summary = await service.cancel_all_pending_summary(actor="test")
    await db.commit()

    assert (summary.cancelled, summary.failed, summary.already_finished) == (2, 0, 1)
    assert broker.ended_meanwhile is not None
    ended = broker.ended_meanwhile.removeprefix("broker-")
    # The order that ended is named with how it ended: a fill is exposure the cancel did
    # not prevent, and the operator must not have to go looking for it.
    assert summary.message == (
        f"Cancelled 2 pending orders. 1 had already finished ({ended} filled)."
    )
    _assert_the_rest_were_cancelled(broker, await _statuses(db))
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "emergency_cancel_all"))
    ).scalar_one()
    (finished,) = audit.payload.pop("already_finished_orders")
    assert (finished["ticker"], finished["status"]) == (ended, "filled")
    assert audit.payload == {
        "source": "system_control",
        "cancelled_count": 2,
        "already_finished_count": 1,
    }


@pytest.mark.asyncio
async def test_the_cancel_all_route_cancels_the_rest_when_one_order_ended_meanwhile(
    db: AsyncSession, demo: None
) -> None:
    await _three_working_orders(db)
    broker = EndingBroker(db)

    answer = await orders_route.cancel_all_pending(
        current_user=SimpleNamespace(email="admin@example.test"), db=db, broker=broker
    )
    await db.commit()

    assert answer == {"cancelled": 2, "failed": 0, "already_finished": 1}
    _assert_the_rest_were_cancelled(broker, await _statuses(db))
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "cancel_all_pending"))
    ).scalar_one()
    (finished,) = audit.payload.pop("already_finished_orders")
    assert finished["status"] == "filled"
    assert audit.payload == {
        "cancelled_count": 2,
        "failed_count": 0,
        "already_finished_count": 1,
    }


@pytest.mark.asyncio
async def test_the_timed_out_sweep_cancels_the_rest_when_one_order_ended_meanwhile(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await add_connection(db)
    await _three_working_orders(db)
    brokers = Brokers()
    broker = EndingBroker(db)
    brokers.by_user[connection.user_id] = broker  # type: ignore[assignment]
    install_borrowed_session(monkeypatch, brokers, db)

    summary = await tasks.cancel_timed_out_orders.run()

    assert summary["cancelled"] == 2
    assert summary["already_finished"] == 1
    assert "failed" not in summary
    _assert_the_rest_were_cancelled(broker, await _statuses(db))


@pytest.mark.asyncio
async def test_cancel_all_with_nothing_ending_meanwhile_reports_as_before(
    db: AsyncSession, demo: None
) -> None:
    await _three_working_orders(db)
    broker = EmergencyBroker()

    answer = await orders_route.cancel_all_pending(
        current_user=SimpleNamespace(email="admin@example.test"), db=db, broker=broker
    )

    assert answer == {"cancelled": 3, "failed": 0}


# ── A flatten that may not be sent is refused, not crashed ────────────────────


async def _halted(db: AsyncSession, *, halted: bool) -> None:
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=halted))
    await db.commit()


@pytest.mark.asyncio
async def test_a_flatten_while_trading_is_halted_is_refused_and_recorded(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=True)
    broker = EmergencyBroker(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])
    service = await _service(db, monkeypatch, broker)

    summary = await service.flatten_all_summary(actor="test")
    await db.commit()

    assert summary.refused is True
    assert summary.complete is False
    assert summary.message.startswith("Flatten refused: ")
    assert "No exit order was sent." in summary.message
    assert summary.message.endswith("1 position(s) remain OPEN and were not attempted (AAA).")
    assert "Kill switch" in summary.message
    assert broker.market_orders == [], "an exit was sent although trading is halted"
    (order,) = (await db.execute(select(Order))).scalars().all()
    assert order.status == "cancelled", "the unsent exit was left looking as if it might be sent"
    assert order.broker_order_id is None
    actions = set((await db.execute(select(AuditLog.action))).scalars().all())
    assert "emergency_flatten_all" in actions
    assert "demo_order_blocked_by_kill_switch" in actions, "the refusal itself was not kept"
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "emergency_flatten_all"))
    ).scalar_one()
    assert audit.payload["flattened"] == 0
    assert "Kill switch" in audit.payload["refused"]
    assert audit.payload["not_attempted"] == ["AAA"]


@pytest.mark.asyncio
async def test_a_halt_that_lands_during_a_flatten_stops_it_and_says_how_far_it_got(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=False)

    class HaltedAfterTheFirstExit(EmergencyBroker):
        async def place_market_order(self, ticker: str, quantity: Decimal, **kwargs: Any) -> Any:
            answer = await super().place_market_order(ticker, quantity, **kwargs)
            await db.execute(text("UPDATE app_settings SET kill_switch_active = 1 WHERE id = 1"))
            return answer

    broker = HaltedAfterTheFirstExit(
        positions=[
            {"ticker": "AAA", "quantity": "2", "currentPrice": "10"},
            {"ticker": "BBB", "quantity": "3", "currentPrice": "10"},
            {"ticker": "CCC", "quantity": "4", "currentPrice": "10"},
        ]
    )
    service = await _service(db, monkeypatch, broker)

    summary = await service.flatten_all_summary(actor="test")
    await db.commit()

    assert summary.refused is True
    assert "1 exit order(s) had been accepted by the broker before that." in summary.message
    assert summary.message.endswith("2 position(s) remain OPEN and were not attempted (BBB, CCC).")
    assert "No exit order was sent" not in summary.message
    assert [sent["ticker"] for sent in broker.market_orders] == ["AAA"]
    statuses = dict((await db.execute(select(Order.ticker, Order.status))).all())
    # Nothing is attempted after the refusal: no intent is even created for the third.
    assert statuses == {"AAA": "accepted", "BBB": "cancelled"}


@pytest.mark.asyncio
async def test_the_flatten_route_reports_a_refusal_as_not_successful(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=True)
    broker = EmergencyBroker(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])

    async def the_broker(_self: Any, _purpose: str) -> EmergencyBroker:
        return broker

    monkeypatch.setattr(SystemControlService, "_get_broker", the_broker)

    result = await emergency_route.emergency_flatten_all(
        current_user=SimpleNamespace(id=None, email="admin@example.test"), db=db
    )

    assert result.success is False
    assert result.action == "flatten_all"
    assert result.message.startswith("Flatten refused: ")


@pytest.mark.asyncio
async def test_the_flatten_route_reports_a_flatten_that_went_out_as_successful(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=False)
    broker = EmergencyBroker(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])

    async def the_broker(_self: Any, _purpose: str) -> EmergencyBroker:
        return broker

    monkeypatch.setattr(SystemControlService, "_get_broker", the_broker)

    result = await emergency_route.emergency_flatten_all(
        current_user=SimpleNamespace(id=None, email="admin@example.test"), db=db
    )

    assert result.success is True
    assert result.message == "Flattened 1 positions."
    assert len(broker.market_orders) == 1


@pytest.mark.asyncio
async def test_a_refusal_after_an_exit_with_an_unknown_outcome_does_not_say_nothing_was_sent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=False)

    class TimesOutThenHalted(EmergencyBroker):
        async def place_market_order(self, ticker: str, quantity: Decimal, **kwargs: Any) -> Any:
            await db.execute(text("UPDATE app_settings SET kill_switch_active = 1 WHERE id = 1"))
            raise TimeoutError("no answer")

    broker = TimesOutThenHalted(
        positions=[
            {"ticker": "AAA", "quantity": "2", "currentPrice": "10"},
            {"ticker": "BBB", "quantity": "3", "currentPrice": "10"},
        ]
    )
    service = await _service(db, monkeypatch, broker)

    summary = await service.flatten_all_summary(actor="test")
    await db.commit()

    assert summary.refused is True
    assert "No exit order was sent" not in summary.message, "a request went out and may be live"
    assert "0 exit order(s) had been accepted by the broker before that." in summary.message
    assert "1 exit order(s) have an unknown outcome and need reconciliation (AAA)." in (
        summary.message
    )
    assert summary.message.endswith("1 position(s) remain OPEN and were not attempted (BBB).") or (
        "1 position(s) remain OPEN and were not attempted (BBB)." in summary.message
    )
    assert summary.nothing_went_out is False
    statuses = dict((await db.execute(select(Order.ticker, Order.status))).all())
    assert statuses == {"AAA": "submission_unknown", "BBB": "cancelled"}


@pytest.mark.asyncio
async def test_a_refused_flatten_leaves_an_exit_intent_it_did_not_create_alone(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    """Asking for an exit can hand back an identical intent another part of the application
    made a moment ago and will send itself. That one is not this flatten's to close."""
    await _halted(db, halted=True)
    a_minute_ago = datetime.now(UTC) - timedelta(seconds=60)
    theirs = _order(
        ticker="AAA",
        side="sell",
        order_type="market",
        quantity=Decimal("2"),
        status="pending_intent",
        time_validity="DAY",
        created_at=a_minute_ago,
    )
    db.add(theirs)
    await db.flush()
    db.add(
        OrderEvent(
            order_id=theirs.id,
            event_type="intent_created",
            to_status="pending_intent",
            occurred_at=a_minute_ago,
        )
    )
    await db.commit()
    broker = EmergencyBroker(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])
    service = await _service(db, monkeypatch, broker)

    summary = await service.flatten_all_summary(actor="test")
    await db.commit()

    assert summary.refused is True
    assert broker.market_orders == []
    (order,) = (await db.execute(select(Order))).scalars().all()
    assert order.id == theirs.id, "the flatten did not find the standing intent; nothing is proved"
    assert order.status == "pending_intent", "an intent the flatten did not create was closed"


@pytest.mark.asyncio
async def test_cancel_all_names_the_orders_whose_cancellation_failed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _three_working_orders(db)

    class RefusesToCancel(EmergencyBroker):
        async def cancel_order(self, broker_order_id: str) -> dict[str, str]:
            raise RuntimeError("broker says no")

    service = await _service(db, monkeypatch, RefusesToCancel())

    summary = await service.cancel_all_pending_summary(actor="test")
    await db.commit()

    assert (summary.cancelled, summary.failed, summary.already_finished) == (0, 3, 0)
    assert sorted(summary.failed_orders) == ["AAA", "BBB", "CCC"]
    for ticker in ("AAA", "BBB", "CCC"):
        assert ticker in summary.message
    assert set((await _statuses(db)).values()) == {"accepted"}, "a failed cancel ended an order"
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "emergency_cancel_all"))
    ).scalar_one()
    assert sorted(entry["ticker"] for entry in audit.payload["failed_orders"]) == [
        "AAA",
        "BBB",
        "CCC",
    ]


@pytest.mark.asyncio
async def test_a_simulated_order_that_ended_meanwhile_does_not_stop_the_batch(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    db.add(AppSettings(id=1, auto_trading_enabled=True, kill_switch_active=False))
    simulated = [
        _order(ticker=name, status="accepted", is_dry_run=True, execution_environment="dry_run")
        for name in ("AAA", "BBB")
    ]
    db.add_all(simulated)
    await db.commit()
    listed = list(simulated)

    async def listed_before_one_ended(_self: Any) -> list[Order]:
        return listed

    monkeypatch.setattr(
        "app.services.system_control.OrderRepository.list_pending", listed_before_one_ended
    )
    await db.execute(text("UPDATE orders SET status = 'filled' WHERE ticker = 'AAA'"))
    service = await _service(db, monkeypatch, EmergencyBroker())

    summary = await service.cancel_all_pending_summary(actor="test")
    await db.commit()

    assert (summary.cancelled, summary.already_finished) == (1, 1)
    statuses = dict((await db.execute(select(Order.ticker, Order.status))).all())
    assert statuses == {"AAA": "filled", "BBB": "cancelled"}


def test_an_order_that_could_not_be_cancelled_for_any_other_reason_is_not_skipped() -> None:
    """Only an order that has ended may be passed over; anything else is a fault."""
    require_already_finished(_order(status="filled"))
    require_already_finished(_order(status="rejected"))
    for status in ("accepted", "submission_unknown", "something_unknown"):
        with pytest.raises(InvalidOrderTransition):
            require_already_finished(_order(status=status))


@pytest.mark.asyncio
async def test_a_flatten_that_leaves_an_exit_unconfirmed_is_not_reported_as_complete(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    await _halted(db, halted=False)

    class TimesOut(EmergencyBroker):
        async def place_market_order(self, ticker: str, quantity: Decimal, **kwargs: Any) -> Any:
            raise TimeoutError("no answer")

    broker = TimesOut(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])

    async def the_broker(_self: Any, _purpose: str) -> EmergencyBroker:
        return broker

    monkeypatch.setattr(SystemControlService, "_get_broker", the_broker)

    result = await emergency_route.emergency_flatten_all(
        current_user=SimpleNamespace(id=None, email="admin@example.test"), db=db
    )

    assert result.success is False, "an exit with an unknown outcome was reported as a success"
    assert "unknown outcome" in result.message
    assert "refused" not in result.message


@pytest.mark.asyncio
async def test_a_refusal_closes_the_flattens_own_intent_even_when_nothing_else_saved_it_first(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, demo: None
) -> None:
    """With no settings row the policy refuses without writing anything. Application
    sessions do not save pending rows before a query, so the record that this flatten
    created the intent has to be saved before it is looked for."""
    broker = EmergencyBroker(positions=[{"ticker": "AAA", "quantity": "2", "currentPrice": "10"}])
    service = await _service(db, monkeypatch, broker)

    with db.no_autoflush:
        summary = await service.flatten_all_summary(actor="test")
    await db.commit()

    assert summary.refused is True
    assert broker.market_orders == []
    (order,) = (await db.execute(select(Order))).scalars().all()
    assert order.status == "cancelled", "the flatten's own unsent exit was left as an intent"


# ── What Telegram records for a flatten ───────────────────────────────────────


async def _confirm_flatten(db: AsyncSession, summary: FlattenSummary) -> tuple[dict, str]:
    request = TelegramControlRequest(
        chat_id="chat",
        user_id="user",
        command="/flatten",
        action="flatten",
        confirmation_code="123456",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(request)
    await db.commit()
    service = TelegramControlService(db)

    async def the_summary(_actor: str) -> FlattenSummary:
        return summary

    service.system_control = SimpleNamespace(flatten_all_summary=the_summary)  # type: ignore[assignment]

    answer = await service._confirm(chat_id="chat", user_id="user", code="123456")
    return answer, request.status


@pytest.mark.asyncio
async def test_telegram_does_not_record_a_flatten_refused_before_anything_went_out_as_carried_out(
    db: AsyncSession,
) -> None:
    refused = FlattenSummary(
        message="Flatten refused: halted. No exit order was sent.",
        refused=True,
        complete=False,
        nothing_went_out=True,
    )

    answer, status = await _confirm_flatten(db, refused)

    assert status == "cancelled"
    assert "executed" not in answer
    assert answer["reply_text"] == refused.message


@pytest.mark.asyncio
async def test_telegram_records_a_flatten_refused_after_an_exit_went_out_as_carried_out(
    db: AsyncSession,
) -> None:
    """Something was done, and may be live at the broker; the reply still says refused."""
    part_way = FlattenSummary(
        message="Flatten refused: halted. 0 exit order(s) had been accepted by the broker "
        "before that. 1 exit order(s) have an unknown outcome and need reconciliation (AAA).",
        refused=True,
        complete=False,
        nothing_went_out=False,
    )

    answer, status = await _confirm_flatten(db, part_way)

    assert status == "executed"
    assert answer["executed"] is True
    assert answer["reply_text"] == part_way.message


@pytest.mark.asyncio
async def test_telegram_records_a_flatten_that_went_out_as_carried_out(db: AsyncSession) -> None:
    done = FlattenSummary(message="Flattened 1 positions.", refused=False, flattened=1)

    answer, status = await _confirm_flatten(db, done)

    assert status == "executed"
    assert answer["executed"] is True
