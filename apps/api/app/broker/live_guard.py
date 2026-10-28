"""One-use task/adapter/request-bound admission at the actual send boundary."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class LiveSubmissionPermit:
    def __init__(
        self, adapter: Any, path: str, payload: dict[str, Any], check: Callable[[], Awaitable[None]]
    ) -> None:
        self.adapter = adapter
        self.task = asyncio.current_task()
        self.path = path
        self.payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.check = check
        self.used = False

    async def consume(self, adapter: Any, path: str, payload: dict[str, Any]) -> None:
        if self.used or asyncio.current_task() is not self.task or adapter is not self.adapter:
            raise RuntimeError("Live dispatch permit is unavailable for this task or transport.")
        if (
            path != self.path
            or json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            != self.payload
        ):
            raise RuntimeError("Live dispatch permit does not match this request.")
        self.used = True  # Failure cannot make this permit reusable.
        await self.check()


live_submission_guard: ContextVar[LiveSubmissionPermit | None] = ContextVar(
    "cashguard_live_submission_guard",
    default=None,
)
