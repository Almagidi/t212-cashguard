"""Controlled Trading 212 demo multi-order placement smoke.

This script intentionally places a tiny bounded set of Trading 212 DEMO orders
through the app API route, not by calling the broker adapter directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

API_URL = os.getenv("T212_DEMO_API_URL", "http://127.0.0.1:8004").rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@localhost")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
CONFIRMATION = "PLACE_MULTI_DEMO_ORDERS"
DEFAULT_MAX_ORDERS = 2
HARD_MAX_ORDERS = 3
MAX_QUANTITY = Decimal("0.05")
COOLDOWN_SECONDS = float(os.getenv("T212_DEMO_MULTI_ORDER_COOLDOWN_SECONDS", "5"))
BETWEEN_ORDER_SECONDS = float(os.getenv("T212_DEMO_MULTI_ORDER_BETWEEN_SECONDS", "2"))

EXIT_SUCCESS = 0
EXIT_SAFETY_REFUSED = 2
EXIT_VALIDATION_FAILED = 3
EXIT_BROKER_ERROR = 4
EXIT_PARTIAL_SUCCESS = 5
EXIT_WRITE_BOUNDARY_FAILURE = 6

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")


class PlanValidationError(ValueError):
    """Raised when the operator-supplied order plan is invalid."""


class SafetyGateError(RuntimeError):
    """Raised when a required terminal safety gate is not satisfied."""


class BrokerApiError(RuntimeError):
    """Raised when login or broker connection fails before order placement."""


@dataclass(frozen=True)
class OrderPlanItem:
    ticker: str
    quantity: Decimal


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    source: Literal["demo-specific", "generic-fallback"]


@dataclass(frozen=True)
class SafetyMarkers:
    live_trading_enabled: bool = False
    broker_environment: str = "demo"
    no_live_endpoint: bool = True
    operator_confirmed: bool = False
    bounded_order_count: bool = False


@dataclass(frozen=True)
class OrderPlacementResult:
    ticker: str
    quantity: Decimal
    outcome: Literal["accepted", "failed"]
    local_order_id: str | None = None
    broker_order_id: str | None = None
    local_status: str | None = None
    execution_environment: str | None = None
    is_dry_run: bool | None = None
    error_category: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ControlledRunResult:
    exit_code: int
    summary: dict[str, Any]


RequestJson = Callable[..., tuple[int, Any]]


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _env_value(env, name)
    if not raw:
        return default
    return float(raw)


def _env_value(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _require_env(
    env: Mapping[str, str],
    name: str,
    expected: str | None = None,
) -> str:
    value = _env_value(env, name)
    if not value:
        raise SafetyGateError(f"Missing required environment variable: {name}")
    if expected is not None and value != expected:
        raise SafetyGateError(f"{name} must be {expected!r}; got {value!r}")
    return value


def require_safety_env(env: Mapping[str, str] = os.environ) -> None:
    app_mode = _env_value(env, "APP_MODE").lower()
    t212_environment = _env_value(env, "T212_ENVIRONMENT").lower()

    if app_mode != "demo":
        raise SafetyGateError(f"APP_MODE must be 'demo'; got {app_mode!r}")
    if t212_environment != "demo":
        raise SafetyGateError(f"T212_ENVIRONMENT must be 'demo'; got {t212_environment!r}")
    if _enabled(env.get("LIVE_TRADING_ENABLED")):
        raise SafetyGateError("LIVE_TRADING_ENABLED must be false")
    if not _enabled(env.get("T212_DEMO_ORDER_ENABLED")):
        raise SafetyGateError("T212_DEMO_ORDER_ENABLED must be true")
    if not _enabled(env.get("T212_DEMO_MULTI_ORDER_ENABLED")):
        raise SafetyGateError("T212_DEMO_MULTI_ORDER_ENABLED must be true")
    _require_env(env, "T212_DEMO_MULTI_ORDER_CONFIRM", CONFIRMATION)
    if _enabled(env.get("DEMO_RECONCILIATION_SCHEDULER_ENABLED")):
        raise SafetyGateError(
            "DEMO_RECONCILIATION_SCHEDULER_ENABLED must be false during placement smoke"
        )
    _require_env(env, "T212_DEMO_MULTI_ORDER_PLAN")


def select_credentials(env: Mapping[str, str] = os.environ) -> Credentials:
    demo_key = _env_value(env, "T212_DEMO_API_KEY")
    demo_secret = _env_value(env, "T212_DEMO_API_SECRET")
    if demo_key and demo_secret:
        return Credentials(demo_key, demo_secret, "demo-specific")

    api_key = _require_env(env, "T212_API_KEY")
    api_secret = _require_env(env, "T212_API_SECRET")
    return Credentials(api_key, api_secret, "generic-fallback")


def ensure_demo_adapter_environment(environment: str) -> None:
    if environment != "demo":
        raise SafetyGateError(f"Refusing to construct non-demo adapter: {environment!r}")


def _parse_quantity(raw: str) -> Decimal:
    try:
        quantity = Decimal(raw)
    except InvalidOperation as exc:
        raise PlanValidationError(f"Malformed quantity: {raw!r}") from exc
    if not quantity.is_finite():
        raise PlanValidationError(f"Malformed quantity: {raw!r}")
    if quantity <= 0:
        raise PlanValidationError("Order quantity must be positive")
    if quantity > MAX_QUANTITY:
        raise PlanValidationError(f"Order quantity must be <= {MAX_QUANTITY}")
    return quantity


def parse_order_plan(raw: str, *, max_orders: int = DEFAULT_MAX_ORDERS) -> list[OrderPlanItem]:
    if max_orders > HARD_MAX_ORDERS:
        raise PlanValidationError(
            f"Requested max order count exceeds hard cap of {HARD_MAX_ORDERS}"
        )

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise PlanValidationError("Order plan is empty")
    if len(parts) > max_orders:
        raise PlanValidationError(f"Order plan may contain at most {max_orders} orders")

    seen: set[str] = set()
    plan: list[OrderPlanItem] = []
    for part in parts:
        if ":" not in part:
            raise PlanValidationError(f"Malformed order plan item: {part!r}")
        ticker_raw, quantity_raw = [item.strip() for item in part.split(":", 1)]
        if not ticker_raw or not quantity_raw:
            raise PlanValidationError(f"Malformed order plan item: {part!r}")

        ticker = ticker_raw.upper()
        if not _TICKER_RE.fullmatch(ticker):
            raise PlanValidationError(f"Malformed ticker: {ticker_raw!r}")
        if ticker in seen:
            raise PlanValidationError(f"Duplicate ticker in order plan: {ticker}")
        seen.add(ticker)
        plan.append(OrderPlanItem(ticker=ticker, quantity=_parse_quantity(quantity_raw)))

    return plan


def request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        API_URL + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            return res.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return exc.code, body


def _error_category(status: int, body: Any) -> str:
    if status == 401 or status == 403:
        return "broker_auth_error"
    if status == 429:
        return "broker_rate_limited"
    if status >= 500:
        return "broker_api_error"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and detail.get("code"):
            return str(detail["code"])
    return "api_error"


def _safe_error(body: Any) -> str:
    text = json.dumps(body, default=str) if isinstance(body, dict | list) else str(body)
    for sensitive in ("api_key", "api_secret", "secret", "token", "password"):
        text = re.sub(sensitive, "[redacted]", text, flags=re.IGNORECASE)
    return text[:500]


def _assert_ok(status: int, body: Any, label: str) -> None:
    if status < 200 or status >= 300:
        print(f"{label} failed with HTTP {status}")
        print(_safe_error(body))
        raise BrokerApiError(f"{label} failed before order placement")


def build_summary(
    *,
    run_id: str,
    requested_count: int,
    results: list[OrderPlacementResult],
    safety: SafetyMarkers,
) -> dict[str, Any]:
    accepted = sum(1 for item in results if item.outcome == "accepted")
    failed = sum(1 for item in results if item.outcome == "failed")
    attempted = len(results)
    return {
        "run_id": run_id,
        "requested_orders": requested_count,
        "attempted": attempted,
        "accepted": accepted,
        "failed": failed,
        "skipped_rejected_before_broker": requested_count - attempted,
        "orders": [
            {
                "ticker": item.ticker,
                "quantity": str(item.quantity),
                "local_order_id": item.local_order_id,
                "broker_order_id": item.broker_order_id,
                "local_status": item.local_status,
                "execution_environment": item.execution_environment,
                "is_dry_run": item.is_dry_run,
                "outcome": item.outcome,
                "error_category": item.error_category,
                "error": item.error,
            }
            for item in results
        ],
        "safety": {
            "live_trading_enabled": safety.live_trading_enabled,
            "broker_environment": safety.broker_environment,
            "no_live_endpoint": safety.no_live_endpoint,
            "operator_confirmed": safety.operator_confirmed,
            "bounded_order_count": safety.bounded_order_count,
        },
    }


def _max_orders_from_env(env: Mapping[str, str]) -> int:
    raw = _env_value(env, "T212_DEMO_MULTI_ORDER_MAX_ORDERS")
    if not raw:
        return DEFAULT_MAX_ORDERS
    try:
        return int(raw)
    except ValueError as exc:
        raise PlanValidationError("T212_DEMO_MULTI_ORDER_MAX_ORDERS must be an integer") from exc


def _print_plan(run_id: str, plan: list[OrderPlanItem], credential_source: str) -> None:
    print("Controlled Trading 212 DEMO multi-order placement smoke")
    print(f"run_id={run_id}")
    print(f"API: {API_URL}")
    print("Live trading: disabled")
    print("Broker environment: Trading 212 demo")
    print("Scheduler: disabled")
    print(f"Credential source: {credential_source}")
    print("Order plan:")
    for index, item in enumerate(plan, start=1):
        print(f"  {index}. ticker={item.ticker} quantity={item.quantity}")
    print("")


def _place_order(
    item: OrderPlanItem,
    *,
    token: str,
    request_json: RequestJson,
) -> OrderPlacementResult:
    status, body = request_json(
        "POST",
        "/v1/orders",
        payload={
            "ticker": item.ticker,
            "side": "buy",
            "order_type": "market",
            "quantity": str(item.quantity),
            "time_validity": "DAY",
        },
        token=token,
        timeout=90.0,
    )
    if status < 200 or status >= 300:
        return OrderPlacementResult(
            ticker=item.ticker,
            quantity=item.quantity,
            outcome="failed",
            error_category=_error_category(status, body),
            error=f"HTTP {status}: {_safe_error(body)}",
        )
    if not isinstance(body, dict):
        return OrderPlacementResult(
            ticker=item.ticker,
            quantity=item.quantity,
            outcome="failed",
            error_category="unexpected_response",
            error="Order route returned a non-object response",
        )

    broker_order_id = body.get("broker_order_id")
    local_status = body.get("status")
    if local_status == "error" or not broker_order_id:
        return OrderPlacementResult(
            ticker=item.ticker,
            quantity=item.quantity,
            outcome="failed",
            local_order_id=str(body.get("id")) if body.get("id") else None,
            local_status=str(local_status) if local_status else None,
            execution_environment=str(body.get("execution_environment"))
            if body.get("execution_environment")
            else None,
            is_dry_run=body.get("is_dry_run") if isinstance(body.get("is_dry_run"), bool) else None,
            error_category="broker_order_not_accepted",
            error=_safe_error(body.get("error_message") or "missing broker_order_id"),
        )

    return OrderPlacementResult(
        ticker=item.ticker,
        quantity=item.quantity,
        outcome="accepted",
        local_order_id=str(body.get("id")) if body.get("id") else None,
        broker_order_id=str(broker_order_id),
        local_status=str(local_status) if local_status else None,
        execution_environment=str(body.get("execution_environment"))
        if body.get("execution_environment")
        else None,
        is_dry_run=body.get("is_dry_run") if isinstance(body.get("is_dry_run"), bool) else None,
    )


def run_controlled_multi_order(
    *,
    env: Mapping[str, str] = os.environ,
    request_json: RequestJson = request_json,
    run_id: str | None = None,
) -> ControlledRunResult:
    require_safety_env(env)
    credentials = select_credentials(env)
    ensure_demo_adapter_environment(_env_value(env, "T212_ENVIRONMENT"))
    plan = parse_order_plan(
        _env_value(env, "T212_DEMO_MULTI_ORDER_PLAN"),
        max_orders=_max_orders_from_env(env),
    )
    run_id = run_id or f"t212-demo-multi-order-{uuid.uuid4()}"
    _print_plan(run_id, plan, credentials.source)

    status, login = request_json(
        "POST",
        "/v1/auth/login",
        payload={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    _assert_ok(status, login, "login")
    token = login["access_token"]

    status, connect = request_json(
        "POST",
        "/v1/broker/trading212/connect",
        payload={
            "api_key": credentials.api_key,
            "api_secret": credentials.api_secret,
            "environment": "demo",
        },
        token=token,
    )
    _assert_ok(status, connect, "broker connect")
    if isinstance(connect, dict) and connect.get("environment") != "demo":
        raise SafetyGateError("Broker connect did not return demo environment")

    cooldown_seconds = _float_env(env, "T212_DEMO_MULTI_ORDER_COOLDOWN_SECONDS", COOLDOWN_SECONDS)
    between_order_seconds = _float_env(
        env,
        "T212_DEMO_MULTI_ORDER_BETWEEN_SECONDS",
        BETWEEN_ORDER_SECONDS,
    )
    if cooldown_seconds > 0:
        print(f"Waiting {cooldown_seconds:.0f}s before the first order route call...")
        time.sleep(cooldown_seconds)

    results: list[OrderPlacementResult] = []
    for index, item in enumerate(plan, start=1):
        result = _place_order(item, token=token, request_json=request_json)
        results.append(result)
        print(
            f"{index}. ticker={result.ticker} quantity={result.quantity} "
            f"outcome={result.outcome} local_order_id={result.local_order_id} "
            f"broker_order_id={result.broker_order_id} error_category={result.error_category}"
        )
        if result.outcome == "failed":
            print("Stopping after first placement failure; no further broker writes attempted.")
            break
        if index < len(plan) and between_order_seconds > 0:
            time.sleep(between_order_seconds)

    summary = build_summary(
        run_id=run_id,
        requested_count=len(plan),
        results=results,
        safety=SafetyMarkers(
            operator_confirmed=True,
            bounded_order_count=len(plan) <= HARD_MAX_ORDERS,
        ),
    )
    accepted = summary["accepted"]
    failed = summary["failed"]
    if failed and accepted:
        exit_code = EXIT_PARTIAL_SUCCESS
    elif failed:
        exit_code = EXIT_BROKER_ERROR
    else:
        exit_code = EXIT_SUCCESS
    return ControlledRunResult(exit_code=exit_code, summary=summary)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Place a bounded, explicitly confirmed Trading 212 DEMO multi-order smoke."
    )
    parser.add_argument(
        "--plan",
        help="Order plan such as AAPL_US_EQ:0.01,MSFT_US_EQ:0.01. Overrides env plan.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    env = dict(os.environ)
    if args.plan is not None:
        env["T212_DEMO_MULTI_ORDER_PLAN"] = args.plan
    try:
        result = run_controlled_multi_order(env=env)
    except SafetyGateError as exc:
        print(f"Safety gate refused controlled multi-order smoke: {exc}")
        return EXIT_SAFETY_REFUSED
    except PlanValidationError as exc:
        print(f"Order plan validation failed: {exc}")
        return EXIT_VALIDATION_FAILED
    except BrokerApiError as exc:
        print(f"Broker/auth/API error before placement loop: {exc}")
        return EXIT_BROKER_ERROR
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Unexpected write-boundary/safety failure: {exc}")
        return EXIT_WRITE_BOUNDARY_FAILURE

    print("")
    print("Placement summary:")
    print(json.dumps(result.summary, indent=2, sort_keys=True))
    print("")
    print("Next manual reconciliation command:")
    print("  T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY \\")
    print("  make t212-demo-multi-order-reconciliation-smoke")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
