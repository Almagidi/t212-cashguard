"""Controlled Trading 212 demo multi-order script tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from scripts import t212_demo_controlled_multi_order as smoke


def _safe_env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_MODE": "demo",
        "T212_ENVIRONMENT": "demo",
        "LIVE_TRADING_ENABLED": "false",
        "T212_DEMO_ORDER_ENABLED": "true",
        "T212_DEMO_MULTI_ORDER_ENABLED": "true",
        "T212_DEMO_MULTI_ORDER_CONFIRM": "PLACE_MULTI_DEMO_ORDERS",
        "DEMO_RECONCILIATION_SCHEDULER_ENABLED": "false",
        "T212_DEMO_MULTI_ORDER_PLAN": "AAPL_US_EQ:0.01,MSFT_US_EQ:0.01",
        "T212_DEMO_API_KEY": "demo-key",
        "T212_DEMO_API_SECRET": "demo-secret",
        "T212_API_KEY": "generic-key",
        "T212_API_SECRET": "generic-secret",
        "T212_DEMO_MULTI_ORDER_COOLDOWN_SECONDS": "0",
        "T212_DEMO_MULTI_ORDER_BETWEEN_SECONDS": "0",
    }
    env.update(overrides)
    return env


def test_order_plan_parser_accepts_valid_plan() -> None:
    plan = smoke.parse_order_plan("AAPL_US_EQ:0.01,MSFT_US_EQ:0.02")

    assert [(item.ticker, item.quantity) for item in plan] == [
        ("AAPL_US_EQ", Decimal("0.01")),
        ("MSFT_US_EQ", Decimal("0.02")),
    ]


@pytest.mark.parametrize(
    "raw",
    ["", "   ", ",,"],
)
def test_order_plan_parser_rejects_empty_plan(raw: str) -> None:
    with pytest.raises(smoke.PlanValidationError, match="empty"):
        smoke.parse_order_plan(raw)


@pytest.mark.parametrize(
    "raw",
    ["AAPL_US_EQ", "AAPL_US_EQ:", ":0.01", "AAPL_US_EQ:not-a-number"],
)
def test_order_plan_parser_rejects_malformed_item(raw: str) -> None:
    with pytest.raises(smoke.PlanValidationError):
        smoke.parse_order_plan(raw)


@pytest.mark.parametrize("quantity", ["0", "-0.01"])
def test_order_plan_parser_rejects_zero_or_negative_quantity(quantity: str) -> None:
    with pytest.raises(smoke.PlanValidationError, match="positive"):
        smoke.parse_order_plan(f"AAPL_US_EQ:{quantity}")


def test_order_plan_parser_rejects_duplicate_tickers() -> None:
    with pytest.raises(smoke.PlanValidationError, match="Duplicate"):
        smoke.parse_order_plan("AAPL_US_EQ:0.01,AAPL_US_EQ:0.01")


def test_order_plan_parser_enforces_default_max_order_cap() -> None:
    with pytest.raises(smoke.PlanValidationError, match="at most 2"):
        smoke.parse_order_plan("AAPL_US_EQ:0.01,MSFT_US_EQ:0.01,NVDA_US_EQ:0.01")


def test_order_plan_parser_allows_explicit_hard_cap_of_three() -> None:
    plan = smoke.parse_order_plan(
        "AAPL_US_EQ:0.01,MSFT_US_EQ:0.01,NVDA_US_EQ:0.01",
        max_orders=3,
    )

    assert len(plan) == 3


def test_order_plan_parser_refuses_above_hard_cap() -> None:
    with pytest.raises(smoke.PlanValidationError, match="hard cap"):
        smoke.parse_order_plan(
            "AAPL_US_EQ:0.01,MSFT_US_EQ:0.01,NVDA_US_EQ:0.01,GOOG_US_EQ:0.01",
            max_orders=4,
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"APP_MODE": "live"}, "APP_MODE must be 'demo'"),
        ({"T212_ENVIRONMENT": "live"}, "T212_ENVIRONMENT must be 'demo'"),
        ({"LIVE_TRADING_ENABLED": "true"}, "LIVE_TRADING_ENABLED must be false"),
        ({"T212_DEMO_MULTI_ORDER_CONFIRM": ""}, "T212_DEMO_MULTI_ORDER_CONFIRM"),
        ({"T212_DEMO_MULTI_ORDER_ENABLED": "false"}, "T212_DEMO_MULTI_ORDER_ENABLED"),
        (
            {"DEMO_RECONCILIATION_SCHEDULER_ENABLED": "true"},
            "DEMO_RECONCILIATION_SCHEDULER_ENABLED must be false",
        ),
    ],
)
def test_safety_env_refuses_unsafe_boundaries(
    override: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(smoke.SafetyGateError, match=message):
        smoke.require_safety_env(_safe_env(**override))


def test_safety_env_refuses_missing_order_plan() -> None:
    with pytest.raises(smoke.SafetyGateError, match="T212_DEMO_MULTI_ORDER_PLAN"):
        smoke.require_safety_env(_safe_env(T212_DEMO_MULTI_ORDER_PLAN=""))


def test_credentials_prefer_demo_specific_names() -> None:
    credentials = smoke.select_credentials(_safe_env())

    assert credentials.api_key == "demo-key"
    assert credentials.api_secret == "demo-secret"
    assert credentials.source == "demo-specific"


def test_credentials_fall_back_to_generic_names_when_demo_absent() -> None:
    credentials = smoke.select_credentials(
        _safe_env(
            T212_DEMO_API_KEY="",
            T212_DEMO_API_SECRET="",
            T212_API_KEY="generic-key",
            T212_API_SECRET="generic-secret",
        )
    )

    assert credentials.api_key == "generic-key"
    assert credentials.api_secret == "generic-secret"
    assert credentials.source == "generic-fallback"


def test_live_environment_adapter_cannot_be_constructed() -> None:
    with pytest.raises(smoke.SafetyGateError, match="demo adapter"):
        smoke.ensure_demo_adapter_environment("live")


def test_summary_counts_accepted_failed_attempted_and_skipped() -> None:
    summary = smoke.build_summary(
        run_id="run-1",
        requested_count=3,
        results=[
            smoke.OrderPlacementResult(
                ticker="AAPL_US_EQ",
                quantity=Decimal("0.01"),
                outcome="accepted",
                local_order_id="local-1",
                broker_order_id="broker-1",
                local_status="accepted",
            ),
            smoke.OrderPlacementResult(
                ticker="MSFT_US_EQ",
                quantity=Decimal("0.01"),
                outcome="failed",
                error_category="broker_api_error",
            ),
        ],
        safety=smoke.SafetyMarkers(operator_confirmed=True, bounded_order_count=True),
    )

    assert summary["requested_orders"] == 3
    assert summary["attempted"] == 2
    assert summary["accepted"] == 1
    assert summary["failed"] == 1
    assert summary["skipped_rejected_before_broker"] == 1
    assert summary["safety"]["broker_environment"] == "demo"
    assert summary["safety"]["no_live_endpoint"] is True


def test_placement_loop_records_accepted_broker_order_ids() -> None:
    calls: list[str] = []

    def fake_request(
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        token: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, dict]:
        del method, token, timeout
        if path == "/v1/auth/login":
            return 200, {"access_token": "token"}
        if path == "/v1/broker/trading212/connect":
            return 200, {"environment": "demo", "credential_state": "configured"}
        if path == "/v1/orders":
            ticker = str(payload["ticker"])
            calls.append(ticker)
            return 201, {
                "id": f"local-{ticker}",
                "ticker": ticker,
                "quantity": payload["quantity"],
                "status": "accepted",
                "broker_order_id": f"broker-{ticker}",
                "execution_environment": "demo",
                "is_dry_run": False,
            }
        raise AssertionError(f"unexpected path {path}")

    result = smoke.run_controlled_multi_order(
        env=_safe_env(),
        request_json=fake_request,
        run_id="run-1",
    )

    assert result.exit_code == smoke.EXIT_SUCCESS
    assert calls == ["AAPL_US_EQ", "MSFT_US_EQ"]
    assert result.summary["accepted"] == 2
    assert [item["broker_order_id"] for item in result.summary["orders"]] == [
        "broker-AAPL_US_EQ",
        "broker-MSFT_US_EQ",
    ]
    assert {item["execution_environment"] for item in result.summary["orders"]} == {"demo"}
    assert {item["is_dry_run"] for item in result.summary["orders"]} == {False}


def test_placement_loop_stops_after_first_broker_error() -> None:
    calls: list[str] = []

    def fake_request(
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        token: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, dict]:
        del method, token, timeout
        if path == "/v1/auth/login":
            return 200, {"access_token": "token"}
        if path == "/v1/broker/trading212/connect":
            return 200, {"environment": "demo", "credential_state": "configured"}
        if path == "/v1/orders":
            calls.append(str(payload["ticker"]))
            return 429, {"detail": {"code": "broker_rate_limited"}}
        raise AssertionError(f"unexpected path {path}")

    result = smoke.run_controlled_multi_order(
        env=_safe_env(),
        request_json=fake_request,
        run_id="run-1",
    )

    assert result.exit_code == smoke.EXIT_BROKER_ERROR
    assert calls == ["AAPL_US_EQ"]
    assert result.summary["attempted"] == 1
    assert result.summary["failed"] == 1
    assert result.summary["skipped_rejected_before_broker"] == 1
