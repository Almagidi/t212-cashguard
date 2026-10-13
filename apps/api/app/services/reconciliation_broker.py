"""The broker as reconciliation is allowed to see it: two reads and no writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError

if TYPE_CHECKING:
    from collections.abc import Callable

# 4xx answers that say "not now" rather than "not this request".
_TRY_AGAIN_STATUSES = frozenset({408, 425})
# One broker read may take this long before it counts as failed.
BROKER_READ_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class AccountBroker:
    """A broker adapter together with the account scope its provider bound it to."""

    broker: Any
    account_scope: str | None


class ReadOnlyBroker:
    """The two broker reads reconciliation needs, and nothing else.

    Each read has a time limit, and a failed read is remembered so the caller can tell a
    broker that did not answer from an order that did not change. No other method of the
    adapter can be reached through this object.
    """

    def __init__(self, adapter: Any, *, account_scope: str | None) -> None:
        self._adapter = adapter
        self.account_scope = account_scope
        self.environment: str | None = getattr(adapter, "environment", None)
        self.reads = 0
        self.failure: Exception | None = None
        self.failed_reads_in_a_row = 0
        self.stopped: str | None = None

    def begin_order(self) -> None:
        self.reads = 0
        self.failure = None

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        return await self._read(lambda: self._adapter.get_order_by_id(broker_order_id))

    async def get_historical_orders(self, **kwargs: Any) -> Any:
        return await self._read(lambda: self._adapter.get_historical_orders(**kwargs))

    async def _read(self, call: Callable[[], Any]) -> Any:
        self.reads += 1
        try:
            async with asyncio.timeout(BROKER_READ_TIMEOUT_SECONDS):
                return await call()
        except Exception as exc:
            self.failure = exc
            raise


def failure_outcome(failure: Exception) -> str:
    if isinstance(failure, T212RateLimitError):
        return "rate_limited"
    if isinstance(failure, T212AuthError):
        return "broker_auth_failed"
    if (
        isinstance(failure, T212APIError)
        and 400 <= failure.status_code < 500
        and failure.status_code not in _TRY_AGAIN_STATUSES
    ):
        # The broker answered and refused this one request (for example: no such order).
        return "broker_rejected_read"
    return "broker_read_failed"
