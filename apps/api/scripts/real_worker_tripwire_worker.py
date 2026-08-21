"""Launch a real Celery worker with fail-fast forbidden-broker tripwires."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import ipaddress
import json
import math
import os
import socket
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_ORIGINAL_SOCKET_CONNECT = socket.socket.connect


def _guarded_connect(sock: socket.socket, address: Any) -> None:
    if isinstance(address, tuple):
        host = str(address[0])
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback:
            raise AssertionError(f"FORBIDDEN_NETWORK:{host}")
    _ORIGINAL_SOCKET_CONNECT(sock, address)


def _forbid(marker: str) -> Callable[..., NoReturn]:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError(marker)

    return fail


def _finite_positive(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be finite positive") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be finite positive")
    return parsed


def _validate_lock_hold(app_mode: str, *, hold_seconds: float, ttl_seconds: int | None) -> None:
    if app_mode.lower() != "mock":
        raise ValueError("harness lock hold requires APP_MODE=mock")
    if ttl_seconds is not None and ttl_seconds <= hold_seconds:
        raise ValueError("harness lock lease must outlive the injected hold")


def _owned_token_factory(
    worker_id: str, original: Callable[[int], str]
) -> tuple[Callable[[int], str], dict[str, str]]:
    state: dict[str, str] = {}

    def token_urlsafe(size: int) -> str:
        token = f"{worker_id}:{original(size)}"
        state["current"] = token
        return token

    return token_urlsafe, state


def _install_harness_lock_hold(
    *, worker_id: str, hold_seconds: float, ttl_seconds: int | None
) -> None:
    """Install a process-local lock hold used only by the disposable D1 harness."""
    from app.core import redis as redis_core

    original_task_lock = redis_core.task_lock
    token_factory, token_state = _owned_token_factory(worker_id, redis_core.secrets.token_urlsafe)
    redis_core.secrets.token_urlsafe = token_factory

    @contextlib.asynccontextmanager
    async def held_task_lock(name: str, ttl_seconds: int) -> AsyncIterator[bool]:
        effective_ttl = ttl_seconds
        if name == "run_strategy_signals" and ttl_seconds_override is not None:
            effective_ttl = ttl_seconds_override
        async with original_task_lock(name, effective_ttl) as acquired:
            if acquired and name == "run_strategy_signals":
                client = redis_core.aioredis.Redis(connection_pool=redis_core._get_pool())
                token = await client.get(f"celery:task_lock:{name}")
                owned_token = token_state.get("current")
                if not owned_token or token != owned_token:
                    raise AssertionError("CASHGUARD_HARNESS_LOCK_OWNER_MISMATCH")
                print(
                    "CASHGUARD_HARNESS_LOCK_OWNER "
                    + json.dumps(
                        {
                            "pid": os.getpid(),
                            "task": name,
                            "token": owned_token,
                            "worker": worker_id,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                await asyncio.sleep(hold_seconds)
            yield acquired

    ttl_seconds_override = ttl_seconds
    redis_core.task_lock = held_task_lock


def _install_precommit_fault() -> None:
    """Raise once immediately before the task's outer database commit."""
    from app.workers import tasks

    original_complete_task = tasks._complete_task
    fired = False

    async def fail_once(db: Any, task_name: str, summary: dict[str, Any]) -> dict[str, Any]:
        nonlocal fired
        if task_name == "run_strategy_signals" and not fired:
            fired = True
            print(
                "CASHGUARD_HARNESS_PRECOMMIT_FAULT "
                + json.dumps({"pid": os.getpid(), "task": task_name}, sort_keys=True),
                flush=True,
            )
            raise RuntimeError("CASHGUARD_DETERMINISTIC_PRECOMMIT_FAULT")
        return await original_complete_task(db, task_name, summary)

    tasks._complete_task = fail_once


def _install_mock_bar_offset(offset_minutes: int) -> None:
    """Shift deterministic mock bars to prove a genuinely new decision key."""
    from app.market_data.mock_provider import MockMarketDataProvider

    original_bars = MockMarketDataProvider._orb_breakout_bars

    def shifted_bars(self: Any, ticker: str, *, interval_minutes: int, bars: int) -> Any:
        rows = original_bars(self, ticker, interval_minutes=interval_minutes, bars=bars)
        for row in rows:
            timestamp = datetime.fromisoformat(str(row["timestamp"]))
            row["timestamp"] = (timestamp + timedelta(minutes=offset_minutes)).isoformat()
        return rows

    MockMarketDataProvider._orb_breakout_bars = shifted_bars


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--queue")
    parser.add_argument("--worker-id")
    parser.add_argument("--lock-hold-seconds")
    parser.add_argument("--lock-ttl-seconds")
    parser.add_argument("--pool", choices=("solo", "prefork"), default="solo")
    parser.add_argument("--precommit-fault-once", action="store_true")
    parser.add_argument("--mock-bar-offset-minutes")
    args = parser.parse_args()
    hostname = args.hostname
    modules = {
        "t212": importlib.import_module("app.broker.trading212"),
        "kraken": importlib.import_module("app.broker.kraken"),
        "kraken_market": importlib.import_module("app.market_data.kraken_provider"),
        "provider": importlib.import_module("app.broker.provider"),
        "runner": importlib.import_module("app.services.strategy_runner"),
    }
    modules["t212"].Trading212Adapter = _forbid("FORBIDDEN_CONSTRUCTION:Trading212Adapter")
    modules["kraken"].KrakenAdapter = _forbid("FORBIDDEN_CONSTRUCTION:KrakenAdapter")
    modules["kraken_market"].KrakenMarketDataProvider = _forbid(
        "FORBIDDEN_CONSTRUCTION:KrakenMarketDataProvider"
    )
    provider_tripwire = _forbid("FORBIDDEN_CONSTRUCTION:create_trading212_provider_adapter")
    modules["provider"].create_trading212_provider_adapter = provider_tripwire
    modules["runner"].create_trading212_provider_adapter = provider_tripwire
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    if args.lock_hold_seconds is not None:
        if not args.worker_id:
            parser.error("lock hold requires --worker-id")
        hold_seconds = _finite_positive(args.lock_hold_seconds, "lock hold")
        ttl_seconds = None
        if args.lock_ttl_seconds is not None:
            ttl_value = _finite_positive(args.lock_ttl_seconds, "lock ttl")
            if not ttl_value.is_integer():
                parser.error("lock ttl must be a whole number of seconds")
            ttl_seconds = int(ttl_value)
        try:
            _validate_lock_hold(
                os.environ.get("APP_MODE", ""),
                hold_seconds=hold_seconds,
                ttl_seconds=ttl_seconds,
            )
        except ValueError as exc:
            parser.error(str(exc))
        _install_harness_lock_hold(
            worker_id=args.worker_id,
            hold_seconds=hold_seconds,
            ttl_seconds=ttl_seconds,
        )
    if args.precommit_fault_once:
        if os.environ.get("APP_MODE", "").lower() != "mock":
            parser.error("pre-commit fault requires APP_MODE=mock")
        _install_precommit_fault()
    if args.mock_bar_offset_minutes is not None:
        if os.environ.get("APP_MODE", "").lower() != "mock":
            parser.error("mock bar offset requires APP_MODE=mock")
        try:
            offset = float(args.mock_bar_offset_minutes)
        except ValueError:
            parser.error("mock bar offset must be a non-zero whole number of minutes")
        if (
            not math.isfinite(offset)
            or not offset.is_integer()
            or offset == 0
            or abs(offset) > 1440
        ):
            parser.error("mock bar offset must be a non-zero whole number of minutes")
        _install_mock_bar_offset(int(offset))
    print(
        "CASHGUARD_BROKER_TRIPWIRES_ARMED CASHGUARD_NETWORK_TRIPWIRE_ARMED",
        flush=True,
    )

    from app.workers.celery_app import celery_app

    worker_args = [
        "worker",
        "--loglevel=INFO",
        f"--pool={args.pool}",
        "--concurrency=1",
        "--prefetch-multiplier=1",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        f"--hostname={hostname}",
    ]
    if args.queue:
        worker_args.append(f"--queues={args.queue}")
    celery_app.worker_main(worker_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
