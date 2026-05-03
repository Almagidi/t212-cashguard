"""Unit tests for persisted worker heartbeat task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.broker.kraken import KrakenAdapter
from app.broker.trading212 import Trading212Adapter
from app.db.models import WorkerHeartbeat
from app.execution.engine import ExecutionEngine
from app.market_data.kraken_provider import KrakenMarketDataProvider
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers import tasks_heartbeat


@pytest.mark.asyncio
async def test_heartbeat_function_writes_and_updates_row(db):
    first = await tasks_heartbeat.record_worker_heartbeat(
        db,
        worker_name="worker-a",
        payload={"test": True},
    )
    second = await tasks_heartbeat.record_worker_heartbeat(
        db,
        worker_name="worker-a",
        payload={"test": "again"},
    )

    count = (await db.execute(select(func.count()).select_from(WorkerHeartbeat))).scalar_one()
    row = (await db.execute(select(WorkerHeartbeat))).scalar_one()
    assert count == 1
    assert first["component"] == "celery_worker"
    assert second["worker_name"] == "worker-a"
    assert row.status == "healthy"
    assert row.payload["source"] == "celery_beat"
    assert row.payload["interval_seconds"] == 60
    assert row.payload["test"] == "again"


def test_heartbeat_task_is_registered_on_sixty_second_cadence():
    from app.workers.celery_app import celery_app

    cfg = celery_app.conf.beat_schedule["worker-heartbeat"]
    assert cfg["task"] == "app.workers.tasks_heartbeat.record_worker_heartbeat_task"
    assert cfg["schedule"] == 60.0


@pytest.mark.asyncio
async def test_heartbeat_function_calls_no_broker_provider_execution_or_planner_paths(
    db,
    monkeypatch,
):
    create_order_intent = MagicMock()
    submit_order = AsyncMock()
    kraken_test_connection = AsyncMock()
    kraken_place_market_order = AsyncMock()
    t212_test_connection = AsyncMock()
    t212_place_market_order = AsyncMock()
    provider_get_quote = AsyncMock()
    provider_get_bars = AsyncMock()
    evaluate_plan = MagicMock()

    monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
    monkeypatch.setattr(ExecutionEngine, "submit_order", submit_order)
    monkeypatch.setattr(KrakenAdapter, "test_connection", kraken_test_connection)
    monkeypatch.setattr(KrakenAdapter, "place_market_order", kraken_place_market_order)
    monkeypatch.setattr(Trading212Adapter, "test_connection", t212_test_connection)
    monkeypatch.setattr(Trading212Adapter, "place_market_order", t212_place_market_order)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_quote", provider_get_quote)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_bars", provider_get_bars)
    monkeypatch.setattr(KrakenDCAPlanner, "evaluate_plan", evaluate_plan)

    await tasks_heartbeat.record_worker_heartbeat(db, worker_name="worker-a")

    create_order_intent.assert_not_called()
    submit_order.assert_not_called()
    kraken_test_connection.assert_not_called()
    kraken_place_market_order.assert_not_called()
    t212_test_connection.assert_not_called()
    t212_place_market_order.assert_not_called()
    provider_get_quote.assert_not_called()
    provider_get_bars.assert_not_called()
    evaluate_plan.assert_not_called()
