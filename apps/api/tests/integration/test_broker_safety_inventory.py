from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from scripts.t212_demo_multi_order_reconciliation_smoke import ReadOnlyBrokerGuard

from app.broker.protocols import BROKER_PROTOCOL_WRITE_METHODS, ReconciliationHistoryBrokerProtocol
from app.broker.safety import TRADING212_BROKER_WRITE_METHODS, is_broker_write_method
from app.broker.trading212 import Trading212Adapter
from app.core.config import settings
from app.db.models import Order
from app.services.demo_reconciliation_scheduler import DemoReconciliationScheduler
from app.services.demo_reconciliation_worker import (
    DemoReconciliationWorker,
    DemoReconciliationWorkerRunSummary,
)


def _demo_order(**overrides: Any) -> Order:
    values = {
        "id": uuid.uuid4(),
        "client_order_key": f"demo-safety-{uuid.uuid4()}",
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("1"),
        "status": "accepted",
        "broker_order_id": "48850886521",
        "execution_environment": "demo",
        "venue": "t212",
        "is_dry_run": False,
    }
    values.update(overrides)
    return Order(**values)


class InventoryGuardedBroker:
    environment = "demo"

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"items": []}
        self.history_calls: list[dict[str, Any]] = []
        self.write_calls: list[str] = []

    async def get_historical_orders(self, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append(kwargs)
        return self.response

    def __getattr__(self, name: str) -> Any:
        if is_broker_write_method(name):
            self.write_calls.append(name)
            raise AssertionError(f"reconciliation must not call broker write method: {name}")
        raise AttributeError(name)


class DelegatingBroker:
    environment = "demo"

    def __init__(self) -> None:
        self.history_calls: list[dict[str, Any]] = []

    async def get_historical_orders(self, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append(kwargs)
        return {"items": []}


@dataclass
class RecordingWorker:
    calls: list[str]

    async def run_once(self) -> DemoReconciliationWorkerRunSummary:
        self.calls.append("run_once")
        now = datetime.now(UTC)
        return DemoReconciliationWorkerRunSummary(
            run_id=uuid.uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            outcome="completed",
            worker_enabled=True,
            read_only_broker_calls=True,
            no_broker_order_sent=True,
            app_mode="demo",
            broker_environment="demo",
            live_trading_enabled=False,
            batch_size=10,
        )


@pytest.fixture(autouse=True)
def _demo_reconciliation_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_SCHEDULER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_BATCH_SIZE", 10)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_LOOKBACK_HOURS", 24)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_MAX_ATTEMPTS_PER_RUN", 10)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_HISTORY_LIMIT", 50)
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS", 120, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS", 300, raising=False
    )
    monkeypatch.setattr(
        settings, "DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS", 60, raising=False
    )


def test_trading212_write_inventory_covers_current_adapter_write_methods() -> None:
    public_async_methods = {
        name
        for name, member in inspect.getmembers(Trading212Adapter, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    adapter_write_methods = {
        name
        for name in public_async_methods
        if name.startswith(("place_", "cancel_", "modify_", "submit_"))
    }

    assert adapter_write_methods == {
        "cancel_order",
        "place_limit_order",
        "place_market_order",
        "place_stop_limit_order",
        "place_stop_order",
    }
    assert adapter_write_methods <= TRADING212_BROKER_WRITE_METHODS


def test_trading212_adapter_exposes_broker_protocol_methods() -> None:
    required_methods = {
        "get_historical_orders",
        "get_pending_orders",
        "get_order_by_id",
        "place_market_order",
        "place_limit_order",
        "place_stop_order",
        "place_stop_limit_order",
        "cancel_order",
    }

    missing = {
        name
        for name in required_methods
        if not inspect.iscoroutinefunction(getattr(Trading212Adapter, name, None))
    }

    assert missing == set()
    assert isinstance(getattr(Trading212Adapter, "environment", None), property) is False


def test_reconciliation_history_protocol_is_read_only_and_supported_by_trading212() -> None:
    assert ReconciliationHistoryBrokerProtocol.__protocol_attrs__ == {
        "environment",
        "get_historical_orders",
    }
    assert ReconciliationHistoryBrokerProtocol.__protocol_attrs__.isdisjoint(
        BROKER_PROTOCOL_WRITE_METHODS
    )
    assert inspect.iscoroutinefunction(getattr(Trading212Adapter, "get_historical_orders", None))
    assert "environment" in Trading212Adapter.__init__.__code__.co_varnames


def test_broker_protocol_write_methods_align_with_safety_inventory() -> None:
    assert BROKER_PROTOCOL_WRITE_METHODS <= TRADING212_BROKER_WRITE_METHODS


def test_trading212_write_inventory_includes_compatibility_write_names() -> None:
    assert {"modify_order", "place_order", "submit_order"} <= TRADING212_BROKER_WRITE_METHODS


def test_trading212_write_inventory_excludes_read_only_adapter_methods() -> None:
    assert (
        not {
            "get_historical_orders",
            "get_pending_orders",
            "get_order_by_id",
        }
        & TRADING212_BROKER_WRITE_METHODS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", sorted(TRADING212_BROKER_WRITE_METHODS))
async def test_multi_order_smoke_guard_blocks_every_inventoried_write_method(
    method_name: str,
) -> None:
    guard = ReadOnlyBrokerGuard(DelegatingBroker())

    with pytest.raises(RuntimeError, match=method_name):
        await getattr(guard, method_name)()

    assert guard.write_calls == [method_name]


@pytest.mark.asyncio
async def test_multi_order_smoke_guard_delegates_read_only_methods() -> None:
    broker = DelegatingBroker()
    guard = ReadOnlyBrokerGuard(broker)

    result = await guard.get_historical_orders(limit=25)

    assert result == {"items": []}
    assert broker.history_calls == [{"limit": 25}]
    assert guard.write_calls == []


@pytest.mark.asyncio
async def test_reconciliation_worker_uses_read_only_broker_path(db) -> None:
    order = _demo_order()
    db.add(order)
    await db.flush()
    broker = InventoryGuardedBroker(
        {"items": [{"id": "48850886521", "status": "FILLED", "filledQuantity": "1"}]}
    )

    summary = await DemoReconciliationWorker(db, broker).run_once()

    assert summary.no_broker_order_sent is True
    assert summary.read_only_broker_calls is True
    assert broker.history_calls == [{"limit": 50}]
    assert broker.write_calls == []


@pytest.mark.asyncio
async def test_reconciliation_scheduler_does_not_call_broker_write_methods(db) -> None:
    broker = InventoryGuardedBroker()
    worker = RecordingWorker(calls=[])
    scheduler = DemoReconciliationScheduler(
        db,
        broker,
        worker_factory=lambda *_args, **_kwargs: worker,
    )

    result = await scheduler.run_once()

    assert result.no_broker_order_sent is True
    assert result.read_only_broker_calls is True
    assert worker.calls == ["run_once"]
    assert broker.write_calls == []
