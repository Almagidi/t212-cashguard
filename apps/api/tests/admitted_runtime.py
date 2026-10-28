"""Explicit post-startup model for endpoint-only tests, never production startup proof."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.main import admission, app
from app.services.startup_admission import ADMISSION_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import ASGIApp, Receive, Scope, Send


@asynccontextmanager
async def admitted_runtime() -> AsyncIterator[ASGIApp]:
    """Endpoint fixtures have isolated/unmigrated SQLite or deliberately faulty DBs.

    Model ONLY their already-validated runtime. Real lifecycle tests use main.app
    directly and may never use this helper.
    """
    lease = admission.begin()
    admission.publish(lease)

    async def request(scope: Scope, receive: Receive, send: Send) -> None:
        scope = {**scope, "state": {**scope.get("state", {}), ADMISSION_KEY: lease}}
        await app(scope, receive, send)

    try:
        yield request
    finally:
        admission.close(lease)
