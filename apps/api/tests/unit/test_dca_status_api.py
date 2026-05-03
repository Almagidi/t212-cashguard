"""API tests for the read-only Kraken DCA operator status surface."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from app.broker.kraken import KrakenAdapter
from app.db.models import AuditLog, DcaPlanState, Order
from app.db.seed import seed_dca_configs
from app.execution.engine import ExecutionEngine
from app.market_data.kraken_provider import KrakenMarketDataProvider


@pytest.mark.asyncio
async def test_dca_status_lists_configs_state_audits_and_safety_flags(
    client,
    auth_headers,
    db,
):
    await seed_dca_configs(db)
    db.add(
        DcaPlanState(
            ticker="BTC/USD",
            venue="kraken",
            last_buy_at=date(2026, 4, 22),
            last_decision_at=date(2026, 4, 29),
            total_allocated_usd=Decimal("100.00000000"),
            executions_count=1,
            last_decision_code="BUY_DUE",
            last_reason="Scheduled accumulation",
        )
    )
    db.add(
        AuditLog(
            action="dca_paper_decision",
            entity_type="dca_plan_state",
            entity_id="kraken:BTC/USD",
            actor="worker:dca_scheduler",
            payload={
                "ticker": "BTC/USD",
                "venue": "kraken",
                "paper_only": True,
                "decision_code": "BUY_DUE",
            },
            occurred_at=datetime.now(UTC),
        )
    )
    db.add(
        AuditLog(
            action="strategy_enabled",
            entity_type="strategy",
            actor="test",
            payload={"ignored": True},
            occurred_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db.commit()

    response = await client.get("/v1/kraken/dca/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["subsystem"] == "kraken_dca"
    assert body["mode"] == "paper_only"
    assert body["runnable"] is False
    assert body["live_enabled"] is False
    assert body["scheduler_registered"] is True
    assert body["scheduler_cadence"] == "daily at 01:00 UTC"
    assert body["config_count"] == 2
    assert body["enabled_config_count"] == 0

    configs = {config["ticker"]: config for config in body["configs"]}
    assert set(configs) == {"BTC/USD", "ETH/USD"}
    btc = configs["BTC/USD"]
    assert btc["venue"] == "kraken"
    assert btc["enabled"] is False
    assert btc["paper_only"] is True
    assert btc["cadence_days"] == 7
    assert btc["fixed_cash_amount"] == "100.00000000"
    assert btc["min_cash_reserve"] == "500.00000000"
    assert btc["max_position_percent"] == "25.0000"
    assert btc["dip_buy_enabled"] is True
    assert btc["dip_buy_multiplier"] == "2.0000"
    assert btc["latest_state"] == {
        "last_buy_at": "2026-04-22",
        "last_decision_at": "2026-04-29",
        "total_allocated_usd": "100.00000000",
        "executions_count": 1,
        "last_decision_code": "BUY_DUE",
        "last_reason": "Scheduled accumulation",
    }
    assert configs["ETH/USD"]["latest_state"] is None

    assert len(body["recent_audit_entries"]) == 1
    audit = body["recent_audit_entries"][0]
    assert audit["action"] == "dca_paper_decision"
    assert audit["entity_id"] == "kraken:BTC/USD"
    assert audit["metadata"]["ticker"] == "BTC/USD"
    assert audit["metadata"]["paper_only"] is True

    assert body["safety_flags"] == {
        "dca_planner_runnable_is_false": True,
        "dca_planner_paper_only_is_true": True,
        "main_runner_registered": False,
        "order_creation_supported": False,
    }


@pytest.mark.asyncio
async def test_dca_status_is_read_only_and_calls_no_execution_or_kraken_paths(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    await seed_dca_configs(db)
    await db.commit()

    create_order_intent = MagicMock()
    provider_get_quote = MagicMock()
    broker_test_connection = MagicMock()
    monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_quote", provider_get_quote)
    monkeypatch.setattr(KrakenAdapter, "test_connection", broker_test_connection)

    before_orders = (await db.execute(select(func.count()).select_from(Order))).scalar_one()

    response = await client.get("/v1/kraken/dca/status", headers=auth_headers)

    after_orders = (await db.execute(select(func.count()).select_from(Order))).scalar_one()
    assert response.status_code == 200
    assert before_orders == 0
    assert after_orders == 0
    create_order_intent.assert_not_called()
    provider_get_quote.assert_not_called()
    broker_test_connection.assert_not_called()


@pytest.mark.asyncio
async def test_dca_status_has_no_mutation_paths(client, auth_headers):
    for method in (client.post, client.patch, client.put, client.delete):
        response = await method("/v1/kraken/dca/status", headers=auth_headers)
        assert response.status_code == 405

    for path in (
        "/v1/kraken/dca/enable",
        "/v1/kraken/dca/disable",
        "/v1/kraken/dca/execute",
    ):
        response = await client.post(path, headers=auth_headers)
        assert response.status_code == 404
