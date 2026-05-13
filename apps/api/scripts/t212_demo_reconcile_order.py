"""Reconcile one local Trading 212 DEMO order from broker history.

This script performs read-only broker calls only:
- GET /api/v0/equity/history/orders

It never submits, cancels, or modifies a broker order.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from app.broker.trading212 import (
    T212APIError,
    T212AuthError,
    T212RateLimitError,
    Trading212Adapter,
)
from app.db.session import AsyncSessionLocal
from app.services.demo_order_reconciliation import DemoOrderReconciler
from app.services.safety_policy import SafetyPolicyViolation


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _live_enabled() -> bool:
    return os.getenv("LIVE_TRADING_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def _require_safety_env() -> None:
    app_mode = os.getenv("APP_MODE", "demo").lower()
    t212_environment = os.getenv("T212_ENVIRONMENT", "demo").lower()

    if app_mode != "demo":
        raise SystemExit(f"Refusing to run: APP_MODE must be demo, got {app_mode!r}")
    if t212_environment != "demo":
        raise SystemExit(
            f"Refusing to run: T212_ENVIRONMENT must be demo, got {t212_environment!r}"
        )
    if _live_enabled():
        raise SystemExit("Refusing to run: LIVE_TRADING_ENABLED must be false")


async def main() -> int:
    _require_safety_env()
    order_id_raw = os.getenv("T212_DEMO_RECONCILE_ORDER_ID", "").strip()
    broker_order_id = os.getenv("T212_DEMO_RECONCILE_BROKER_ORDER_ID", "").strip()
    if not order_id_raw and not broker_order_id:
        raise SystemExit("Set T212_DEMO_RECONCILE_ORDER_ID or T212_DEMO_RECONCILE_BROKER_ORDER_ID.")

    api_key = _env("T212_API_KEY")
    api_secret = _env("T212_API_SECRET")

    print("Trading 212 DEMO order reconciliation")
    print("Mode: demo")
    print("Environment: demo")
    print("Live trading enabled: false")
    print("Broker endpoint: GET /api/v0/equity/history/orders")
    print("Write endpoints: not called")
    print("")

    try:
        async with (
            AsyncSessionLocal() as db,
            Trading212Adapter(api_key, api_secret, "demo") as broker,
        ):
            reconciler = DemoOrderReconciler(db, broker, actor="script:t212_demo_reconcile_order")
            if order_id_raw:
                result = await reconciler.reconcile_by_order_id(uuid.UUID(order_id_raw))
            else:
                result = await reconciler.reconcile_by_broker_order_id(broker_order_id)
            await db.commit()
    except ValueError as exc:
        print(f"Local order lookup failed: {exc}")
        return 1
    except SafetyPolicyViolation as exc:
        print(f"Safety gate refused reconciliation: {exc.reason}")
        return 2
    except T212RateLimitError as exc:
        print(f"Rate limited by Trading 212 demo API. Retry after {exc.retry_after}s.")
        return 3
    except T212AuthError as exc:
        print(f"Trading 212 authentication failed: HTTP {exc.status_code}")
        print("Check that the key/secret are for the Trading 212 DEMO environment.")
        return 4
    except T212APIError as exc:
        print(f"Trading 212 API error: HTTP {exc.status_code}")
        return 5

    print("Reconciliation summary:")
    print(f"  local_order_id={result.order_id}")
    print(f"  broker_order_id={result.broker_order_id}")
    print(f"  previous_local_status={result.previous_status}")
    print(f"  broker_status={result.broker_status or 'unmatched'}")
    print(f"  new_local_status={result.new_status}")
    print(f"  matched={str(result.matched).lower()}")
    print(f"  outcome={result.outcome}")
    print(f"  audit_events={','.join(result.audit_events)}")
    print("")
    if result.outcome != "success":
        print(
            "CHECK FAILED: reconciliation completed safely but did not confirm a successful match."
        )
        return 6

    print("PASS: Trading 212 DEMO order reconciliation completed without broker write calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
