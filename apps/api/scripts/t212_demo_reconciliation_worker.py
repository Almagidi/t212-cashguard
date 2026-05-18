"""Run one Trading 212 DEMO reconciliation worker pass.

This script performs read-only broker calls only:
- GET /api/v0/equity/history/orders

It never submits, cancels, modifies, deposits, or withdraws anything.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

from app.broker.provider import (
    BrokerProviderCredentials,
    BrokerProviderRequest,
    BrokerProviderValidationError,
    create_trading212_provider_adapter,
)
from app.core.serialization import to_jsonable
from app.db.session import AsyncSessionLocal
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.services.safety_policy import SafetyPolicyViolation


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _require_safety_env() -> tuple[str, bool]:
    app_mode = os.getenv("APP_MODE", "demo").lower()
    t212_environment = os.getenv("T212_ENVIRONMENT", "demo").lower()
    live_trading_enabled = _enabled("LIVE_TRADING_ENABLED")

    if app_mode != "demo":
        raise SystemExit(f"Refusing to run: APP_MODE must be demo, got {app_mode!r}")
    if t212_environment != "demo":
        raise SystemExit(
            f"Refusing to run: T212_ENVIRONMENT must be demo, got {t212_environment!r}"
        )
    if live_trading_enabled:
        raise SystemExit("Refusing to run: LIVE_TRADING_ENABLED must be false")
    if not _enabled("DEMO_RECONCILIATION_WORKER_ENABLED"):
        raise SystemExit(
            "Refusing to run: DEMO_RECONCILIATION_WORKER_ENABLED must be true "
            "for this controlled one-shot worker pass."
        )
    return app_mode, live_trading_enabled


async def main() -> int:
    app_mode, live_trading_enabled = _require_safety_env()
    api_key = _env("T212_API_KEY")
    api_secret = _env("T212_API_SECRET")

    print("Trading 212 DEMO reconciliation worker")
    print("Mode: demo")
    print("Environment: demo")
    print("Live trading enabled: false")
    print("Broker endpoint: GET /api/v0/equity/history/orders")
    print("Write endpoints: not called")
    print("")

    try:
        broker = create_trading212_provider_adapter(
            BrokerProviderRequest(
                broker_id="trading212",
                environment="demo",
                purpose="demo_reconciliation",
            ),
            BrokerProviderCredentials(api_key=api_key, api_secret=api_secret),
            app_mode=app_mode,
            live_trading_enabled=live_trading_enabled,
        )
        async with (
            AsyncSessionLocal() as db,
            broker,
        ):
            summary = await DemoReconciliationWorker(
                db,
                broker,
                actor="script:t212_demo_reconciliation_worker",
            ).run_once()
            await db.commit()
    except BrokerProviderValidationError as exc:
        print(f"Safety gate refused worker run: {exc}")
        return 2
    except SafetyPolicyViolation as exc:
        print(f"Safety gate refused worker run: {exc.reason}")
        return 2

    print(
        json.dumps(
            to_jsonable(asdict(summary)),
            indent=2,
            sort_keys=True,
        )
    )
    if summary.outcome in {"completed", "completed_with_failures", "rate_limited"}:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
