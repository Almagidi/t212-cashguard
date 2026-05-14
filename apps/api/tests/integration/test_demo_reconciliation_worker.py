from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.deps import get_broker
from app.broker.trading212 import T212RateLimitError
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order
from app.main import app
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.services.safety_policy import SafetyPolicyViolation


def _demo_order(**overrides) -> Order:
    values = {
        "id": uuid.uuid4(),
        "client_order_key": f"demo-worker-{uuid.uuid4()}",
        "ticker": "AAPL",
        "side": "buy",
        "order_type": "market",
        "quantity": Decimal("1"),
        "status": "accepted",
        "broker_order_id": str(uuid.uuid4().int)[:11],
        "execution_environment": "demo",
        "venue": "t212",
        "is_dry_run": False,
        "created_at": datetime.now(UTC) - timedelta(minutes=5),
    }
    values.update(overrides)
    return Order(**values)


class WorkerBroker:
    environment = "demo"

    def __init__(self, responses=None, exc: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.exc = exc
        self.history_calls: list[dict[str, object]] = []
        self.placement_calls = 0
        self.cancel_calls = 0

    async def get_historical_orders(self, **kwargs):
        self.history_calls.append(kwargs)
        if self.exc:
            raise self.exc
        if self.responses:
            return self.responses.pop(0)
        return {"items": []}

    async def place_market_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.placement_calls += 1
        raise AssertionError("worker must not place broker orders")

    async def cancel_order(self, *args, **kwargs):  # pragma: no cover - safety sentinel
        self.cancel_calls += 1
        raise AssertionError("worker must not cancel broker orders")


async def _actions(db) -> list[str]:
    return [
        audit.action
        for audit in (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
    ]


@pytest.fixture(autouse=True)
def _demo_safety(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_BATCH_SIZE", 10)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_LOOKBACK_HOURS", 24)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_MAX_ATTEMPTS_PER_RUN", 10)
    monkeypatch.setattr(settings, "DEMO_RECONCILIATION_HISTORY_LIMIT", 50)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("APP_MODE", "mock"),
        ("T212_ENVIRONMENT", "live"),
        ("LIVE_TRADING_ENABLED", True),
    ],
)
async def test_worker_refuses_unsafe_global_boundaries(
    db,
    monkeypatch,
    setting_name,
    unsafe_value,
):
    monkeypatch.setattr(settings, setting_name, unsafe_value)
    broker = WorkerBroker()

    with pytest.raises(SafetyPolicyViolation):
        await DemoReconciliationWorker(db, broker).run_once()

    assert broker.history_calls == []
    assert broker.placement_calls == 0


@pytest.mark.asyncio
async def test_worker_refuses_non_demo_broker_environment(db):
    broker = WorkerBroker()
    broker.environment = "live"

    with pytest.raises(SafetyPolicyViolation):
        await DemoReconciliationWorker(db, broker).run_once()

    assert broker.history_calls == []


@pytest.mark.asyncio
async def test_selects_only_eligible_due_demo_broker_orders(db):
    now = datetime.now(UTC)
    eligible = _demo_order(status="accepted", created_at=now - timedelta(minutes=10))
    rows = [
        eligible,
        _demo_order(status="filled"),
        _demo_order(execution_environment="live"),
        _demo_order(execution_environment="paper", is_dry_run=True),
        _demo_order(is_dry_run=True),
        _demo_order(broker_order_id=None),
        _demo_order(last_reconciled_at=now - timedelta(seconds=10)),
        _demo_order(created_at=now - timedelta(hours=30)),
    ]
    db.add_all(rows)
    await db.flush()

    candidates = await DemoReconciliationWorker(
        db, WorkerBroker()
    ).select_reconciliation_candidates()

    assert [candidate.id for candidate in candidates] == [eligible.id]


@pytest.mark.asyncio
async def test_run_once_reconciles_candidates_and_writes_batch_audits(db):
    first = _demo_order(broker_order_id="48850886521")
    second = _demo_order(broker_order_id="48850886522")
    db.add_all([first, second])
    await db.flush()
    broker = WorkerBroker(
        responses=[
            {"items": [{"id": "48850886521", "status": "FILLED", "filledQuantity": "1"}]},
            {"items": [{"id": "48850886522", "status": "WORKING"}]},
        ]
    )

    summary = await DemoReconciliationWorker(db, broker, actor="test-worker").run_once()

    assert summary.outcome == "completed"
    assert summary.candidates_found == 2
    assert summary.attempted == 2
    assert summary.succeeded == 2
    assert summary.updated_order_ids == [first.id, second.id]
    assert first.status == "filled"
    assert second.status == "accepted"
    assert broker.history_calls == [{"limit": 50}, {"limit": 50}]
    assert broker.placement_calls == 0
    actions = await _actions(db)
    assert "demo_reconciliation_worker_started" in actions
    assert "demo_reconciliation_worker_completed" in actions


@pytest.mark.asyncio
async def test_missing_broker_history_is_reported_without_failing_local_order(db):
    order = _demo_order(status="accepted", broker_order_id="missing")
    db.add(order)
    await db.flush()

    summary = await DemoReconciliationWorker(
        db,
        WorkerBroker(responses=[{"items": [{"id": "other", "status": "FILLED"}]}]),
    ).run_once()

    assert summary.outcome == "completed"
    assert summary.missing == 1
    assert summary.failed == 0
    assert order.status == "accepted"


@pytest.mark.asyncio
async def test_rate_limit_stops_batch_without_marking_orders_failed(db):
    first = _demo_order(broker_order_id="first")
    second = _demo_order(broker_order_id="second")
    db.add_all([first, second])
    await db.flush()

    summary = await DemoReconciliationWorker(
        db,
        WorkerBroker(exc=T212RateLimitError(7.0)),
    ).run_once()

    assert summary.outcome == "rate_limited"
    assert summary.attempted == 1
    assert summary.rate_limited == 1
    assert summary.failed == 0
    assert summary.rate_limited_order_ids == [first.id]
    assert first.status == "accepted"
    assert second.status == "accepted"
    assert "demo_reconciliation_worker_rate_limited" in await _actions(db)


@pytest.mark.asyncio
async def test_worker_status_reads_safe_config_and_latest_summary(db):
    db.add(AppSettings(id=1, extra={}))
    await db.flush()
    order = _demo_order(broker_order_id="48850886521")
    db.add(order)
    await db.flush()

    worker = DemoReconciliationWorker(
        db,
        WorkerBroker(responses=[{"items": [{"id": "48850886521", "status": "FILLED"}]}]),
    )
    summary = await worker.run_once()
    status = await worker.get_worker_status()

    assert status.enabled is True
    assert status.app_mode == "demo"
    assert status.broker_environment == "demo"
    assert status.live_trading_enabled is False
    assert status.batch_size == 10
    assert status.last_run_summary is not None
    assert status.last_run_summary["run_id"] == str(summary.run_id)
    assert status.safety_state == "safe"


@pytest.mark.asyncio
async def test_reconciliation_status_endpoint_returns_safe_worker_config(
    client,
    auth_headers,
):
    response = await client.get(
        "/v1/broker/trading212/reconciliation/status",
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["app_mode"] == "demo"
    assert data["broker_environment"] == "demo"
    assert data["live_trading_enabled"] is False
    assert data["batch_size"] == 10
    assert data["safety_state"] == "safe"


@pytest.mark.asyncio
async def test_run_once_endpoint_reconciles_with_admin_auth_and_preserves_safety(
    client,
    auth_headers,
    db,
):
    order = _demo_order(broker_order_id="48850886521")
    db.add(order)
    await db.flush()
    broker = WorkerBroker(responses=[{"items": [{"id": "48850886521", "status": "FILLED"}]}])

    app.dependency_overrides[get_broker] = lambda: broker
    try:
        response = await client.post(
            "/v1/broker/trading212/reconciliation/run-once",
            headers=auth_headers,
        )
    finally:
        app.dependency_overrides.pop(get_broker, None)

    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "completed"
    assert data["attempted"] == 1
    assert data["succeeded"] == 1
    assert data["no_broker_order_sent"] is True
    assert data["read_only_broker_calls"] is True
    assert data["live_trading_enabled"] is False
    assert broker.placement_calls == 0
    assert order.status == "filled"
