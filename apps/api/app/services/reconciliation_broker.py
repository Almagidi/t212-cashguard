"""The broker as reconciliation is allowed to see it: two reads and no writes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.execution.broker_record import says_no_such_order

if TYPE_CHECKING:
    from collections.abc import Callable

# 4xx answers that say "not now" rather than "not this request".
_TRY_AGAIN_STATUSES = frozenset({408, 425})
# The broker reads one order takes may together take this long before the read that is
# under way counts as failed. One limit for the order, not one per read: an order that needs
# the history as well cannot hold a run up for longer than an order that does not.
BROKER_READ_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class AccountBroker:
    """A broker adapter together with the account scope its provider bound it to."""

    broker: Any
    account_scope: str | None


class ReadOnlyBroker:
    """The two broker reads reconciliation needs, and nothing else.

    The reads for one order share a time limit, and a failed read is remembered so the
    caller can tell a broker that did not answer from an order that did not change. "No
    such open order" is an answer and is not remembered as a failure: an order that has
    ended is no longer among the open orders, and the history is where it is read next.

    A history page is read from the broker once in the life of this object, which is one
    run, and then kept: the orders of a run that ended lately are all on the same page,
    and so are the unknown submissions of one instrument. A kept page can be as old as
    the run (about 40 seconds at most). It can only lack a record, never state a wrong
    one, and an order whose record it lacks is asked about again on a later run.

    No other method of the adapter can be reached through this object.
    """

    def __init__(self, adapter: Any, *, account_scope: str | None) -> None:
        self._adapter = adapter
        self.account_scope = account_scope
        self.environment: str | None = getattr(adapter, "environment", None)
        self.reads = 0
        self.failure: Exception | None = None
        self.failed_reads_in_a_row = 0
        self.stopped: str | None = None
        self._history_pages: dict[tuple[tuple[str, Any], ...], Any] = {}
        self._first_read_at: float | None = None

    def begin_order(self) -> None:
        self.reads = 0
        self.failure = None
        self._first_read_at = None

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        try:
            return await self._read(lambda: self._adapter.get_order_by_id(broker_order_id))
        except Exception as exc:
            if says_no_such_order(exc) and self.failure is exc:
                self.failure = None
            raise

    async def get_historical_orders(self, **kwargs: Any) -> Any:
        asked = tuple(sorted(kwargs.items()))
        if asked in self._history_pages:
            # Answered from the page this run already holds; it still counts as a look.
            self.reads += 1
            return self._history_pages[asked]
        page = await self._read(lambda: self._adapter.get_historical_orders(**kwargs))
        self._history_pages[asked] = page
        return page

    async def _read(self, call: Callable[[], Any]) -> Any:
        self.reads += 1
        # The limit runs from the order's first read, not from when its handling began:
        # work done before the broker is asked does not eat into it.
        if self._first_read_at is None:
            self._first_read_at = time.monotonic()
        left = BROKER_READ_TIMEOUT_SECONDS - (time.monotonic() - self._first_read_at)
        try:
            async with asyncio.timeout(max(left, 0.0)):
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
