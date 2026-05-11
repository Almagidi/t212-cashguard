"""Route registration smoke test.

Verifies that operator and Kraken DCA routes are present in the FastAPI
application's route table without starting the server, connecting to any
database, or requiring broker credentials.

Fails fast if a router is accidentally removed from app/main.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.main import app

# The four routes introduced in T-OPS-006 that must never silently disappear.
REQUIRED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/v1/operator/status"),
    ("GET", "/v1/kraken/dca/status"),
    ("GET", "/v1/kraken/dca/activity"),
    ("GET", "/v1/kraken/dca/configs"),
]


def _registered_routes() -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if path:
            for method in methods:
                result.add((method, path))
    return result


@pytest.mark.parametrize("method,path", REQUIRED_ROUTES, ids=[p for _, p in REQUIRED_ROUTES])
def test_required_route_is_registered(method: str, path: str) -> None:
    routes = _registered_routes()
    assert (method, path) in routes, (
        f"Route {method} {path} is not registered in the FastAPI app. "
        "Check that the router is imported and included in app/main.py."
    )


def test_legacy_all_routes_module_is_removed() -> None:
    route_dir = Path(__file__).resolve().parents[2] / "app" / "api" / "v1" / "routes"
    assert not (route_dir / "all_routes.py").exists(), (
        "Legacy all_routes.py must stay removed; it contained stale direct broker-selection "
        "logic that could bypass focused route safety policy if re-registered."
    )
