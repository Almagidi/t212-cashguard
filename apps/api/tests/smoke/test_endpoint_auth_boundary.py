"""Endpoint auth-boundary guard tests.

Pins the authentication boundary of every registered HTTP route so that:

1. The set of unauthenticated (public) routes is exact and cannot grow
   silently when a new router is added.
2. Every state-changing route (POST/PATCH/PUT/DELETE) requires
   authentication, with login and the Telegram webhook as the only
   pinned exceptions.
3. Safety-critical mutation routes (emergency controls, broker
   connection management, settings, kill switch, order creation) keep
   their admin-only requirement and cannot be downgraded to plain user
   auth.

These tests introspect the FastAPI dependency tree without starting a
server, touching the database, or requiring broker credentials. If a
route legitimately changes its auth level, the pinned sets below must be
updated in the same PR so the change is explicit and reviewable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute

from app.api.deps import get_current_admin, get_current_user
from app.main import app

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.dependencies.models import Dependant

# Routes that are intentionally reachable without authentication.
# Adding a route here requires explicit review: it widens the
# unauthenticated surface of the API.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/metrics"),
        ("POST", "/v1/auth/login"),
        ("GET", "/v1/health/deps"),
        ("GET", "/v1/health/live"),
        ("GET", "/v1/health/market-data"),
        ("GET", "/v1/health/ready"),
        ("GET", "/v1/health/startup"),
        ("GET", "/v1/health/workers"),
        # Telegram webhook authenticates via its own shared-secret check
        # inside the route body, not via the user/JWT dependency chain.
        ("POST", "/v1/telegram/webhook"),
    }
)

# State-changing routes that are allowed to skip the user/JWT dependency
# chain. Anything else mutating state without auth is a regression.
PUBLIC_STATE_CHANGING_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/telegram/webhook"),
    }
)

# Safety-critical mutation routes that must require admin auth.
# Downgrading any of these to plain user auth weakens a safety gate and
# must fail this suite until the pin is deliberately changed.
ADMIN_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/emergency/kill-switch"),
        ("POST", "/v1/emergency/auto-trading/off"),
        ("POST", "/v1/emergency/auto-trading/on"),
        ("POST", "/v1/emergency/cancel-all"),
        ("POST", "/v1/emergency/flatten-all"),
        ("POST", "/v1/broker/trading212/connect"),
        ("DELETE", "/v1/broker/trading212/disconnect"),
        ("POST", "/v1/broker/trading212/test"),
        ("POST", "/v1/broker/trading212/reconciliation/run-once"),
        ("POST", "/v1/broker/trading212/reconciliation/scheduler/run-once"),
        ("PATCH", "/v1/settings"),
        ("GET", "/v1/settings/live-readiness"),
        ("POST", "/v1/settings/live-readiness"),
        ("POST", "/v1/risk/kill-switch/enable"),
        ("POST", "/v1/risk/kill-switch/disable"),
        ("POST", "/v1/risk/daily-reset"),
        ("PATCH", "/v1/risk/profile"),
        ("POST", "/v1/orders"),
        ("POST", "/v1/orders/paper"),
        ("POST", "/v1/orders/{order_id}/cancel"),
        ("POST", "/v1/orders/cancel-all-pending"),
    }
)

_STATE_CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _dependency_calls(dependant: Dependant) -> set[Callable[..., object]]:
    calls: set[Callable[..., object]] = set()
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


def _http_routes() -> dict[tuple[str, str], APIRoute]:
    routes: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - _IGNORED_METHODS:
            routes[(method, route.path)] = route
    return routes


ROUTES = _http_routes()
ROUTE_IDS = [f"{method} {path}" for method, path in sorted(ROUTES)]


def _requires_user_auth(route: APIRoute) -> bool:
    calls = _dependency_calls(route.dependant)
    return get_current_user in calls or get_current_admin in calls


def _requires_admin_auth(route: APIRoute) -> bool:
    return get_current_admin in _dependency_calls(route.dependant)


def test_public_route_set_is_exact() -> None:
    """The unauthenticated surface must match the pinned allowlist exactly."""
    actual_public = {key for key, route in ROUTES.items() if not _requires_user_auth(route)}
    unexpected = actual_public - PUBLIC_ROUTES
    missing = PUBLIC_ROUTES - set(ROUTES)
    assert not unexpected, (
        "Routes reachable without authentication that are not in the pinned "
        f"PUBLIC_ROUTES allowlist: {sorted(unexpected)}. If this is "
        "deliberate, update the allowlist in this test with review."
    )
    assert not missing, (
        "Pinned public routes that no longer exist (stale allowlist entry "
        f"or accidentally removed route): {sorted(missing)}."
    )


@pytest.mark.parametrize("method,path", sorted(ROUTES), ids=ROUTE_IDS)
def test_route_auth_boundary(method: str, path: str) -> None:
    """Every route outside the public allowlist must require auth."""
    route = ROUTES[(method, path)]
    if (method, path) in PUBLIC_ROUTES:
        return
    assert _requires_user_auth(route), (
        f"{method} {path} has no get_current_user/get_current_admin in its "
        "dependency tree and is not in PUBLIC_ROUTES. All non-public routes "
        "must be authenticated."
    )


def test_state_changing_routes_require_auth() -> None:
    """No mutation endpoint may be public except login and the webhook."""
    unauthenticated_mutations = {
        (method, path)
        for (method, path), route in ROUTES.items()
        if method in _STATE_CHANGING_METHODS
        and not _requires_user_auth(route)
        and (method, path) not in PUBLIC_STATE_CHANGING_ROUTES
    }
    assert not unauthenticated_mutations, (
        "State-changing routes reachable without authentication: "
        f"{sorted(unauthenticated_mutations)}."
    )


@pytest.mark.parametrize(
    "method,path",
    sorted(ADMIN_ONLY_ROUTES),
    ids=[f"{method} {path}" for method, path in sorted(ADMIN_ONLY_ROUTES)],
)
def test_safety_critical_routes_require_admin(method: str, path: str) -> None:
    """Safety-critical mutations must keep admin-only protection."""
    route = ROUTES.get((method, path))
    assert route is not None, (
        f"Pinned admin-only route {method} {path} is no longer registered. "
        "If it was intentionally removed, update ADMIN_ONLY_ROUTES."
    )
    assert _requires_admin_auth(route), (
        f"{method} {path} no longer requires get_current_admin. "
        "Downgrading a safety-critical route to user auth weakens a safety "
        "gate and must be an explicit, reviewed decision."
    )
