"""Launch a real Celery worker with fail-fast forbidden-broker tripwires."""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import socket
from typing import Any, NoReturn

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


def _forbid(marker: str):
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError(marker)

    return fail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hostname", required=True)
    hostname = parser.parse_args().hostname
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
    print(
        "CASHGUARD_BROKER_TRIPWIRES_ARMED CASHGUARD_NETWORK_TRIPWIRE_ARMED",
        flush=True,
    )

    from app.workers.celery_app import celery_app

    celery_app.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--pool=solo",
            "--concurrency=1",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            f"--hostname={hostname}",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
