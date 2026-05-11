"""Trading 212 demo read-only smoke test.

This script intentionally performs GET/read-only broker calls only:
- test connection
- account summary
- positions
- recent historical orders

It must never submit, cancel, or modify an order.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.broker.trading212 import (
    T212APIError,
    T212AuthError,
    T212RateLimitError,
    Trading212Adapter,
)


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _live_enabled() -> bool:
    return os.getenv("LIVE_TRADING_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def _numberish(value: Any) -> str:
    if isinstance(value, int | float | str):
        return str(value)
    return "unavailable"


def _summary_line(summary: dict[str, Any]) -> str:
    cash = summary.get("cash")
    if isinstance(cash, dict):
        free = cash.get("free") or cash.get("availableToTrade") or cash.get("available")
        total = cash.get("total") or summary.get("total")
    else:
        free = summary.get("free") or summary.get("availableToTrade") or cash
        total = summary.get("total") or summary.get("totalValue") or cash

    currency = summary.get("currency") or summary.get("currencyCode") or "unknown"
    return f"currency={currency}, total={_numberish(total)}, free={_numberish(free)}"


async def main() -> int:
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

    api_key = _env("T212_API_KEY")
    api_secret = _env("T212_API_SECRET")

    print("Trading 212 demo read-only smoke")
    print("Mode: demo")
    print("Environment: demo")
    print("Live trading enabled: false")
    print("Write endpoints: not called")
    print("")

    try:
        async with Trading212Adapter(api_key, api_secret, "demo") as broker:
            print("1. Testing broker connection...")
            test = await broker.test_connection()
            print(f"   is_ok={test.get('is_ok')}")
            print(f"   account_id_present={bool(test.get('account_id'))}")
            print(f"   currency={test.get('currency') or 'unknown'}")

            if not test.get("is_ok"):
                print(f"   error={test.get('error')}")
                return 1

            print("2. Reading account summary...")
            await asyncio.sleep(6)
            summary = await broker.get_account_summary()
            print(f"   {_summary_line(summary)}")

            print("3. Reading open positions...")
            await asyncio.sleep(6)
            positions = await broker.get_positions()
            tickers = [
                str(item.get("ticker") or item.get("instrumentCode") or "unknown")
                for item in positions[:5]
                if isinstance(item, dict)
            ]
            print(f"   positions_count={len(positions)}")
            print(f"   sample_tickers={tickers}")

            print("4. Reading recent historical orders...")
            await asyncio.sleep(10)
            historical_orders = await broker._request_dict(
                "GET",
                "/api/v0/equity/history/orders",
                params={"limit": 5},
            )
            items = historical_orders.get("items") or historical_orders.get("data") or []
            if not isinstance(items, list):
                items = []
            print(f"   recent_orders_count={len(items)}")

    except T212RateLimitError as exc:
        print(f"Rate limited by Trading 212 demo API. Retry after {exc.retry_after}s.")
        return 2
    except T212AuthError as exc:
        print(f"Trading 212 authentication failed: HTTP {exc.status_code}")
        print("Check that the key/secret are for the Trading 212 DEMO environment.")
        return 3
    except T212APIError as exc:
        print(f"Trading 212 API error: HTTP {exc.status_code}")
        print(exc.body)
        return 4

    print("")
    print("PASS: Trading 212 demo read-only smoke completed without write calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
