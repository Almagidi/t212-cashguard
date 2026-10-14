"""What a cancellation may claim, and how the reads that confirm one are spent.

A broker accepting a cancel request says neither that the order ended nor how much of it
filled first. Only the broker's final record of the order says that, so every accepted
request is followed by a read of the order (see ``ExecutionEngine.confirm_cancel``).

Those reads share the broker's rate limit with the cancel requests themselves: a read that
is answered "too many requests" makes the adapter hold its next request, whatever it is. A
batch therefore sends every cancel request first and reads afterwards, within one time limit,
and stops reading at the first read that fails. An order left unread is reported as awaiting
confirmation: it stays open, and the scheduled reconciler settles it from the record.

No database and no broker calls here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.broker.trading212 import T212AuthError, T212RateLimitError

if TYPE_CHECKING:
    from app.db.models import Order

# Broker statuses this code takes to mean "open, and no cancellation under way". A repeated
# cancel is sent again only for these; any other open status (CANCELLING, one this code does
# not know, none at all) is waited out, not asked again.
WORKING_STATUSES = frozenset({"WORKING", "PENDING"})

# How long the reads after a single cancel, or after a whole batch, may take together.
SINGLE_CANCEL_READ_SECONDS = 10.0
BATCH_CANCEL_READ_SECONDS = 10.0

# Fixed words for why an accepted cancel request is not yet confirmed (with the reasons
# ``broker_record`` gives for a record it refuses, and ``no_broker_record``).
STILL_OPEN = "still_open_at_broker"
READ_FAILED = "broker_read_failed"
RATE_LIMITED = "rate_limited"
AUTH_FAILED = "broker_auth_failed"
TIME_SPENT = "read_time_spent"
NOT_READ = "not_read_after_a_failed_read"
# The broker still listed the order as working after an earlier accepted request: asked again.
ASKED_AGAIN = "asked_again_still_working"
# The order's record could not be written here (a local fault); it is left as it was.
LOCAL_ERROR = "local_error"


def read_failure_reason(failure: BaseException) -> str:
    """The fixed word for a read of the broker that did not get an answer."""
    if isinstance(failure, T212RateLimitError):
        return RATE_LIMITED
    if isinstance(failure, T212AuthError):
        return AUTH_FAILED
    if isinstance(failure, TimeoutError):
        return TIME_SPENT
    return READ_FAILED


@dataclass
class CancelReads:
    """The reads that confirm cancel requests: one time limit for all of them, and none after
    one has failed (a rate limit would otherwise hold every later request)."""

    deadline: float
    stopped_because: str | None = None

    @classmethod
    def within(cls, seconds: float) -> CancelReads:
        return cls(deadline=asyncio.get_running_loop().time() + seconds)

    def seconds_left(self) -> float:
        return self.deadline - asyncio.get_running_loop().time()


@dataclass
class CancelBatch:
    """What became of each order in a batch of cancellations."""

    cancelled: list[Order] = field(default_factory=list)
    # Ended another way before the cancellation took effect: filled or rejected (a fill
    # among them is exposure the cancel did not prevent).
    finished: list[Order] = field(default_factory=list)
    # The broker took the request and has not said what became of the order: still open.
    unconfirmed: list[tuple[Order, str]] = field(default_factory=list)
    # The broker refused the request, or it could not be sent: still open.
    failed: list[Order] = field(default_factory=list)

    @property
    def partly_filled(self) -> list[Order]:
        """Cancellations that came after part of the order had filled."""
        return [order for order in self.cancelled if order.filled_quantity]
