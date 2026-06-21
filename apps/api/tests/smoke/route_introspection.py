"""Version-aware FastAPI route introspection for smoke tests.

FastAPI 0.137 changed how included routers are stored on the application,
which silently broke route-table introspection in our smoke tests. This
module centralises a single, version-aware traversal so the auth-boundary
and route-registration suites keep asserting the exact same safety guards on
both FastAPI versions.

Why the ``_IncludedRouter`` handling exists
-------------------------------------------
Up to and including FastAPI 0.136.x, ``app.include_router(...)`` eagerly
*flattened* the included routes into ``app.routes`` with their full,
prefixed paths. A single pass over ``app.routes`` reading ``route.path`` /
``route.methods`` therefore yielded every endpoint.

Starting in FastAPI 0.137.x, includes are *deferred*: ``app.routes`` instead
holds ``fastapi.routing._IncludedRouter`` wrapper objects. Each wrapper
exposes:

* ``include_context.prefix`` -- the prefix supplied at ``include_router``
  time (e.g. ``"/v1"``), relative to the parent router.
* ``original_router.routes`` -- the actual ``APIRoute`` objects. Each
  ``APIRoute.path`` already contains the router's own prefix
  (e.g. ``"/auth/login"``) but **not** the include-time prefix.

The full external path is therefore the accumulation of every enclosing
``include_context.prefix`` followed by the leaf ``route.path``. Wrappers can
nest (a router that itself includes another router), so resolution must
recurse and carry the accumulated prefix down through each level.

Design guarantees
-----------------
* On FastAPI <=0.136.x there are no ``_IncludedRouter`` wrappers, so the walk
  degenerates to a flat pass over ``app.routes`` and returns exactly the same
  ``(method, full_path) -> APIRoute`` mapping as the previous inline logic.
* We intentionally do **not** use ``app.openapi()``. The OpenAPI schema omits
  the dependency tree, but the auth-boundary tests must inspect the real
  ``APIRoute.dependant`` graph to assert admin/user dependencies. This helper
  preserves access to the live ``APIRoute`` objects so those assertions keep
  working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from starlette.routing import BaseRoute

# FastAPI 0.137.x wraps each included router in this private type. We match on
# the class name rather than importing it because it is an internal symbol that
# does not exist on FastAPI <=0.136.x; matching by name keeps this helper
# importable and correct on both versions.
_INCLUDED_ROUTER_TYPE_NAME = "_IncludedRouter"

_DEFAULT_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _iter_with_prefix(
    routes: list[BaseRoute],
    prefix: str,
) -> Iterator[tuple[str, BaseRoute]]:
    """Yield ``(full_path, route)`` for every leaf route under ``routes``.

    ``prefix`` is the accumulated include-time prefix from all enclosing
    ``_IncludedRouter`` wrappers. For a deferred include (FastAPI 0.137.x) we
    descend into the wrapped router, extending the accumulated prefix with the
    wrapper's ``include_context.prefix`` so that nested includes resolve to the
    correct full external path.
    """
    for route in routes:
        if type(route).__name__ == _INCLUDED_ROUTER_TYPE_NAME:
            included_prefix = prefix + route.include_context.prefix
            yield from _iter_with_prefix(route.original_router.routes, included_prefix)
        else:
            path = getattr(route, "path", None)
            if path is not None:
                yield prefix + path, route


def iter_http_routes(app: FastAPI) -> Iterator[tuple[str, BaseRoute]]:
    """Yield ``(full_path, route)`` for every leaf route in ``app``.

    Works identically on FastAPI <=0.136.x (flat ``app.routes``) and 0.137.x
    (deferred ``_IncludedRouter`` wrappers).
    """
    yield from _iter_with_prefix(app.routes, "")


def build_http_route_map(
    app: FastAPI,
    *,
    ignored_methods: frozenset[str] = _DEFAULT_IGNORED_METHODS,
) -> dict[tuple[str, str], APIRoute]:
    """Map ``(method, full_path)`` to the live ``APIRoute`` object.

    Preserving the ``APIRoute`` (rather than collapsing to OpenAPI metadata)
    is what lets the auth-boundary suite walk ``route.dependant`` and assert
    admin/user dependencies.
    """
    routes: dict[tuple[str, str], APIRoute] = {}
    for full_path, route in iter_http_routes(app):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - ignored_methods:
            routes[(method, full_path)] = route
    return routes


def registered_route_methods(app: FastAPI) -> set[tuple[str, str]]:
    """Return the set of ``(method, full_path)`` for every HTTP route.

    Mirrors the pre-0.137 flat ``app.routes`` scan used by the route
    registration smoke test, but resolves full paths through deferred includes.
    """
    result: set[tuple[str, str]] = set()
    for full_path, route in iter_http_routes(app):
        methods = getattr(route, "methods", None) or set()
        if full_path:
            for method in methods:
                result.add((method, full_path))
    return result
