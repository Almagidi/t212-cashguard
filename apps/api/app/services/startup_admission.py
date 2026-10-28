"""A lifespan-bound capability for HTTP AND WebSocket admission."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

ADMISSION_KEY = "cashguard_startup_admission"


@dataclass(eq=False)
class AdmissionLease:
    pid: int
    loop: asyncio.AbstractEventLoop
    state: str = "UNVALIDATED"
    detail: str = "Startup validation has not completed."
    revoked: bool = False


class StartupAdmission:
    def __init__(self) -> None:
        self._owner: AdmissionLease | None = None
        self._lock = threading.Lock()

    def begin(self) -> AdmissionLease:
        with self._lock:
            if self._owner is not None and self._owner.pid == os.getpid():
                raise RuntimeError("Another application lifespan already owns startup admission")
            lease = AdmissionLease(os.getpid(), asyncio.get_running_loop())
            self._owner = lease
            return lease

    def publish(self, lease: AdmissionLease) -> None:
        if not self.owns(lease):
            raise RuntimeError("Startup lifespan no longer owns admission")
        lease.state, lease.detail = "READY_UNARMED", "Startup permission review passed."

    def owns(self, lease: object) -> bool:
        return (
            isinstance(lease, AdmissionLease)
            and lease is self._owner
            and not lease.revoked
            and lease.pid == os.getpid()
            and lease.loop is asyncio.get_running_loop()
        )

    def close(self, lease: AdmissionLease) -> None:
        with self._lock:
            lease.revoked = True
            lease.state = "UNVALIDATED"
            if self._owner is lease:
                self._owner = None


class StartupAdmissionMiddleware:
    def __init__(self, app: ASGIApp, admission: StartupAdmission) -> None:
        self.app, self.admission = app, admission

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] == "/v1/health/live":
            await self.app(scope, receive, send)
            return
        lease = scope.get("state", {}).get(ADMISSION_KEY)
        owned = self.admission.owns(lease)
        if owned and lease.state == "READY_UNARMED":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send(
                {"type": "websocket.close", "code": 1013, "reason": "Startup review required"}
            )
            return
        await JSONResponse(
            status_code=503,
            content={
                "detail": lease.detail if owned else "Startup validation has not completed.",
                "startup_state": lease.state if owned else "UNVALIDATED",
            },
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)
