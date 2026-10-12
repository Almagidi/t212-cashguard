"""How long reconciliation waits before it asks the broker about the same order again.

Pure functions. The state they work on is stored on the order itself: ``retry_count`` is the
number of attempts in a row that did not settle anything, and ``last_reconciled_at`` is when
the last attempt was recorded.
"""

from __future__ import annotations

import math

# Wait after the 1st, 2nd, ... attempt in a row that settled nothing. The last entry is the
# ceiling: an order is never left longer than this without another attempt.
BACKOFF_STEPS_SECONDS: tuple[int, ...] = (30, 60, 120, 240, 480, 900)
MAX_BACKOFF_LEVEL = len(BACKOFF_STEPS_SECONDS)
MAX_BACKOFF_SECONDS = BACKOFF_STEPS_SECONDS[-1]


def backoff_delay_seconds(level: int) -> int:
    """The wait that goes with a backoff level. Level 0 or below means no wait."""
    if level <= 0:
        return 0
    return BACKOFF_STEPS_SECONDS[min(level, MAX_BACKOFF_LEVEL) - 1]


def usable_retry_after_seconds(value: object) -> float | None:
    """A broker's Retry-After as a wait in seconds, or None when it cannot be trusted.

    Only a finite number above zero counts. It is capped at the longest backoff, so no
    header value can keep an order from being looked at again.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return float(MAX_BACKOFF_SECONDS) if value > 0 else None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, float(MAX_BACKOFF_SECONDS))


def level_after_unsettled_attempt(current_level: int | None, retry_after: object = None) -> int:
    """The backoff level after one more attempt that settled nothing.

    One step up, never past the ceiling. A usable Retry-After raises the level further,
    until the wait is at least as long as the broker asked for.
    """
    level = min(max(current_level or 0, 0) + 1, MAX_BACKOFF_LEVEL)
    wanted = usable_retry_after_seconds(retry_after)
    if wanted is None:
        return level
    while level < MAX_BACKOFF_LEVEL and backoff_delay_seconds(level) < wanted:
        level += 1
    return level
