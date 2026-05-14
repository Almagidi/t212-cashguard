"""Run a manual Trading 212 DEMO multi-order reconciliation smoke.

This script performs read-only broker calls only:
- GET /api/v0/equity/history/orders

It never submits, cancels, modifies, deposits, or withdraws anything.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from typing import Any

from app.broker.safety import is_broker_write_method
from app.broker.trading212 import (
    T212APIError,
    T212AuthError,
    T212RateLimitError,
    Trading212Adapter,
)
from app.core.serialization import to_jsonable
from app.db.session import AsyncSessionLocal
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.services.safety_policy import SafetyPolicyViolation


class ReadOnlyBrokerGuard:
    """Proxy broker calls and fail closed if reconciliation attempts a write."""

    def __init__(self, broker: Trading212Adapter) -> None:
        self._broker = broker
        self.write_calls: list[str] = []
        self.environment = broker.environment

    def __getattr__(self, name: str) -> Any:
        if is_broker_write_method(name):
            self.write_calls.append(name)
            raise RuntimeError(f"Broker write method blocked during reconciliation smoke: {name}")
        return getattr(self._broker, name)


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _credential(preferred_name: str, fallback_name: str) -> str:
    preferred = os.getenv(preferred_name, "").strip()
    if preferred:
        return preferred
    return _env(fallback_name)


def _enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _require_safety_env() -> None:
    app_mode = os.getenv("APP_MODE", "demo").lower()
    t212_environment = os.getenv("T212_ENVIRONMENT", "demo").lower()

    if app_mode != "demo":
        raise SystemExit(f"Refusing to run: APP_MODE must be demo, got {app_mode!r}")
    if t212_environment != "demo":
        raise SystemExit(
            f"Refusing to run: T212_ENVIRONMENT must be demo, got {t212_environment!r}"
        )
    if _enabled("LIVE_TRADING_ENABLED"):
        raise SystemExit("Refusing to run: LIVE_TRADING_ENABLED must be false")
    if not _enabled("DEMO_RECONCILIATION_WORKER_ENABLED"):
        raise SystemExit(
            "Refusing to run: DEMO_RECONCILIATION_WORKER_ENABLED must be true "
            "for this controlled multi-order smoke."
        )
    if _enabled("DEMO_RECONCILIATION_SCHEDULER_ENABLED"):
        raise SystemExit(
            "Refusing to run: DEMO_RECONCILIATION_SCHEDULER_ENABLED must be false "
            "to avoid concurrent scheduler interference during this smoke."
        )


def _print_order_rows(order_results: list[dict[str, Any]]) -> None:
    if not order_results:
        print("Orders:")
        print("  none attempted")
        return

    print("Orders:")
    for index, item in enumerate(order_results, start=1):
        print(
            "  "
            f"{index}. local_order_id={item['order_id']} "
            f"broker_order_id={item['broker_order_id']} "
            f"ticker={item['ticker']} "
            f"previous_status={item['previous_status']} "
            f"broker_status={item['broker_status'] or 'unmatched'} "
            f"new_status={item['new_status']} "
            f"matched={str(item['matched']).lower()} "
            f"outcome={item['outcome']}"
        )


async def main() -> int:
    _require_safety_env()
    api_key = _credential("T212_DEMO_API_KEY", "T212_API_KEY")
    api_secret = _credential("T212_DEMO_API_SECRET", "T212_API_SECRET")

    print("Trading 212 DEMO multi-order reconciliation smoke")
    print("Mode: demo")
    print("Environment: demo")
    print("Live trading enabled: false")
    print("Scheduler enabled: false")
    print("Broker endpoint: GET /api/v0/equity/history/orders")
    print("Write endpoints: guarded and not expected")
    print("")

    guarded_broker: ReadOnlyBrokerGuard | None = None
    try:
        async with (
            AsyncSessionLocal() as db,
            Trading212Adapter(api_key, api_secret, "demo") as broker,
        ):
            guarded_broker = ReadOnlyBrokerGuard(broker)
            summary = await DemoReconciliationWorker(
                db,
                guarded_broker,
                actor="script:t212_demo_multi_order_reconciliation_smoke",
            ).run_once()
            await db.commit()
    except SafetyPolicyViolation as exc:
        print(f"Safety gate refused smoke run: {exc.reason}")
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
    except RuntimeError as exc:
        if guarded_broker is not None and guarded_broker.write_calls:
            print(f"Safety guard blocked broker write method: {exc}")
            print(f"Broker write calls: {','.join(guarded_broker.write_calls)}")
            return 6
        raise

    write_calls = guarded_broker.write_calls if guarded_broker is not None else []
    payload = to_jsonable(asdict(summary))
    payload["orders_considered"] = payload["candidates_found"]
    payload["no_broker_order_sent"] = summary.no_broker_order_sent and not write_calls
    payload["broker_write_calls"] = write_calls

    _print_order_rows(summary.order_results)
    print("")
    print("Aggregate:")
    print(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
    )

    if write_calls:
        print("")
        print("CHECK FAILED: broker write methods were attempted.")
        return 6
    if summary.outcome in {"completed", "completed_with_failures", "rate_limited"}:
        print("")
        print("PASS: multi-order reconciliation smoke completed without broker write calls.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
