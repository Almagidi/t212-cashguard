"""API tests for paper-only Kraken DCA config management."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.broker.kraken import KrakenAdapter
from app.db.models import AuditLog, DcaConfig, Order
from app.db.seed import seed_dca_configs
from app.execution.engine import ExecutionEngine
from app.market_data.kraken_provider import KrakenMarketDataProvider
from app.services.strategy_runner import StrategyRunner
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers import tasks_dca


def _payload(**overrides):
    payload = {
        "ticker": "BTC/USD",
        "venue": "kraken",
        "cadence_days": 7,
        "fixed_cash_amount": "100.00000000",
        "dip_buy_enabled": True,
        "dip_buy_multiplier": "2.0000",
        "min_cash_reserve": "500.00000000",
        "max_position_percent": "25.0000",
    }
    payload.update(overrides)
    return payload


async def _order_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(Order))).scalar_one()


async def _audit_actions(db) -> list[str]:
    result = await db.execute(select(AuditLog.action).order_by(AuditLog.occurred_at))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_list_configs_returns_seeded_slash_tickers(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()

    response = await client.get("/v1/kraken/dca/configs", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert [config["ticker"] for config in body] == ["BTC/USD", "ETH/USD"]
    assert all(config["venue"] == "kraken" for config in body)
    assert all(config["paper_only"] is True for config in body)
    assert all(config["enabled"] is False for config in body)


@pytest.mark.asyncio
async def test_get_config_by_id_and_missing_returns_404(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()
    config = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()

    response = await client.get(f"/v1/kraken/dca/configs/{config.id}", headers=auth_headers)
    missing = await client.get(f"/v1/kraken/dca/configs/{uuid.uuid4()}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["id"] == str(config.id)
    assert response.json()["ticker"] == "BTC/USD"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_create_config_forces_paper_only_disabled_and_audits(client, auth_headers, db):
    before_orders = await _order_count(db)

    response = await client.post(
        "/v1/kraken/dca/configs",
        headers=auth_headers,
        json=_payload(enabled=False, paper_only=True),
    )

    after_orders = await _order_count(db)
    assert response.status_code == 201
    body = response.json()
    assert body["ticker"] == "BTC/USD"
    assert body["venue"] == "kraken"
    assert body["paper_only"] is True
    assert body["enabled"] is False
    assert before_orders == 0
    assert after_orders == 0

    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "dca_config_created"))
    ).scalar_one()
    assert audit.entity_type == "dca_config"
    assert audit.entity_id == body["id"]
    assert audit.payload["ticker"] == "BTC/USD"
    assert audit.payload["venue"] == "kraken"
    assert audit.payload["paper_only"] is True


@pytest.mark.asyncio
async def test_create_rejects_unsupported_ticker_no_slash_ticker_bad_venue_and_live_flags(
    client,
    auth_headers,
):
    cases = [
        _payload(ticker="SOL/USD"),
        _payload(ticker="BTCUSD"),
        _payload(venue="t212"),
        _payload(paper_only=False),
        _payload(enabled=True),
        _payload(max_position_percent="50.0001"),
    ]

    for payload in cases:
        response = await client.post(
            "/v1/kraken/dca/configs",
            headers=auth_headers,
            json=payload,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_duplicate_ticker_venue_returns_conflict(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()

    response = await client.post(
        "/v1/kraken/dca/configs",
        headers=auth_headers,
        json=_payload(ticker="BTC/USD"),
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_patch_safe_fields_audits_and_does_not_create_orders(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()
    config = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()
    before_orders = await _order_count(db)

    response = await client.patch(
        f"/v1/kraken/dca/configs/{config.id}",
        headers=auth_headers,
        json={
            "cadence_days": 14,
            "fixed_cash_amount": "125.00000000",
            "dip_buy_enabled": False,
            "dip_buy_multiplier": "1.5000",
            "min_cash_reserve": "600.00000000",
            "max_position_percent": "10.0000",
        },
    )

    after_orders = await _order_count(db)
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "BTC/USD"
    assert body["enabled"] is False
    assert body["cadence_days"] == 14
    assert body["fixed_cash_amount"] == "125.00000000"
    assert body["dip_buy_enabled"] is False
    assert body["dip_buy_multiplier"] == "1.5000"
    assert body["min_cash_reserve"] == "600.00000000"
    assert body["max_position_percent"] == "10.0000"
    assert before_orders == 0
    assert after_orders == 0

    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "dca_config_updated"))
    ).scalar_one()
    assert audit.payload["config_id"] == str(config.id)
    assert audit.payload["changed_fields"] == [
        "cadence_days",
        "dip_buy_enabled",
        "dip_buy_multiplier",
        "fixed_cash_amount",
        "max_position_percent",
        "min_cash_reserve",
    ]


@pytest.mark.asyncio
async def test_patch_rejects_paper_only_ticker_and_venue_mutation(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()
    config = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()

    for payload in (
        {"paper_only": False},
        {"ticker": "ETH/USD"},
        {"venue": "t212"},
        {"enabled": True},
        {"enabled": False},
        {"max_position_percent": "0"},
    ):
        response = await client.patch(
            f"/v1/kraken/dca/configs/{config.id}",
            headers=auth_headers,
            json=payload,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_enable_disable_set_state_and_audit(client, auth_headers, db):
    await seed_dca_configs(db)
    await db.commit()
    config = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "ETH/USD"))
    ).scalar_one()

    enabled = await client.post(
        f"/v1/kraken/dca/configs/{config.id}/enable",
        headers=auth_headers,
    )
    disabled = await client.post(
        f"/v1/kraken/dca/configs/{config.id}/disable",
        headers=auth_headers,
    )

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["paper_only"] is True
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert await _audit_actions(db) == [
        "login_success",
        "dca_config_enabled",
        "dca_config_disabled",
    ]


@pytest.mark.asyncio
async def test_enable_rejects_non_paper_config(client, auth_headers, db):
    config = DcaConfig(
        ticker="BTC/USD",
        venue="paper-test",
        cadence_days=7,
        fixed_cash_amount=Decimal("100.00000000"),
        dip_buy_enabled=True,
        dip_buy_multiplier=Decimal("2.0000"),
        min_cash_reserve=Decimal("500.00000000"),
        max_position_percent=Decimal("25.0000"),
        paper_only=False,
        enabled=False,
    )
    db.add(config)
    await db.commit()

    response = await client.post(
        f"/v1/kraken/dca/configs/{config.id}/enable",
        headers=auth_headers,
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_management_routes_call_no_execution_provider_broker_scheduler_or_planner(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    create_order_intent = MagicMock()
    provider_get_quote = AsyncMock()
    provider_get_bars = AsyncMock()
    broker_test_connection = AsyncMock()
    task_delay = MagicMock()
    task_apply_async = MagicMock()
    evaluate_plan = MagicMock()
    monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_quote", provider_get_quote)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_bars", provider_get_bars)
    monkeypatch.setattr(KrakenAdapter, "test_connection", broker_test_connection)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "delay", task_delay)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "apply_async", task_apply_async)
    monkeypatch.setattr(KrakenDCAPlanner, "evaluate_plan", evaluate_plan)

    created = await client.post(
        "/v1/kraken/dca/configs",
        headers=auth_headers,
        json=_payload(ticker="BTC/USD"),
    )
    config_id = created.json()["id"]
    updated = await client.patch(
        f"/v1/kraken/dca/configs/{config_id}",
        headers=auth_headers,
        json={"cadence_days": 21},
    )
    enabled = await client.post(
        f"/v1/kraken/dca/configs/{config_id}/enable",
        headers=auth_headers,
    )
    disabled = await client.post(
        f"/v1/kraken/dca/configs/{config_id}/disable",
        headers=auth_headers,
    )

    assert [created.status_code, updated.status_code, enabled.status_code, disabled.status_code] == [
        201,
        200,
        200,
        200,
    ]
    assert await _order_count(db) == 0
    create_order_intent.assert_not_called()
    provider_get_quote.assert_not_called()
    provider_get_bars.assert_not_called()
    broker_test_connection.assert_not_called()
    task_delay.assert_not_called()
    task_apply_async.assert_not_called()
    evaluate_plan.assert_not_called()

    runner = StrategyRunner(MagicMock())
    strategy = MagicMock()
    strategy.type = "kraken_dca"
    strategy.params = {}
    assert runner._make_engine(strategy) is None


@pytest.mark.asyncio
async def test_unsupported_slash_ticker_is_rejected(client, auth_headers):
    response = await client.post(
        "/v1/kraken/dca/configs",
        headers=auth_headers,
        json=_payload(ticker="SOL/USD"),
    )

    assert response.status_code == 422
