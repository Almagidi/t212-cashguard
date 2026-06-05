"""Integration coverage for emergency system-control operations."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order, OrderEvent
from app.services.system_control import SystemControlService


def _order(**overrides: Any) -> Order:
    values = {
        "id": uuid.uuid4(),
        "client_order_key": f"emergency-test-{uuid.uuid4()}",
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("1"),
        "status": "pending_intent",
        "execution_environment": "demo",
        "is_dry_run": False,
    }
    values.update(overrides)
    return Order(**values)


class EmergencyBroker:
    environment = "demo"

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self.positions = positions or []
        self.cancelled_order_ids: list[str] = []
        self.market_orders: list[dict[str, Any]] = []
        self.account_summary_calls = 0

    async def __aenter__(self) -> EmergencyBroker:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"EmergencyBroker: unexpected method call '{name}'")

    async def get_positions(self) -> list[dict[str, Any]]:
        return self.positions

    async def get_account_summary(self) -> dict[str, Any]:  # pragma: no cover - safety sentinel
        self.account_summary_calls += 1
        raise AssertionError("emergency operations must not read account summary")

    async def cancel_order(self, broker_order_id: str) -> dict[str, str]:
        self.cancelled_order_ids.append(broker_order_id)
        return {"id": broker_order_id, "status": "CANCELLED"}

    async def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        payload = {
            "ticker": ticker,
            "quantity": quantity,
            "time_validity": time_validity,
        }
        self.market_orders.append(payload)
        return {
            "id": f"flatten-{ticker}",
            "status": "WORKING",
            "filledQuantity": 0,
            "filledPrice": 0,
        }


async def _use_fake_broker(
    monkeypatch: pytest.MonkeyPatch,
    service: SystemControlService,
    broker: EmergencyBroker,
) -> list[str]:
    provider_calls: list[str] = []

    async def fake_get_broker(purpose: str) -> EmergencyBroker:
        provider_calls.append(purpose)
        return broker

    monkeypatch.setattr(service, "_get_broker", fake_get_broker)
    return provider_calls


async def _audit_logs(db, action: str) -> list[AuditLog]:
    return (
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_cancel_all_pending_cancels_real_pending_orders_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    pending_intent = _order(ticker="CANCEL_INTENT", status="pending_intent")
    submitted = _order(
        ticker="CANCEL_SUBMITTED",
        status="submitted",
        broker_order_id="broker-submitted",
    )
    accepted = _order(
        ticker="CANCEL_ACCEPTED",
        status="accepted",
        broker_order_id="broker-accepted",
    )
    filled = _order(
        ticker="KEEP_FILLED",
        status="filled",
        broker_order_id="broker-filled",
        filled_quantity=Decimal("1"),
    )
    db.add_all([pending_intent, submitted, accepted, filled])
    await db.commit()

    broker = EmergencyBroker()
    service = SystemControlService(db)
    provider_calls = await _use_fake_broker(monkeypatch, service, broker)

    message = await service.cancel_all_pending(actor="integration-test")
    await db.commit()

    assert message == "Cancelled 3 pending orders."
    assert provider_calls == ["operator_system_control_emergency"]
    assert set(broker.cancelled_order_ids) == {"broker-submitted", "broker-accepted"}
    assert broker.market_orders == []
    assert broker.account_summary_calls == 0

    statuses = {
        order.ticker: order.status for order in (await db.execute(select(Order))).scalars().all()
    }
    assert statuses == {
        "CANCEL_INTENT": "cancelled",
        "CANCEL_SUBMITTED": "cancelled",
        "CANCEL_ACCEPTED": "cancelled",
        "KEEP_FILLED": "filled",
    }

    cancelled_events = (
        (
            await db.execute(
                select(OrderEvent)
                .where(OrderEvent.event_type == "cancelled")
                .order_by(OrderEvent.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    assert {event.order_id for event in cancelled_events} == {
        pending_intent.id,
        submitted.id,
        accepted.id,
    }

    audits = await _audit_logs(db, "emergency_cancel_all")
    assert len(audits) == 1
    assert audits[0].actor == "integration-test"
    assert audits[0].payload == {"source": "system_control", "cancelled_count": 3}


@pytest.mark.asyncio
async def test_flatten_all_submits_market_sells_for_long_positions_and_audits(db, monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)

    db.add(
        AppSettings(
            id=1,
            auto_trading_enabled=False,
            kill_switch_active=False,
            live_trading_unlocked=False,
        )
    )
    await db.commit()

    broker = EmergencyBroker(
        positions=[
            {"ticker": "LONG_AAPL", "quantity": "2", "currentPrice": "170.25"},
            {"ticker": "ZERO_MSFT", "quantity": "0", "currentPrice": "300"},
            {"ticker": "SHORT_TSLA", "quantity": "-1", "currentPrice": "250"},
        ]
    )
    service = SystemControlService(db)
    provider_calls = await _use_fake_broker(monkeypatch, service, broker)

    message = await service.flatten_all(actor="integration-test")
    await db.commit()

    assert message == "Flattened 1 positions."
    assert provider_calls == ["operator_system_control_emergency"]
    assert broker.cancelled_order_ids == []
    assert broker.market_orders == [
        {
            "ticker": "LONG_AAPL",
            "quantity": Decimal("-2.00000000"),
            "time_validity": "DAY",
        }
    ]
    assert broker.account_summary_calls == 0

    orders = (await db.execute(select(Order).order_by(Order.created_at))).scalars().all()
    assert len(orders) == 1
    order = orders[0]
    assert order.ticker == "LONG_AAPL"
    assert order.side == "sell"
    assert order.order_type == "market"
    assert order.quantity == Decimal("2.00000000")
    assert order.status == "accepted"
    assert order.is_dry_run is False
    assert order.execution_environment == "demo"
    assert order.expected_fill_price == Decimal("170.25000000")
    assert order.broker_order_id == "flatten-LONG_AAPL"
    assert order.broker_request == {"ticker": "LONG_AAPL", "quantity": -2.0, "timeValidity": "DAY"}

    actions = [
        event.event_type
        for event in (
            await db.execute(
                select(OrderEvent)
                .where(OrderEvent.order_id == order.id)
                .order_by(OrderEvent.occurred_at)
            )
        ).scalars()
    ]
    assert actions == ["intent_created", "submitted", "broker_accepted"]

    audits = await _audit_logs(db, "emergency_flatten_all")
    assert len(audits) == 1
    assert audits[0].actor == "integration-test"
    assert audits[0].payload == {"source": "system_control", "flattened": 1}

    broker_audits = (await db.execute(select(AuditLog))).scalars().all()
    assert {audit.action for audit in broker_audits} >= {
        "demo_broker_order_attempt",
        "demo_broker_order_success",
        "emergency_flatten_all",
    }
