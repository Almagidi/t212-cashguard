"""One owner at a time for order reconciliation.

Reconciliation is started from several places: the scheduled Celery task, the demo
reconciliation worker run by the in-process scheduler and by two operator routes, and a
terminal script for a single order. Each takes this one distributed lock before it reads
the broker and keeps it until its changes are committed, or discarded if the run fails.

What the lock does not do is listed in docs/RECONCILIATION_OWNERSHIP.md: it is a lease
without renewal, and other tasks change order state under their own locks.
"""

from __future__ import annotations

from app.core.config import settings

# The scheduled task has always used this name, so introducing the shared lock changes
# nothing for workers that are already running.
RECONCILIATION_OWNER_LOCK = "reconcile_pending_orders"
_LEASE_MARGIN_SECONDS = 30
_MAX_LEASE_SECONDS = 300


def reconciliation_run_limit_seconds() -> int:
    """The longest one demo reconciliation run may take before it is stopped."""
    return max(1, settings.DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS)


def reconciliation_owner_lease_seconds() -> int:
    """How long the owner lock lasts if its holder dies without releasing it.

    Longer than the run limit, so a live owner is stopped before its lease ends; capped,
    so a dead owner cannot block reconciliation for long whatever the settings say.
    """
    shortest = reconciliation_run_limit_seconds() + _LEASE_MARGIN_SECONDS
    longest = max(_MAX_LEASE_SECONDS, shortest)
    wanted = max(settings.DEMO_RECONCILIATION_SCHEDULER_LOCK_TTL_SECONDS, shortest)
    return min(wanted, longest)
