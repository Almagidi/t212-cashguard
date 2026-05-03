"""Celery configuration with all periodic tasks."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure

from app.core.config import settings

celery_app = Celery(
    "cashguard",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
    include=["app.workers.tasks", "app.workers.tasks_dca", "app.workers.tasks_heartbeat"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Re-queue any task that was in-flight when the worker process died
    # (e.g. SIGKILL after Docker stop_grace_period expires).
    task_reject_on_worker_lost=True,
    # Cancel pending tasks immediately if the broker connection is lost so
    # they don't linger in an unacknowledged state.
    worker_cancel_long_running_tasks_on_connection_loss=True,
    beat_schedule={
        # Strategy signals — every 5 minutes during market hours
        "strategy-signals": {
            "task": "app.workers.tasks.run_strategy_signals",
            "schedule": 300.0,  # 5 minutes
        },
        # Portfolio rebalance automation — every 15 minutes; service decides when due
        "portfolio-rebalance": {
            "task": "app.workers.tasks.run_portfolio_rebalance",
            "schedule": 900.0,
        },
        # Position monitor — every 60 seconds (trailing stops, exits)
        "position-monitor": {
            "task": "app.workers.tasks.run_position_monitor",
            "schedule": 60.0,  # 1 minute
        },
        # Order reconciliation — every 30 seconds
        "reconcile-orders": {
            "task": "app.workers.tasks.reconcile_pending_orders",
            "schedule": 30.0,
        },
        # Account snapshot — every 60 seconds
        "account-snapshot": {
            "task": "app.workers.tasks.sync_account_snapshot",
            "schedule": 60.0,
        },
        # EOD flatten check — every 2 minutes
        "eod-flatten": {
            "task": "app.workers.tasks.check_eod_flatten",
            "schedule": 120.0,
        },
        # Daily stats reset at midnight UTC
        "daily-reset": {
            "task": "app.workers.tasks.daily_reset",
            "schedule": crontab(hour=0, minute=0),
        },
        # Persisted worker liveness — observability only, no trading side effects.
        "worker-heartbeat": {
            "task": "app.workers.tasks_heartbeat.record_worker_heartbeat_task",
            "schedule": 60.0,
        },
        # Paper-only Kraken DCA planner evaluation — daily cadence only.
        # Separate from the 5-minute signal runner and never creates orders.
        "dca-paper-evaluate": {
            "task": "app.workers.tasks_dca.evaluate_due_plans_task",
            "schedule": crontab(hour=1, minute=0),
        },
        # Morning scanner — 14:15 UTC = 09:15 ET
        "morning-scan": {
            "task": "app.workers.tasks.morning_scan",
            "schedule": crontab(hour=14, minute=15),
        },
        # Order timeout cleanup — every 5 minutes
        "order-timeout": {
            "task": "app.workers.tasks.cancel_timed_out_orders",
            "schedule": 300.0,
        },
        # CFD overnight funding cost — 22:00 UTC (before EOD flatten at 21:00)
        # Runs after NYSE close so all intraday positions are priced at day close.
        "cfd-funding": {
            "task": "app.workers.tasks.track_cfd_funding",
            "schedule": crontab(hour=22, minute=0),
        },
        # Data retention — purge old audit logs and risk events at 03:00 UTC
        # Runs in the overnight quiet window, well away from market-hours tasks.
        "purge-old-records": {
            "task": "app.workers.tasks.purge_old_records",
            "schedule": crontab(hour=3, minute=0),
        },
    },
)

# Wire up dead-letter queue on task exhaustion.
# Import here (after celery_app is defined) to avoid circular imports.
from app.workers.dead_letter import handle_task_failure  # noqa: E402

task_failure.connect(handle_task_failure)
