"""API tests for the unified read-only operator status endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.broker.kraken import KrakenAdapter
from app.broker.trading212 import Trading212Adapter
from app.db.models import (
    AuditLog,
    DcaConfig,
    DcaPlanState,
    Order,
    PositionSnapshot,
    Strategy,
    VenueConfig,
    WorkerHeartbeat,
)
from app.db.seed import seed_dca_configs
from app.execution.engine import ExecutionEngine
from app.execution.paper_engine import PAPER_EXECUTION_ENVIRONMENT
from app.market_data.kraken_provider import KrakenMarketDataProvider
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers import tasks_dca, tasks_heartbeat

BASE_TIME = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


def _venue(
    venue: str,
    *,
    kill_switch_active: bool = False,
    auto_trading_enabled: bool = False,
    degraded_mode_active: bool = False,
    note: str | None = None,
) -> VenueConfig:
    return VenueConfig(
        venue=venue,
        kill_switch_active=kill_switch_active,
        auto_trading_enabled=auto_trading_enabled,
        degraded_mode_active=degraded_mode_active,
        note=note,
        updated_at=BASE_TIME,
    )


def _strategy(
    *,
    venue: str = "t212",
    is_live: bool = False,
    live_approved: bool = False,
) -> Strategy:
    params = {}
    if live_approved:
        params = {"promotion": {"live_approved_at": BASE_TIME.isoformat()}}
    return Strategy(
        id=uuid.uuid4(),
        name=f"{venue} strategy",
        type="kraken_trend_follow" if venue == "kraken" else "orb",
        is_enabled=True,
        is_live=is_live,
        venue=venue,
        params=params,
        allowed_tickers=["BTC/USD"] if venue == "kraken" else ["AAPL"],
    )


def _order(
    *,
    venue: str = "t212",
    status: str = "submitted",
    created_at: datetime = BASE_TIME,
) -> Order:
    return Order(
        id=uuid.uuid4(),
        client_order_key=f"{venue}-{status}-{uuid.uuid4()}",
        ticker="BTC/USD" if venue == "kraken" else "AAPL",
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
        status=status,
        venue=venue,
        created_at=created_at,
    )


def _dca_decision(
    *,
    ticker: str = "BTC/USD",
    code: str = "BUY_DUE",
    occurred_at: datetime = BASE_TIME,
) -> AuditLog:
    return AuditLog(
        action="dca_paper_decision",
        entity_type="dca_plan_state",
        entity_id=f"kraken:{ticker}",
        actor="worker:dca_scheduler",
        payload={
            "ticker": ticker,
            "venue": "kraken",
            "paper_only": True,
            "decision_code": code,
            "reason": "test decision",
            "amount_usd": "100.00000000",
        },
        occurred_at=occurred_at,
    )


def _heartbeat(*, last_seen_at: datetime, worker_name: str = "worker-a") -> WorkerHeartbeat:
    return WorkerHeartbeat(
        component="celery_worker",
        worker_name=worker_name,
        status="healthy",
        last_seen_at=last_seen_at,
        payload={"source": "test"},
    )


@pytest.mark.asyncio
async def test_operator_status_returns_control_tower_summary(
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
            _venue("t212", auto_trading_enabled=False, note="normal"),
            _venue("kraken", auto_trading_enabled=False, note="paper only"),
            _strategy(venue="t212", live_approved=True),
            _strategy(venue="kraken"),
            _order(
                venue="t212",
                status="submitted",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            ),
            _order(
                venue="kraken",
                status="filled",
                created_at=datetime.now(UTC) - timedelta(minutes=4),
            ),
            DcaPlanState(
                ticker="BTC/USD",
                venue="kraken",
                last_buy_at=date(2026, 4, 30),
                last_decision_at=date(2026, 5, 1),
                total_allocated_usd=Decimal("250.00000000"),
                executions_count=2,
                last_decision_code="BUY_DUE",
            ),
            _dca_decision(ticker="BTC/USD", code="BUY_DUE", occurred_at=BASE_TIME),
            _dca_decision(
                ticker="ETH/USD",
                code="BLOCKED_LOW_CASH",
                occurred_at=BASE_TIME - timedelta(minutes=1),
            ),
            _dca_decision(
                ticker="BTC/USD",
                code="SKIP_ALREADY_BOUGHT_THIS_WINDOW",
                occurred_at=BASE_TIME - timedelta(minutes=2),
            ),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["subsystem"] == "operator"
    assert body["mode"] == "read_only_status"
    assert body["overall_status"] == "degraded"
    assert body["live_trading_possible"] is False
    assert body["live_trading_enabled_anywhere"] is False

    venues = {item["venue"]: item for item in body["venues"]}
    assert venues["t212"]["present"] is True
    assert venues["t212"]["kill_switch_active"] is False
    assert venues["kraken"]["note"] == "paper only"

    assert body["trading212"]["strategies_count"] == 1
    assert body["trading212"]["live_approved_strategies_count"] == 1
    assert body["trading212"]["active_orders_count"] == 1
    assert body["trading212"]["recent_orders_count"] == 1
    assert body["trading212"]["latest_order_status"] == "submitted"
    assert body["trading212"]["live_readiness_status"]["ready_for_live"] is False

    assert body["kraken"]["strategies_count"] == 1
    assert body["kraken"]["paper_only_strategies_count"] == 1
    assert body["kraken"]["live_enabled"] is False
    assert body["kraken"]["active_orders_count"] == 0
    assert body["kraken"]["recent_orders_count"] == 1
    assert "disabled/unproven" in body["kraken"]["safety_notes"][0]

    assert body["dca"]["config_count"] == 2
    assert body["dca"]["enabled_config_count"] == 1
    assert body["dca"]["decision_count_total"] == 3
    assert body["dca"]["buy_due_count"] == 1
    assert body["dca"]["blocked_count"] == 1
    assert body["dca"]["skipped_count"] == 1
    assert body["dca"]["total_paper_allocated_usd"] == "250.00000000"
    assert body["dca"]["scheduler_registered"] is True
    assert body["dca"]["scheduler_cadence"] == "daily at 01:00 UTC"
    assert body["dca"]["worker_health"] == "missing"
    assert body["dca"]["runnable"] is False
    assert body["dca"]["live_enabled"] is False
    assert body["dca"]["paper_only"] is True
    assert body["dca"]["tickers"] == ["BTC/USD", "ETH/USD"]
    assert body["paper_execution"] == {
        "paper_only": True,
        "enabled_in_mode": "mock",
        "total_paper_orders": 0,
        "latest_paper_order_timestamp": None,
        "last_paper_execution_status": None,
        "open_paper_positions_count": 0,
        "safety_notes": [
            "Paper execution is local/mock only.",
            "No broker order sent.",
            "Global kill switch blocks paper simulation in this endpoint.",
        ],
    }

    assert body["schedulers"] == {
        "dca_paper_evaluate_registered": True,
        "dca_paper_evaluate_cadence": "daily at 01:00 UTC",
        "heartbeat_registered": True,
        "heartbeat_cadence": "60.0",
        "worker_health": "missing",
        "heartbeat_component": "celery_worker",
        "heartbeat_last_seen_at": None,
        "heartbeat_stale_after_seconds": 180,
    }
    assert body["safety_flags"]["endpoint_read_only"] is True
    assert body["safety_flags"]["creates_orders"] is False
    assert body["safety_flags"]["calls_brokers"] is False
    assert body["safety_flags"]["triggers_schedulers"] is False
    assert body["safety_flags"]["runs_strategies"] is False
    assert body["safety_flags"]["dca_runnable"] is False
    assert body["safety_flags"]["dca_live_enabled"] is False
    assert body["safety_flags"]["kraken_live_enabled"] is False
    assert body["safety_flags"]["worker_health_known"] is False

    dca_activity = [
        item for item in body["recent_activity"] if item["action"] == "dca_paper_decision"
    ]
    assert len(dca_activity) == 3
    assert dca_activity[0]["payload_summary"]["ticker"] == "BTC/USD"
    assert "api_key" not in dca_activity[0]["payload_summary"]


@pytest.mark.asyncio
async def test_operator_status_reports_recent_persisted_heartbeat_as_healthy(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["dca"]["worker_health"] == "healthy"
    assert body["schedulers"]["worker_health"] == "healthy"
    assert body["schedulers"]["heartbeat_component"] == "celery_worker"
    assert body["schedulers"]["heartbeat_last_seen_at"] is not None
    assert body["schedulers"]["heartbeat_stale_after_seconds"] == 180
    assert body["safety_flags"]["worker_health_known"] is True


@pytest.mark.asyncio
async def test_operator_status_reports_old_persisted_heartbeat_as_stale(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC) - timedelta(seconds=181)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["dca"]["worker_health"] == "stale"
    assert body["schedulers"]["worker_health"] == "stale"
    assert body["safety_flags"]["worker_health_known"] is False


@pytest.mark.asyncio
async def test_operator_status_does_not_treat_beat_registration_as_worker_health(
    client,
    auth_headers,
    db,
):
    db.add_all([_venue("t212"), _venue("kraken")])
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["schedulers"]["heartbeat_registered"] is True
    assert body["schedulers"]["worker_health"] == "missing"
    assert body["dca"]["scheduler_registered"] is True
    assert body["dca"]["worker_health"] == "missing"


@pytest.mark.asyncio
async def test_operator_status_route_boundary_has_no_mutation_or_control_paths(
    client,
    auth_headers,
):
    for method in (client.post, client.patch, client.put, client.delete):
        response = await method("/v1/operator/status", headers=auth_headers)
        assert response.status_code == 405

    for path in (
        "/v1/operator/execute",
        "/v1/operator/run",
        "/v1/operator/trade",
        "/v1/operator/live",
        "/v1/operator/status/execute",
        "/v1/operator/status/run",
        "/v1/operator/status/trade",
        "/v1/operator/status/live",
    ):
        response = await client.post(path, headers=auth_headers)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_operator_status_is_read_only_and_calls_no_side_effect_paths(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    await seed_dca_configs(db)
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            DcaPlanState(
                ticker="BTC/USD",
                venue="kraken",
                total_allocated_usd=Decimal("100.00000000"),
                executions_count=1,
                last_decision_code="BUY_DUE",
            ),
        ]
    )
    await db.commit()

    create_order_intent = MagicMock()
    submit_order = AsyncMock()
    kraken_test_connection = AsyncMock()
    kraken_place_market_order = AsyncMock()
    t212_test_connection = AsyncMock()
    t212_place_market_order = AsyncMock()
    provider_get_quote = AsyncMock()
    provider_get_bars = AsyncMock()
    task_delay = MagicMock()
    task_apply_async = MagicMock()
    heartbeat_task_delay = MagicMock()
    heartbeat_task_apply_async = MagicMock()
    evaluate_plan = MagicMock()

    monkeypatch.setattr(ExecutionEngine, "create_order_intent", create_order_intent)
    monkeypatch.setattr(ExecutionEngine, "submit_order", submit_order)
    monkeypatch.setattr(KrakenAdapter, "test_connection", kraken_test_connection)
    monkeypatch.setattr(KrakenAdapter, "place_market_order", kraken_place_market_order)
    monkeypatch.setattr(Trading212Adapter, "test_connection", t212_test_connection)
    monkeypatch.setattr(Trading212Adapter, "place_market_order", t212_place_market_order)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_quote", provider_get_quote)
    monkeypatch.setattr(KrakenMarketDataProvider, "get_bars", provider_get_bars)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "delay", task_delay)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "apply_async", task_apply_async)
    monkeypatch.setattr(tasks_heartbeat.record_worker_heartbeat_task, "delay", heartbeat_task_delay)
    monkeypatch.setattr(
        tasks_heartbeat.record_worker_heartbeat_task,
        "apply_async",
        heartbeat_task_apply_async,
    )
    monkeypatch.setattr(KrakenDCAPlanner, "evaluate_plan", evaluate_plan)

    before_orders = await _count(db, Order)
    before_audits = await _count(db, AuditLog)
    before_heartbeats = await _count(db, WorkerHeartbeat)
    t212_before = (
        await db.execute(select(VenueConfig).where(VenueConfig.venue == "t212"))
    ).scalar_one()
    config_before = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()
    state_before = (
        await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
    ).scalar_one()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    t212_after = (
        await db.execute(select(VenueConfig).where(VenueConfig.venue == "t212"))
    ).scalar_one()
    config_after = (
        await db.execute(select(DcaConfig).where(DcaConfig.ticker == "BTC/USD"))
    ).scalar_one()
    state_after = (
        await db.execute(select(DcaPlanState).where(DcaPlanState.ticker == "BTC/USD"))
    ).scalar_one()

    assert response.status_code == 200
    assert await _count(db, Order) == before_orders == 0
    assert await _count(db, AuditLog) == before_audits
    assert await _count(db, WorkerHeartbeat) == before_heartbeats == 0
    assert t212_after.kill_switch_active == t212_before.kill_switch_active
    assert t212_after.auto_trading_enabled == t212_before.auto_trading_enabled
    assert t212_after.degraded_mode_active == t212_before.degraded_mode_active
    assert config_after.enabled == config_before.enabled
    assert config_after.paper_only == config_before.paper_only
    assert config_after.fixed_cash_amount == config_before.fixed_cash_amount
    assert state_after.total_allocated_usd == state_before.total_allocated_usd
    assert state_after.executions_count == state_before.executions_count
    assert state_after.last_decision_code == state_before.last_decision_code

    create_order_intent.assert_not_called()
    submit_order.assert_not_called()
    kraken_test_connection.assert_not_called()
    kraken_place_market_order.assert_not_called()
    t212_test_connection.assert_not_called()
    t212_place_market_order.assert_not_called()
    provider_get_quote.assert_not_called()
    provider_get_bars.assert_not_called()
    task_delay.assert_not_called()
    task_apply_async.assert_not_called()
    heartbeat_task_delay.assert_not_called()
    heartbeat_task_apply_async.assert_not_called()
    evaluate_plan.assert_not_called()


@pytest.mark.asyncio
async def test_operator_status_kill_switch_blocks_and_missing_venues_degrade(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212", kill_switch_active=True),
            _venue("kraken"),
        ]
    )
    await db.commit()

    blocked = await client.get("/v1/operator/status", headers=auth_headers)
    assert blocked.status_code == 200
    assert blocked.json()["overall_status"] == "blocked"
    assert blocked.json()["safety_flags"]["any_venue_kill_switch_active"] is True

    await db.delete(
        (await db.execute(select(VenueConfig).where(VenueConfig.venue == "t212"))).scalar_one()
    )
    await db.commit()

    degraded = await client.get("/v1/operator/status", headers=auth_headers)
    body = degraded.json()
    assert degraded.status_code == 200
    assert body["overall_status"] == "degraded"
    assert body["safety_flags"]["missing_expected_venue_configs"] is True
    assert body["venues"][0]["present"] is False
    assert "missing" in body["venues"][0]["note"]


@pytest.mark.asyncio
async def test_operator_status_recent_activity_limit_is_bounded(
    client,
    auth_headers,
    db,
):
    db.add_all([_venue("t212"), _venue("kraken")])
    db.add_all(
        [
            AuditLog(
                action="dca_config_updated",
                entity_type="dca_config",
                entity_id=str(index),
                actor="test",
                payload={"ticker": "BTC/USD", "api_key": "should-not-leak"},
                occurred_at=BASE_TIME - timedelta(seconds=index),
            )
            for index in range(101)
        ]
    )
    await db.commit()

    response = await client.get(
        "/v1/operator/status?audit_limit=100",
        headers=auth_headers,
    )
    too_large = await client.get(
        "/v1/operator/status?audit_limit=101",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["recent_activity"]) == 100
    payload_summaries = [item["payload_summary"] for item in body["recent_activity"]]
    assert {"ticker": "BTC/USD"} in payload_summaries
    assert all("api_key" not in summary for summary in payload_summaries)
    assert too_large.status_code == 422


@pytest.mark.asyncio
async def test_operator_status_includes_paper_execution_summary(
    client,
    auth_headers,
    db,
):
    from app.db.models import BrokerConnection, User

    user = (await db.execute(select(User))).scalars().first()
    assert user is not None
    connection = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="paper",
        environment="mock",
        api_key_encrypted="paper-only",
        api_secret_encrypted="paper-only",
        is_active=True,
    )
    order = _order(venue="paper", status="filled", created_at=BASE_TIME)
    order.is_dry_run = True
    order.execution_environment = PAPER_EXECUTION_ENVIRONMENT
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            connection,
            order,
            PositionSnapshot(
                id=uuid.uuid4(),
                connection_id=connection.id,
                ticker="PAPERXYZ",
                quantity=Decimal("2"),
                avg_price=Decimal("10"),
                current_price=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                quantity_available=Decimal("2"),
                raw={"paper_only": True},
                snapshotted_at=BASE_TIME,
            ),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)

    assert response.status_code == 200
    paper = response.json()["paper_execution"]
    assert paper["paper_only"] is True
    assert paper["total_paper_orders"] == 1
    assert paper["last_paper_execution_status"] == "filled"
    assert paper["open_paper_positions_count"] == 1
    assert "No broker order sent." in paper["safety_notes"]


# ─── Operator safety-visibility metadata (PR 2) ───────────────────────────────
#
# These cover the read-only safety posture surfaced for operator visibility:
# the configured unrealized-P&L failure policy and a safe, non-secret
# broker-credential source/status. They assert the values are accurate AND that
# no secret material ever reaches the response.


def _safety_flags(response) -> dict:
    assert response.status_code == 200
    return response.json()["safety_flags"]


@pytest.mark.asyncio
async def test_operator_status_exposes_pnl_failure_policy_and_mock_credential_source(
    client,
    auth_headers,
    db,
):
    db.add_all([_venue("t212"), _venue("kraken")])
    await db.commit()

    flags = _safety_flags(await client.get("/v1/operator/status", headers=auth_headers))

    # Default test runtime is APP_MODE=mock with the fail-closed P&L policy.
    assert flags["unrealized_pnl_failure_policy"] == "block_trading"
    assert flags["credential_source"] == "mock"
    assert flags["credentials_configured"] is True


@pytest.mark.asyncio
async def test_operator_status_credential_source_stored_connection(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    from app.core.config import settings
    from app.db.models import BrokerConnection, User

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    user = (await db.execute(select(User))).scalars().first()
    assert user is not None
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            BrokerConnection(
                id=uuid.uuid4(),
                user_id=user.id,
                broker="trading212",
                environment="demo",
                api_key_encrypted="ENC-DEMO-KEY",
                api_secret_encrypted="ENC-DEMO-SECRET",
                is_active=True,
            ),
        ]
    )
    await db.commit()

    flags = _safety_flags(await client.get("/v1/operator/status", headers=auth_headers))
    assert flags["credential_source"] == "stored_connection"
    assert flags["credentials_configured"] is True


@pytest.mark.asyncio
async def test_operator_status_credential_source_environment_fallback(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "env-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "env-demo-secret")
    db.add_all([_venue("t212"), _venue("kraken")])
    await db.commit()

    flags = _safety_flags(await client.get("/v1/operator/status", headers=auth_headers))
    assert flags["credential_source"] == "environment_fallback"
    assert flags["credentials_configured"] is True


@pytest.mark.asyncio
async def test_operator_status_credential_source_none_when_unconfigured(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    db.add_all([_venue("t212"), _venue("kraken")])
    await db.commit()

    flags = _safety_flags(await client.get("/v1/operator/status", headers=auth_headers))
    assert flags["credential_source"] == "none"
    assert flags["credentials_configured"] is False


@pytest.mark.asyncio
async def test_operator_status_safety_metadata_exposes_no_secret_values(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    from app.core.config import settings
    from app.db.models import BrokerConnection, User

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "RAW-ENV-DEMO-KEY")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "RAW-ENV-DEMO-SECRET")
    user = (await db.execute(select(User))).scalars().first()
    assert user is not None
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            BrokerConnection(
                id=uuid.uuid4(),
                user_id=user.id,
                broker="trading212",
                environment="demo",
                api_key_encrypted="ENCRYPTED-KEY-BLOB",
                api_secret_encrypted="ENCRYPTED-SECRET-BLOB",
                is_active=True,
            ),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200

    # No credential value (stored encrypted blob or env-configured secret) may
    # appear anywhere in the serialized response.
    raw = response.text
    for secret in (
        "ENCRYPTED-KEY-BLOB",
        "ENCRYPTED-SECRET-BLOB",
        "RAW-ENV-DEMO-KEY",
        "RAW-ENV-DEMO-SECRET",
    ):
        assert secret not in raw

    flags = response.json()["safety_flags"]
    # Only safe, coarse metadata is surfaced.
    assert flags["credential_source"] in {
        "stored_connection",
        "environment_fallback",
        "mock",
        "none",
    }
    assert flags["credential_source"] == "stored_connection"
    assert isinstance(flags["credentials_configured"], bool)
    assert flags["unrealized_pnl_failure_policy"] in {
        "assume_zero",
        "block_trading",
        "activate_kill_switch",
    }


# ─── Operator "why blocked" readiness detail ──────────────────────────────────
#
# These cover a read-only, additive aggregation of the exact same booleans
# already used to compute ``overall_status``. ``why_blocked`` must never
# diverge from ``overall_status``: it explains it, it does not replace or
# duplicate its logic.


@pytest.mark.asyncio
async def test_operator_status_ok_has_no_blocking_reasons(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "ok"
    assert body["why_blocked"] == []


@pytest.mark.asyncio
async def test_operator_status_kill_switch_active_has_blocked_reason(
    client,
    auth_headers,
    db,
):
    db.add_all([_venue("t212", kill_switch_active=True), _venue("kraken")])
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "blocked"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "kill_switch_active" in reasons
    assert reasons["kill_switch_active"]["severity"] == "blocked"
    assert reasons["kill_switch_active"]["message"]


@pytest.mark.asyncio
async def test_operator_status_cash_only_mode_disabled_has_blocked_reason(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CASH_ONLY_MODE", False)
    db.add_all([_venue("t212"), _venue("kraken")])
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "blocked"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "cash_only_mode_disabled" in reasons
    assert reasons["cash_only_mode_disabled"]["severity"] == "blocked"


@pytest.mark.asyncio
async def test_operator_status_missing_venue_config_has_degraded_reason(
    client,
    auth_headers,
    db,
):
    db.add_all([_venue("t212")])
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "degraded"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "missing_venue_config" in reasons
    assert reasons["missing_venue_config"]["severity"] == "degraded"


@pytest.mark.asyncio
async def test_operator_status_venue_degraded_has_degraded_reason(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken", degraded_mode_active=True, note="Kraken degraded mode active."),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "degraded"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "venue_degraded" in reasons
    assert reasons["venue_degraded"]["severity"] == "degraded"


@pytest.mark.asyncio
async def test_operator_status_stale_worker_health_has_degraded_reason(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC) - timedelta(seconds=181)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "degraded"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "worker_health_unknown" in reasons
    assert reasons["worker_health_unknown"]["severity"] == "degraded"


@pytest.mark.asyncio
async def test_operator_status_missing_app_settings_has_degraded_readiness_reason(
    client,
    auth_headers,
    db,
):
    from app.db.models import AppSettings

    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    app_settings = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    await db.delete(app_settings)
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["trading212"]["live_readiness_status"] is None
    assert body["overall_status"] == "degraded"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "live_readiness_unavailable" in reasons
    assert reasons["live_readiness_unavailable"]["severity"] == "degraded"


@pytest.mark.asyncio
async def test_operator_status_why_blocked_does_not_call_side_effect_paths(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    """The why_blocked aggregation must remain part of the existing read-only
    boundary: it must not introduce any broker, execution, or scheduler calls."""

    def _fail(*args, **kwargs):
        raise AssertionError("why_blocked computation must not call this path")

    monkeypatch.setattr(ExecutionEngine, "create_order_intent", _fail)
    monkeypatch.setattr(ExecutionEngine, "submit_order", _fail)
    monkeypatch.setattr(KrakenAdapter, "place_market_order", _fail)
    monkeypatch.setattr(Trading212Adapter, "place_market_order", _fail)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "delay", _fail)
    monkeypatch.setattr(tasks_dca.evaluate_due_plans_task, "apply_async", _fail)

    db.add_all([_venue("t212", kill_switch_active=True), _venue("kraken")])
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["why_blocked"]


# ─── Protective stops visibility ──────────────────────────────────────────────


def _risk_event(
    *,
    event_type: str,
    occurred_at: datetime,
    message: str | None = None,
    ticker: str | None = None,
    payload: dict | None = None,
):
    from app.db.models import RiskEvent

    return RiskEvent(
        event_type=event_type,
        ticker=ticker,
        message=message,
        payload=payload,
        occurred_at=occurred_at,
    )


@pytest.mark.asyncio
async def test_operator_status_protective_stops_ok_state(
    client,
    auth_headers,
    db,
):
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    stops = body["protective_stops"]
    assert stops["status"] == "ok"
    assert stops["global_kill_switch_active"] is False
    assert stops["global_auto_trading_enabled"] is False
    assert stops["last_kill_switch_event"] is None
    assert stops["recent_events"] == []
    assert stops["safety_notes"]
    assert body["overall_status"] == "ok"
    assert body["why_blocked"] == []


@pytest.mark.asyncio
async def test_operator_status_global_kill_switch_blocks_and_shows_trigger(
    client,
    auth_headers,
    db,
):
    from app.db.models import AppSettings

    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
            _risk_event(
                event_type="kill_switch_on",
                occurred_at=BASE_TIME,
                message="Kill switch activated by circuit_breaker:trading212",
                payload={"actor": "circuit_breaker:trading212"},
            ),
        ]
    )
    await db.commit()

    app_settings = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    app_settings.kill_switch_active = True
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["overall_status"] == "blocked"
    reasons = {reason["code"]: reason for reason in body["why_blocked"]}
    assert "global_kill_switch_active" in reasons
    assert reasons["global_kill_switch_active"]["severity"] == "blocked"
    assert reasons["global_kill_switch_active"]["message"]

    stops = body["protective_stops"]
    assert stops["status"] == "triggered"
    assert stops["global_kill_switch_active"] is True
    last_event = stops["last_kill_switch_event"]
    assert last_event["event_type"] == "kill_switch_on"
    assert last_event["actor"] == "circuit_breaker:trading212"
    assert last_event["message"] == "Kill switch activated by circuit_breaker:trading212"


@pytest.mark.asyncio
async def test_operator_status_protective_stops_unknown_when_app_settings_missing(
    client,
    auth_headers,
    db,
):
    from app.db.models import AppSettings

    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
        ]
    )
    await db.commit()

    app_settings = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one()
    await db.delete(app_settings)
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    stops = body["protective_stops"]
    assert stops["status"] == "unknown"
    assert stops["global_kill_switch_active"] is None
    assert stops["global_auto_trading_enabled"] is None
    reasons = {reason["code"] for reason in body["why_blocked"]}
    assert "global_kill_switch_active" not in reasons


@pytest.mark.asyncio
async def test_operator_status_protective_stops_events_filtered_bounded_and_sanitized(
    client,
    auth_headers,
    db,
):
    events = [
        _risk_event(
            event_type="cash_guard_block",
            occurred_at=BASE_TIME - timedelta(minutes=index),
            message=f"cash guard block {index}",
            ticker="AAPL",
            payload={"actor": "system", "estimated_cost": "12345.67"},
        )
        for index in range(12)
    ]
    events.append(
        _risk_event(
            event_type="not_a_protective_event",
            occurred_at=BASE_TIME + timedelta(minutes=1),
            message="should not appear",
        )
    )
    db.add_all(
        [
            _venue("t212"),
            _venue("kraken"),
            _heartbeat(last_seen_at=datetime.now(UTC)),
            *events,
        ]
    )
    await db.commit()

    response = await client.get("/v1/operator/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    stops = body["protective_stops"]
    recent = stops["recent_events"]
    assert len(recent) == 10
    assert all(event["event_type"] == "cash_guard_block" for event in recent)
    assert recent[0]["message"] == "cash guard block 0"
    assert recent[0]["ticker"] == "AAPL"
    assert recent[0]["actor"] == "system"
    # Raw payloads must never be exposed through the operator surface.
    assert all("payload" not in event for event in recent)
    # Protective blocks alone do not flip the protective-stop status.
    assert stops["status"] == "ok"
