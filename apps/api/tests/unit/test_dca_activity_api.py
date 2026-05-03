"""API tests for the read-only Kraken DCA paper activity report."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.broker.kraken import KrakenAdapter
from app.db.models import AuditLog, DcaConfig, DcaPlanState, Order
from app.db.seed import seed_dca_configs
from app.execution.engine import ExecutionEngine
from app.market_data.kraken_provider import KrakenMarketDataProvider
from app.services.strategy_runner import StrategyRunner
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers import tasks_dca

BASE_TIME = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


def _decision(
    *,
    ticker: str = "BTC/USD",
    venue: str = "kraken",
    code: str | None = "BUY_DUE",
    reason: str | None = "Scheduled accumulation",
    occurred_at: datetime = BASE_TIME,
    extra_payload: dict | None = None,
) -> AuditLog:
    payload = {
        "ticker": ticker,
        "venue": venue,
        "paper_only": True,
        "amount_usd": "100.00000000",
        "mode": "scheduled",
        "next_scheduled_date": "2026-05-07",
        "evaluated_on": "2026-04-30",
    }
    if code is not None:
        payload["decision_code"] = code
    if reason is not None:
        payload["reason"] = reason
    if extra_payload:
        payload.update(extra_payload)
    return AuditLog(
        action="dca_paper_decision",
        entity_type="dca_plan_state",
        entity_id=f"{venue}:{ticker}",
        actor="worker:dca_scheduler",
        payload=payload,
        occurred_at=occurred_at,
    )


@pytest.mark.asyncio
async def test_dca_activity_reports_configs_states_decisions_and_slash_tickers(
    client,
    auth_headers,
    db,
):
    await seed_dca_configs(db)
    eth_config = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "ETH/USD"))
    ).scalar_one()
    eth_config.enabled = True
    db.add_all(
        [
            DcaPlanState(
                ticker="BTC/USD",
                venue="kraken",
                last_buy_at=date(2026, 4, 23),
                last_decision_at=date(2026, 4, 30),
                total_allocated_usd=Decimal("300.00000000"),
                executions_count=3,
                last_decision_code="BUY_DUE",
                last_reason="Persisted state",
            ),
            DcaPlanState(
                ticker="ETH/USD",
                venue="kraken",
                last_buy_at=None,
                last_decision_at=date(2026, 4, 30),
                total_allocated_usd=Decimal("0.00000000"),
                executions_count=0,
                last_decision_code="BLOCKED_LOW_CASH",
                last_reason="Cash below reserve",
            ),
            _decision(
                ticker="BTC/USD",
                code="BUY_DUE",
                reason="Scheduled accumulation",
                occurred_at=BASE_TIME,
            ),
            _decision(
                ticker="ETH/USD",
                code="BLOCKED_LOW_CASH",
                reason="Cash below reserve",
                occurred_at=BASE_TIME - timedelta(minutes=1),
            ),
            _decision(
                ticker="BTC/USD",
                code="SKIP_ALREADY_BOUGHT_THIS_WINDOW",
                reason="Already bought this window",
                occurred_at=BASE_TIME - timedelta(minutes=2),
            ),
            _decision(
                ticker="ETH/USD",
                code=None,
                reason=None,
                occurred_at=BASE_TIME - timedelta(minutes=3),
                extra_payload={"malformed": True},
            ),
        ]
    )
    await db.commit()

    response = await client.get(
        "/v1/kraken/dca/activity?audit_limit=3",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subsystem"] == "kraken_dca"
    assert body["mode"] == "paper_only"
    assert body["runnable"] is False
    assert body["live_enabled"] is False
    assert body["config_count"] == 2
    assert body["enabled_config_count"] == 1
    assert body["decision_count_total"] == 4
    assert body["decision_counts_by_code"] == {
        "BUY_DUE": 1,
        "BLOCKED_LOW_CASH": 1,
        "SKIP_ALREADY_BOUGHT_THIS_WINDOW": 1,
    }
    assert body["buy_due_count"] == 1
    assert body["blocked_count"] == 1
    assert body["skipped_count"] == 1
    assert body["total_paper_allocated_usd"] == "300.00000000"
    assert body["order_count_sanity"] == 0

    assert [config["ticker"] for config in body["configs"]] == ["BTC/USD", "ETH/USD"]
    assert all("BTCUSD" not in config["ticker"] for config in body["configs"])
    assert body["configs"][0]["fixed_cash_amount"] == "100.00000000"
    assert body["configs"][0]["max_position_percent"] == "25.0000"

    by_ticker = {item["ticker"]: item for item in body["per_ticker_activity"]}
    assert set(by_ticker) == {"BTC/USD", "ETH/USD"}
    assert by_ticker["BTC/USD"]["venue"] == "kraken"
    assert by_ticker["BTC/USD"]["enabled"] is False
    assert by_ticker["BTC/USD"]["latest_decision_code"] == "BUY_DUE"
    assert by_ticker["BTC/USD"]["latest_reason"] == "Scheduled accumulation"
    assert by_ticker["BTC/USD"]["total_allocated_usd"] == "300.00000000"
    assert by_ticker["BTC/USD"]["executions_count"] == 3
    assert by_ticker["BTC/USD"]["last_buy_at"] == "2026-04-23"
    assert by_ticker["BTC/USD"]["decision_counts_by_code"] == {
        "BUY_DUE": 1,
        "SKIP_ALREADY_BOUGHT_THIS_WINDOW": 1,
    }
    assert by_ticker["ETH/USD"]["enabled"] is True
    assert by_ticker["ETH/USD"]["latest_decision_code"] == "BLOCKED_LOW_CASH"

    recent = body["recent_decisions"]
    assert len(recent) == 3
    assert [item["decision_code"] for item in recent] == [
        "BUY_DUE",
        "BLOCKED_LOW_CASH",
        "SKIP_ALREADY_BOUGHT_THIS_WINDOW",
    ]
    assert recent[0]["ticker"] == "BTC/USD"
    assert recent[0]["payload_summary"] == {
        "paper_only": True,
        "amount_usd": "100.00000000",
        "mode": "scheduled",
        "next_scheduled_date": "2026-05-07",
        "evaluated_on": "2026-04-30",
    }

    assert body["safety_flags"] == {
        "dca_planner_runnable_is_false": True,
        "dca_planner_paper_only_is_true": True,
        "main_runner_registered": False,
        "order_creation_supported": False,
        "execution_called_by_report": False,
        "provider_called_by_report": False,
        "scheduler_triggered_by_report": False,
    }


@pytest.mark.asyncio
async def test_dca_activity_handles_partial_audit_payload_and_bounds_limit(
    client,
    auth_headers,
    db,
):
    db.add(
        AuditLog(
            action="dca_paper_decision",
            entity_type="dca_plan_state",
            entity_id="partial",
            actor="worker:dca_scheduler",
            payload={"ticker": "BTC/USD"},
            occurred_at=BASE_TIME,
        )
    )
    await db.commit()

    response = await client.get("/v1/kraken/dca/activity", headers=auth_headers)
    too_large = await client.get(
        "/v1/kraken/dca/activity?audit_limit=101",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision_count_total"] == 1
    assert body["decision_counts_by_code"] == {}
    assert body["recent_decisions"][0]["ticker"] == "BTC/USD"
    assert body["recent_decisions"][0]["venue"] is None
    assert body["recent_decisions"][0]["decision_code"] is None
    assert too_large.status_code == 422


@pytest.mark.asyncio
async def test_dca_activity_is_read_only_and_calls_no_side_effect_paths(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    await seed_dca_configs(db)
    db.add(
        DcaPlanState(
            ticker="BTC/USD",
            venue="kraken",
            last_decision_at=date(2026, 4, 30),
            total_allocated_usd=Decimal("100.00000000"),
            executions_count=1,
            last_decision_code="BUY_DUE",
        )
    )
    await db.commit()

    create_order_intent = MagicMock()
    provider_get_quote = AsyncMock()
    provider_get_bars = AsyncMock()
    broker_test_connection = AsyncMock()
    broker_place_market_order = AsyncMock()
    task_delay = MagicMock()
    task_apply_async = MagicMock()
    evaluate_plan = MagicMock()
    monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_quote", provider_get_quote)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_bars", provider_get_bars)
    monkeypatch.setattr(KrakenAdapter, "test_connection", broker_test_connection)
    monkeypatch.setattr(KrakenAdapter, "place_market_order", broker_place_market_order)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "delay", task_delay)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "apply_async", task_apply_async)
    monkeypatch.setattr(KrakenDCAPlanner, "evaluate_plan", evaluate_plan)

    before_orders = await _count(db, Order)
    before_audits = await _count(db, AuditLog)
    config_before = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()
    state_before = (
        await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
    ).scalar_one()

    response = await client.get("/v1/kraken/dca/activity", headers=auth_headers)

    config_after = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()
    state_after = (
        await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
    ).scalar_one()
    assert response.status_code == 200
    assert await _count(db, Order) == before_orders == 0
    assert await _count(db, AuditLog) == before_audits
    assert config_after.enabled == config_before.enabled
    assert config_after.paper_only == config_before.paper_only
    assert config_after.fixed_cash_amount == config_before.fixed_cash_amount
    assert state_after.total_allocated_usd == state_before.total_allocated_usd
    assert state_after.executions_count == state_before.executions_count
    assert state_after.last_decision_code == state_before.last_decision_code

    create_order_intent.assert_not_called()
    provider_get_quote.assert_not_called()
    provider_get_bars.assert_not_called()
    broker_test_connection.assert_not_called()
    broker_place_market_order.assert_not_called()
    task_delay.assert_not_called()
    task_apply_async.assert_not_called()
    evaluate_plan.assert_not_called()

    runner = StrategyRunner(MagicMock())
    strategy = MagicMock()
    strategy.type = "kraken_dca"
    strategy.params = {}
    assert runner._make_engine(strategy) is None


@pytest.mark.asyncio
async def test_dca_activity_has_no_mutation_methods(client, auth_headers):
    for method in (client.post, client.patch, client.put, client.delete):
        response = await method("/v1/kraken/dca/activity", headers=auth_headers)
        assert response.status_code == 405
