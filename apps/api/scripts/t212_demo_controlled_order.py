"""Controlled Trading 212 demo order smoke test.

This script intentionally places ONE tiny Trading 212 DEMO order through the app
API route, not by calling the broker adapter directly.

Required safety gates:
- APP_MODE=demo
- T212_ENVIRONMENT=demo
- LIVE_TRADING_ENABLED=false
- T212_DEMO_ORDER_ENABLED=true
- T212_DEMO_ORDER_CONFIRM=PLACE_DEMO_ORDER
- T212_API_KEY and T212_API_SECRET loaded
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

API_URL = os.getenv("T212_DEMO_API_URL", "http://127.0.0.1:8004").rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@localhost")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
TICKER = os.getenv("T212_DEMO_ORDER_TICKER", "AAPL").strip().upper()
QUANTITY = os.getenv("T212_DEMO_ORDER_QUANTITY", "0.001").strip()
COOLDOWN_SECONDS = float(os.getenv("T212_DEMO_ORDER_COOLDOWN_SECONDS", "5"))


def require_env(name: str, expected: str | None = None) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    if expected is not None and value != expected:
        raise SystemExit(f"{name} must be {expected!r}; got {value!r}")
    return value


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


def assert_ok(status: int, body: Any, label: str) -> None:
    if status < 200 or status >= 300:
        print(f"{label} failed with HTTP {status}")
        print(json.dumps(body, indent=2, default=str))
        raise SystemExit(1)


def main() -> int:
    require_env("APP_MODE", "demo")
    require_env("T212_ENVIRONMENT", "demo")
    require_env("LIVE_TRADING_ENABLED", "false")
    require_env("T212_DEMO_ORDER_ENABLED", "true")
    require_env("T212_DEMO_ORDER_CONFIRM", "PLACE_DEMO_ORDER")
    require_env("T212_API_KEY")
    require_env("T212_API_SECRET")

    print("Controlled Trading 212 DEMO order test")
    print(f"API: {API_URL}")
    print(f"Ticker: {TICKER}")
    print(f"Quantity: {QUANTITY}")
    print("Live trading: disabled")
    print("Environment: Trading 212 demo")
    print("")

    status, login = request_json(
        "POST",
        "/v1/auth/login",
        payload={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert_ok(status, login, "login")
    token = login["access_token"]

    status, connect = request_json(
        "POST",
        "/v1/broker/trading212/connect",
        payload={
            "api_key": os.environ["T212_API_KEY"].strip(),
            "api_secret": os.environ["T212_API_SECRET"].strip(),
            "environment": "demo",
        },
        token=token,
    )
    assert_ok(status, connect, "broker connect")

    print("Broker connected locally:")
    print(f"  environment={connect.get('environment')}")
    print(f"  credential_state={connect.get('credential_state')}")
    print(f"  last_test_ok={connect.get('last_test_ok')}")
    print(f"  account_currency={connect.get('account_currency')}")
    print("")

    if COOLDOWN_SECONDS > 0:
        print(
            f"Waiting {COOLDOWN_SECONDS:.0f}s before order route call "
            "to avoid Trading 212 demo account-summary rate limits..."
        )
        print("")
        time.sleep(COOLDOWN_SECONDS)

    status, order = request_json(
        "POST",
        "/v1/orders",
        payload={
            "ticker": TICKER,
            "side": "buy",
            "order_type": "market",
            "quantity": QUANTITY,
            "time_validity": "DAY",
        },
        token=token,
        timeout=90.0,
    )
    assert_ok(status, order, "demo order placement")

    print("DEMO order route response:")
    print(f"  id={order.get('id')}")
    print(f"  ticker={order.get('ticker')}")
    print(f"  side={order.get('side')}")
    print(f"  quantity={order.get('quantity')}")
    print(f"  status={order.get('status')}")
    print(f"  broker_order_id={order.get('broker_order_id')}")
    print(f"  execution_environment={order.get('execution_environment')}")
    print(f"  is_dry_run={order.get('is_dry_run')}")
    print("")

    if order.get("status") == "error" or not order.get("broker_order_id"):
        print("FAIL: Demo order route returned an error or no broker_order_id.")
        print("This means the request did not complete as an accepted Trading 212 demo order.")
        print(
            "Inspect the order error_message and demo_broker_order_failure audit before retrying."
        )
        return 1

    time.sleep(3)
    status, positions = request_json("GET", "/v1/positions", token=token)
    assert_ok(status, positions, "positions after demo order")

    print("Positions endpoint returned successfully after demo order.")
    print("")
    print("PASS: One controlled Trading 212 DEMO order test completed.")
    print("This was demo-only. LIVE_TRADING_ENABLED remained false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
