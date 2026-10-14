"""
Celery periodic tasks.
All tasks are idempotent and safe to retry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger()


_LOOP: asyncio.AbstractEventLoop | None = None


def run_async(coro: Awaitable[Any]) -> Any:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _record_task_heartbeat(db: Any, task_name: str, payload: dict[str, Any]) -> None:
    from app.services.worker_health import record_worker_heartbeat

    await record_worker_heartbeat(db, task_name=task_name, payload=payload)


async def _mark_connection_reconnect_required(
    db: Any, conn: Any, reason: str, *, actor: str
) -> None:
    from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required

    await mark_broker_connection_reconnect_required(db, conn, reason, actor=actor)


async def _complete_task(db: Any, task_name: str, summary: dict[str, Any]) -> dict[str, Any]:
    await _record_task_heartbeat(db, task_name, summary)
    await db.commit()
    return summary


async def run_daily_reset_once(db: Any) -> dict[str, Any]:
    """Reset daily counters without automatically recovering the kill switch."""
    from sqlalchemy import desc, select

    from app.db.models import AppSettings, AuditLog, RiskEvent

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s:
        last_ks = await db.execute(
            select(RiskEvent)
            .where(RiskEvent.event_type == "kill_switch_on")
            .order_by(desc(RiskEvent.occurred_at))
            .limit(1)
        )
        last_ks_event = last_ks.scalar_one_or_none()
        if s.kill_switch_active and last_ks_event:
            db.add(
                AuditLog(
                    action="daily_reset_manual_recovery_required",
                    actor="daily_reset_task",
                    payload={
                        "reason": "Kill switch remains active after daily reset; auto-trading stays disabled.",
                        "last_kill_switch_event": last_ks_event.message,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )

    return {"reset": True, "timestamp": datetime.now(UTC).isoformat()}


async def _record_task_failure(task_name: str, exc: Exception) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await _record_task_heartbeat(
            db,
            task_name,
            {
                "status": "error",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
        )
        await db.commit()


SKIPPED_ALREADY_RUNNING: dict[str, Any] = {"skipped": True, "reason": "already_running"}
# After a redelivered run finds the lock held, it waits out the lease plus this margin.
REDELIVERY_RETRY_MARGIN_SECONDS = 5


def _is_redelivery(task: Any) -> bool:
    """True when the broker is delivering this message again after an unacknowledged run."""
    delivery_info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
    return bool(delivery_info.get("redelivered"))


def run_monitored_task(
    task_name: str,
    coro_factory: Callable[[], Awaitable[dict[str, Any]]],
    *,
    exclusive_ttl_seconds: int | None = None,
    task: Any = None,
) -> dict[str, Any]:
    """Run a task body with failure reporting.

    With ``exclusive_ttl_seconds`` the body runs under the task's single-owner lock, and a
    delivery that arrives while it is held is skipped. The lease must outlive the task's
    hard time limit, so that a killed run's lock expires instead of being taken over while
    the run is still alive.

    A redelivered message means an earlier run was never acknowledged (its worker died).
    Its lock may still be unexpired, so with ``task`` given that delivery is retried after
    the lease instead of being dropped; otherwise a once-a-day task would lose the day's run.
    """
    lock_held_elsewhere = False

    async def _run_as_single_owner(ttl_seconds: int) -> dict[str, Any]:
        nonlocal lock_held_elsewhere
        from app.core.redis import task_lock

        async with task_lock(task_name, ttl_seconds=ttl_seconds) as acquired:
            if not acquired:
                lock_held_elsewhere = True
                log.info(
                    "tasks.skipped_locked",
                    task=task_name,
                    redelivered=_is_redelivery(task),
                )
                return dict(SKIPPED_ALREADY_RUNNING)
            return await coro_factory()

    async def _wrapped() -> dict[str, Any]:
        try:
            if exclusive_ttl_seconds is None:
                return await coro_factory()
            return await _run_as_single_owner(exclusive_ttl_seconds)
        except Exception as exc:
            log.exception("tasks.failed", task=task_name, error=str(exc))
            try:
                await _record_task_failure(task_name, exc)
            except Exception:
                log.exception("tasks.failure_heartbeat_failed", task=task_name)
            raise

    result = cast("dict[str, Any]", run_async(_wrapped()))
    if (
        lock_held_elsewhere
        and exclusive_ttl_seconds is not None
        and task is not None
        and _is_redelivery(task)
    ):
        # Bounded by the task's own max_retries, which the dead-letter handler also reads.
        raise task.retry(countdown=exclusive_ttl_seconds + REDELIVERY_RETRY_MARGIN_SECONDS)
    return result


# ── Strategy signal generation (every 5 min) ─────────────────────────────────


@celery_app.task(
    name="app.workers.tasks.run_strategy_signals",
    bind=True,
    max_retries=0,
    time_limit=240,
    soft_time_limit=180,
)
def run_strategy_signals(self: Any) -> dict[str, Any]:
    """Entry signal generation loop. Runs every 5 minutes."""

    async def _run() -> dict[str, Any]:
        from app.core.config import settings
        from app.core.redis import task_lock
        from app.db.session import AsyncSessionLocal
        from app.services.strategy_runner import StrategyRunner

        async with task_lock("run_strategy_signals", ttl_seconds=270) as acquired:
            if not acquired:
                log.debug("tasks.skipped_locked", task="run_strategy_signals")
                return {"skipped": True, "reason": "already_running"}
            async with AsyncSessionLocal() as db:
                runner = StrategyRunner(db)
                summary: dict[str, Any] = await runner.run_all_enabled()
                if settings.APP_MODE == "mock" and summary.get("errors"):
                    raise RuntimeError(f"strategy execution failed: {summary['errors']!r}")
                summary = await _complete_task(db, "run_strategy_signals", summary)
            log.info("tasks.signals_complete", **summary)
            return summary

    return run_monitored_task("run_strategy_signals", _run)


# ── Portfolio rebalancing (every 15 min; service gates on due dates) ─────────


@celery_app.task(
    name="app.workers.tasks.run_portfolio_rebalance",
    bind=True,
    max_retries=0,
    time_limit=300,
    soft_time_limit=240,
)
def run_portfolio_rebalance(self: Any) -> dict[str, Any]:
    """Portfolio sleeve automation for lower-turnover basket strategies."""

    async def _run() -> dict[str, Any]:
        from app.core.redis import task_lock
        from app.db.session import AsyncSessionLocal
        from app.services.portfolio_execution_service import PortfolioExecutionService

        async with task_lock("run_portfolio_rebalance", ttl_seconds=330) as acquired:
            if not acquired:
                log.debug("tasks.skipped_locked", task="run_portfolio_rebalance")
                return {"skipped": True, "reason": "already_running"}
            async with AsyncSessionLocal() as db:
                service = PortfolioExecutionService(db)
                summary: dict[str, Any] = await service.run_all_enabled()
                summary = await _complete_task(db, "run_portfolio_rebalance", summary)
            if summary.get("strategies_due", 0) > 0 or summary.get("errors"):
                log.info("tasks.portfolio_rebalance", **summary)
            return summary

    return run_monitored_task("run_portfolio_rebalance", _run)


# ── Position monitor (every 60s) ──────────────────────────────────────────────


@celery_app.task(
    name="app.workers.tasks.run_position_monitor",
    bind=True,
    max_retries=0,
    time_limit=90,
    soft_time_limit=75,
)
def run_position_monitor(self: Any) -> dict[str, Any]:
    """
    THE EXIT ENGINE.
    Monitors open positions every 60 seconds for:
    - Trailing stop triggers
    - Take-profit targets
    - Partial exits at 1R
    - Daily loss limit breach (including unrealized)
    """

    async def _run() -> dict[str, Any]:
        from app.core.redis import task_lock
        from app.db.session import AsyncSessionLocal
        from app.services.position_monitor import PositionMonitor

        async with task_lock("run_position_monitor", ttl_seconds=120) as acquired:
            if not acquired:
                log.debug("tasks.skipped_locked", task="run_position_monitor")
                return {"skipped": True, "reason": "already_running"}
            async with AsyncSessionLocal() as db:
                monitor = PositionMonitor(db)
                summary: dict[str, Any] = await monitor.run()
                summary = await _complete_task(db, "run_position_monitor", summary)
            if summary.get("exits_submitted", 0) > 0:
                log.info("tasks.position_monitor", **summary)
            return summary

    return run_monitored_task("run_position_monitor", _run)


# ── Order reconciliation (every 30s) ─────────────────────────────────────────


@celery_app.task(
    name="app.workers.tasks.reconcile_pending_orders",
    bind=True,
    max_retries=3,
    time_limit=60,
    soft_time_limit=45,
)
def reconcile_pending_orders(self: Any) -> dict[str, Any]:
    """Ask the broker about orders that are still open or whose submission is unknown.

    Which orders, in what order and how often is decided in
    ``app.services.scheduled_reconciliation``. This task takes the reconciliation owner
    lock, builds one read-only provider per active connection, and reports the run.
    """
    task_name = "reconcile_pending_orders"

    async def _run() -> dict[str, Any]:
        from app.core.redis import TaskLockUnavailable, exclusive_task_lock
        from app.services.reconciliation_owner import RECONCILIATION_OWNER_LOCK

        try:
            async with exclusive_task_lock(RECONCILIATION_OWNER_LOCK, ttl_seconds=90) as acquired:
                if not acquired:
                    log.debug("tasks.skipped_locked", task=task_name)
                    return {"skipped": True, "reason": "already_running"}
                return await _reconcile_as_owner()
        except TaskLockUnavailable:
            # Not the same as "another run is active": nobody could be asked.
            log.error("tasks.owner_lock_unavailable", task=task_name)
            return {"skipped": True, "reason": "owner_lock_unavailable"}

    async def _reconcile_as_owner() -> dict[str, Any]:
        from contextlib import AsyncExitStack

        from celery.exceptions import SoftTimeLimitExceeded

        from app.core.config import settings as app_settings
        from app.db.session import AsyncSessionLocal
        from app.execution.engine import ExecutionEngine
        from app.services.reconciliation_queue import (
            active_connections,
            count_awaiting_reconciliation,
            count_awaiting_without_environment,
            report_orphaned_submissions,
        )
        from app.services.scheduled_reconciliation import ScheduledReconciler

        environment = app_settings.APP_MODE
        async with AsyncSessionLocal() as db:
            summary: dict[str, Any] = {"reconciled": 0}
            # Orders nobody can be asked about are reported on every run, busy or idle.
            orphans = await report_orphaned_submissions(db, datetime.now(UTC))
            if orphans:
                summary["orphaned_submitted"] = orphans
            out_of_scope: dict[str, int] = {}
            no_environment = await count_awaiting_without_environment(db)
            if no_environment:
                out_of_scope["no_environment"] = no_environment
                summary["out_of_scope"] = out_of_scope
            if environment == "mock" or not await count_awaiting_reconciliation(db, environment):
                return await _report(db, summary)

            connections = await active_connections(db, environment)
            if not connections:
                return await _report(db, {**summary, "skipped": "no_connection"})

            async with AsyncExitStack() as providers:
                accounts, refusals = await _open_accounts(db, connections, providers)
                blocked = next((r for r in refusals if "decision_code" in r), None)
                if blocked is not None:
                    return await _report(
                        db,
                        {
                            **summary,
                            "skipped": blocked["decision_code"],
                            "reason": blocked["reason"],
                        },
                    )
                if refusals:
                    # A connection that was switched off must stay off whatever happens next.
                    await db.commit()
                if not accounts:
                    return await _report(db, {**summary, **refusals[-1]})
                reconciler = ScheduledReconciler(
                    db,
                    environment=environment,
                    engine_factory=ExecutionEngine,
                    fatal_errors=(SoftTimeLimitExceeded,),
                )
                outcome = await reconciler.run(accounts, active_connection_count=len(connections))
            out_of_scope = {**outcome.pop("out_of_scope", {}), **out_of_scope}
            summary = {**summary, **outcome}
            if out_of_scope:
                summary["out_of_scope"] = out_of_scope
            if refusals:
                summary["connections_unusable"] = len(refusals)
            return await _report(db, summary)

    async def _report(db: Any, summary: dict[str, Any]) -> dict[str, Any]:
        if summary.get("out_of_scope"):
            log.warning("tasks.reconcile_orders_out_of_scope", **summary["out_of_scope"])
        return await _complete_task(db, task_name, summary)

    async def _open_accounts(
        db: Any, connections: list[Any], providers: Any
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Build a provider for each connection. Returns the usable ones and why the rest
        were refused. A safety-policy refusal stops at once: it applies to the whole run."""
        from app.broker.provider import (
            BrokerProviderCredentials,
            BrokerProviderRequest,
            BrokerProviderValidationError,
            BrokerRuntimeEnvironment,
            create_trading212_provider_adapter,
        )
        from app.core.config import settings as app_settings
        from app.core.security import CredentialDecryptionError, decrypt_field
        from app.services.reconciliation_broker import AccountBroker
        from app.services.reconciliation_scope import bound_account_scope
        from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

        accounts: list[Any] = []
        refusals: list[dict[str, Any]] = []
        for conn in connections:
            try:
                require_broker_environment(conn.environment, action="worker reconcile")
            except SafetyPolicyViolation as exc:
                return [], [{"decision_code": exc.decision_code, "reason": exc.reason}]
            try:
                api_key = decrypt_field(conn.api_key_encrypted)
                api_secret = decrypt_field(conn.api_secret_encrypted)
            except CredentialDecryptionError as exc:
                log.warning("tasks.credentials_invalid", task=task_name, error=str(exc))
                await _mark_connection_reconnect_required(
                    db,
                    conn,
                    str(exc),
                    actor="worker:reconcile_pending_orders",
                )
                refusals.append({"skipped": "credential_error"})
                continue
            try:
                provider_broker = create_trading212_provider_adapter(
                    BrokerProviderRequest(
                        broker_id="trading212",
                        environment=cast("BrokerRuntimeEnvironment", conn.environment),
                        purpose="worker_reconcile",
                        user_id=conn.user_id,
                        account_id=getattr(conn, "account_id", None),
                    ),
                    BrokerProviderCredentials(
                        api_key=api_key,
                        api_secret=api_secret,
                    ),
                    app_mode=app_settings.APP_MODE,
                    live_trading_enabled=bool(app_settings.LIVE_TRADING_ENABLED),
                )
            except BrokerProviderValidationError as exc:
                refusals.append({"skipped": "provider_validation_error", "reason": str(exc)})
                continue
            broker = await providers.enter_async_context(provider_broker)
            accounts.append(AccountBroker(broker, bound_account_scope(broker)))
        return accounts, refusals

    return run_monitored_task(task_name, _run)


# ── Account snapshot (every 60s) ─────────────────────────────────────────────


@celery_app.task(name="app.workers.tasks.sync_account_snapshot", bind=True, time_limit=30)
def sync_account_snapshot(self: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        import uuid
        from decimal import Decimal

        from sqlalchemy import select

        from app.broker.provider import (
            BrokerProviderCredentials,
            BrokerProviderRequest,
            BrokerProviderValidationError,
            BrokerRuntimeEnvironment,
            create_trading212_provider_adapter,
        )
        from app.core.config import settings
        from app.core.security import CredentialDecryptionError, decrypt_field
        from app.db.models import BrokerAccountSnapshot, BrokerConnection
        from app.db.session import AsyncSessionLocal
        from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

        async with AsyncSessionLocal() as db:
            conn_result = await db.execute(
                select(BrokerConnection)
                .where(BrokerConnection.is_active)
                .where(BrokerConnection.environment == settings.APP_MODE)
                .limit(1)
            )
            conn = conn_result.scalar_one_or_none()
            if settings.APP_MODE == "mock":
                from app.broker.mock_adapter import MockBrokerAdapter

                async with MockBrokerAdapter() as mock_broker:
                    summary = await mock_broker.get_account_summary()
                # In mock mode there is no real broker_connection row, so we
                # cannot satisfy the FK on broker_accounts_snapshots.
                # Skip the DB write — live account data is served on-demand
                # by /v1/account/summary which calls the adapter directly.
                if not conn:
                    summary = {"synced": True, "mode": "mock", "persisted": False}
                    return await _complete_task(db, "sync_account_snapshot", summary)
                currency = "USD"
                conn_id = conn.id
            elif conn:
                try:
                    require_broker_environment(conn.environment, action="worker account sync")
                except SafetyPolicyViolation as exc:
                    summary = {"synced": False, "skipped": exc.decision_code, "reason": exc.reason}
                    return await _complete_task(db, "sync_account_snapshot", summary)
                try:
                    api_key = decrypt_field(conn.api_key_encrypted)
                    api_secret = decrypt_field(conn.api_secret_encrypted)
                except CredentialDecryptionError as exc:
                    log.warning(
                        "tasks.credentials_invalid", task="sync_account_snapshot", error=str(exc)
                    )
                    await _mark_connection_reconnect_required(
                        db,
                        conn,
                        str(exc),
                        actor="worker:sync_account_snapshot",
                    )
                    summary = {"synced": False, "skipped": "credential_error"}
                    return await _complete_task(db, "sync_account_snapshot", summary)
                try:
                    provider_broker = create_trading212_provider_adapter(
                        BrokerProviderRequest(
                            broker_id="trading212",
                            environment=cast("BrokerRuntimeEnvironment", conn.environment),
                            purpose="worker_account_sync",
                            user_id=conn.user_id,
                            account_id=getattr(conn, "account_id", None),
                        ),
                        BrokerProviderCredentials(
                            api_key=api_key,
                            api_secret=api_secret,
                        ),
                        app_mode=settings.APP_MODE,
                        live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
                    )
                except BrokerProviderValidationError as exc:
                    summary = {
                        "synced": False,
                        "skipped": "provider_validation_error",
                        "reason": str(exc),
                    }
                    return await _complete_task(db, "sync_account_snapshot", summary)
                async with provider_broker:
                    summary = await provider_broker.get_account_summary()
                currency = conn.account_currency or "USD"
                conn_id = conn.id
            else:
                summary = {"synced": False, "skipped": "no_connection"}
                return await _complete_task(db, "sync_account_snapshot", summary)

            db.add(
                BrokerAccountSnapshot(
                    id=uuid.uuid4(),
                    connection_id=conn_id,
                    total_value=Decimal(str(summary.get("total", 0))),
                    cash=Decimal(str(summary.get("cash", 0))),
                    free_funds=Decimal(str(summary.get("free", 0))),
                    invested=Decimal(str(summary.get("invested", 0))),
                    result=Decimal(str(summary.get("result", 0))),
                    currency=currency,
                    raw=summary,
                )
            )
            task_summary = {"synced": True}
            return await _complete_task(db, "sync_account_snapshot", task_summary)

    return run_monitored_task("sync_account_snapshot", _run, exclusive_ttl_seconds=45, task=self)


# ── EOD flatten (every 2 min) ─────────────────────────────────────────────────


@celery_app.task(name="app.workers.tasks.check_eod_flatten", bind=True, time_limit=120)
def check_eod_flatten(self: Any) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        from sqlalchemy import select

        from app.core.redis import task_lock
        from app.db.models import AppSettings, Strategy
        from app.db.session import AsyncSessionLocal
        from app.services.position_monitor import PositionMonitor

        async with task_lock("check_eod_flatten", ttl_seconds=150) as acquired:
            if not acquired:
                log.debug("tasks.skipped_locked", task="check_eod_flatten")
                return {"flattened": 0, "reason": "already_running"}
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
                s = result.scalar_one_or_none()
                if not s or s.kill_switch_active:
                    summary = {"flattened": 0, "reason": "kill_switch"}
                    return await _complete_task(db, "check_eod_flatten", summary)

                result2 = await db.execute(
                    select(Strategy).where(
                        Strategy.is_enabled,
                        Strategy.is_live,
                        Strategy.eod_flatten,
                    )
                )
                strategies = result2.scalars().all()
                monitor = PositionMonitor(db)
                result3 = await monitor.eod_flatten(
                    list(strategies),
                    now_utc=datetime.now(UTC),
                )
                return await _complete_task(db, "check_eod_flatten", result3)

    return run_monitored_task("check_eod_flatten", _run)


# ── Daily reset (midnight UTC) ────────────────────────────────────────────────


@celery_app.task(name="app.workers.tasks.daily_reset", bind=True, time_limit=60)
def daily_reset(self: Any) -> dict[str, Any]:
    """Reset daily stats and re-enable strategies after overnight reset."""

    async def _run() -> dict[str, Any]:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await _complete_task(db, "daily_reset", await run_daily_reset_once(db))

    return run_monitored_task("daily_reset", _run, exclusive_ttl_seconds=120, task=self)


# ── Order timeout (every 5 min) ───────────────────────────────────────────────


@celery_app.task(
    name="app.workers.tasks.cancel_timed_out_orders",
    bind=True,
    time_limit=60,
    soft_time_limit=45,
)
def cancel_timed_out_orders(self: Any) -> dict[str, Any]:
    """
    Cancel working limit/stop orders that have been open longer than the timeout.
    Prevents stale orders from filling at bad prices hours later.
    """

    async def _run() -> dict[str, Any]:
        from datetime import timedelta

        from sqlalchemy import select

        from app.broker.provider import (
            BrokerProviderCredentials,
            BrokerProviderRequest,
            BrokerProviderValidationError,
            BrokerRuntimeEnvironment,
            create_trading212_provider_adapter,
        )
        from app.core.config import settings
        from app.core.redis import task_lock
        from app.core.security import CredentialDecryptionError, decrypt_field
        from app.db.models import BrokerConnection, Order
        from app.db.session import AsyncSessionLocal
        from app.execution.engine import ExecutionEngine, plain_amount
        from app.execution.state_machine import ACTIVE_ORDER_STATUSES
        from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

        ORDER_TIMEOUT_MINUTES = 60  # Cancel working orders after 1 hour

        async with task_lock("cancel_timed_out_orders", ttl_seconds=90) as acquired:
            if not acquired:
                log.debug("tasks.skipped_locked", task="cancel_timed_out_orders")
                return {"skipped": True, "reason": "already_running"}

            async with AsyncSessionLocal() as db:
                cutoff = datetime.now(UTC) - timedelta(minutes=ORDER_TIMEOUT_MINUTES)
                result = await db.execute(
                    select(Order).where(
                        Order.status.in_(ACTIVE_ORDER_STATUSES),
                        Order.order_type.in_(["limit", "stop", "stop_limit"]),
                        Order.created_at < cutoff,
                        Order.is_dry_run.is_(False),
                        Order.broker_order_id.isnot(None),
                    )
                )
                timed_out = result.scalars().all()
                if not timed_out or settings.APP_MODE == "mock":
                    return await _complete_task(db, "cancel_timed_out_orders", {"cancelled": 0})

                conn_result = await db.execute(
                    select(BrokerConnection)
                    .where(BrokerConnection.is_active)
                    .where(BrokerConnection.environment == settings.APP_MODE)
                    .limit(1)
                )
                conn = conn_result.scalar_one_or_none()
                if not conn:
                    return await _complete_task(
                        db,
                        "cancel_timed_out_orders",
                        {"cancelled": 0, "skipped": "no_connection"},
                    )
                try:
                    require_broker_environment(conn.environment, action="worker timeout cancel")
                except SafetyPolicyViolation as exc:
                    return await _complete_task(
                        db,
                        "cancel_timed_out_orders",
                        {"cancelled": 0, "skipped": exc.decision_code, "reason": exc.reason},
                    )

                count = 0
                try:
                    api_key = decrypt_field(conn.api_key_encrypted)
                    api_secret = decrypt_field(conn.api_secret_encrypted)
                except CredentialDecryptionError as exc:
                    log.warning(
                        "tasks.credentials_invalid",
                        task="cancel_timed_out_orders",
                        error=str(exc),
                    )
                    await _mark_connection_reconnect_required(
                        db,
                        conn,
                        str(exc),
                        actor="worker:cancel_timed_out_orders",
                    )
                    return await _complete_task(
                        db,
                        "cancel_timed_out_orders",
                        {"cancelled": 0, "skipped": "credential_error"},
                    )
                try:
                    provider_broker = create_trading212_provider_adapter(
                        BrokerProviderRequest(
                            broker_id="trading212",
                            environment=cast("BrokerRuntimeEnvironment", conn.environment),
                            purpose="worker_cancel_timed_out_orders",
                            user_id=conn.user_id,
                            account_id=getattr(conn, "account_id", None),
                        ),
                        BrokerProviderCredentials(
                            api_key=api_key,
                            api_secret=api_secret,
                        ),
                        app_mode=settings.APP_MODE,
                        live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
                    )
                except BrokerProviderValidationError as exc:
                    return await _complete_task(
                        db,
                        "cancel_timed_out_orders",
                        {
                            "cancelled": 0,
                            "skipped": "provider_validation_error",
                            "reason": str(exc),
                        },
                    )

                async with provider_broker as broker:
                    # Every cancel request goes out before any order is read back, so a rate
                    # limit met while reading cannot hold up a cancellation (see cancel_many).
                    batch = await ExecutionEngine(db, broker).cancel_many(list(timed_out))
                count = len(batch.cancelled)
                failed = len(batch.failed)
                already_finished = len(batch.finished)
                awaiting_confirmation = len(batch.unconfirmed)
                for order in batch.failed:
                    log.error(
                        "tasks.order_timeout_cancel_failed",
                        order_id=str(order.id),
                        ticker=order.ticker,
                        requires_reconciliation=True,
                    )
                for order, reason in batch.unconfirmed:
                    # The broker took the request and has not said what became of the
                    # order. It stays open, for reconciliation to settle.
                    log.warning(
                        "tasks.order_timeout_cancel_unconfirmed",
                        order_id=str(order.id),
                        ticker=order.ticker,
                        reason=reason,
                        requires_reconciliation=True,
                    )
                for order in batch.finished:
                    # It ended (filled, say) before the cancellation could take effect.
                    log.info(
                        "tasks.order_timeout_already_finished",
                        order_id=str(order.id),
                        ticker=order.ticker,
                        status=order.status,
                    )
                for order in batch.cancelled:
                    log.warning(
                        "tasks.order_timeout_cancel",
                        order_id=str(order.id),
                        ticker=order.ticker,
                        age_mins=ORDER_TIMEOUT_MINUTES,
                        filled_quantity=plain_amount(order.filled_quantity),
                    )

                summary = {"cancelled": count}
                if failed:
                    summary["failed"] = failed
                if already_finished:
                    summary["already_finished"] = already_finished
                if awaiting_confirmation:
                    summary["awaiting_confirmation"] = awaiting_confirmation
                return await _complete_task(db, "cancel_timed_out_orders", summary)

    return run_monitored_task("cancel_timed_out_orders", _run)


# ── Morning scanner (09:15 ET = 14:15 UTC) ────────────────────────────────────


@celery_app.task(name="app.workers.tasks.morning_scan", bind=True, time_limit=120)
def morning_scan(self: Any) -> dict[str, Any]:
    """
    Runs at 09:15 ET to find ORB and Opening Fade candidates for today.
    Uses strategy-typed scan: ORB strategies receive gap 0.5-2% candidates,
    Opening Fade strategies receive gap 1.5-6% candidates.
    Tickers that fall in the 1.5-2% overlap are routed to both.
    """

    async def _run() -> dict[str, Any]:
        from sqlalchemy import select

        from app.db.models import AuditLog, Strategy
        from app.db.session import AsyncSessionLocal
        from app.scanner.morning_scan import MorningScanner

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Strategy).where(
                    Strategy.is_enabled,
                    Strategy.is_live,
                )
            )
            strategies = result.scalars().all()

            if not strategies:
                return await _complete_task(db, "morning_scan", {"scanned": 0})

            # Use the union of all strategy watchlists as universe
            universe = list({t for s in strategies for t in s.allowed_tickers})
            scanner = MorningScanner()
            candidates = await scanner.scan(universe=universe if universe else None)
            orb_candidates = [
                candidate for candidate in candidates if candidate.strategy_type in {"orb", "both"}
            ]
            fade_candidates = [
                candidate
                for candidate in candidates
                if candidate.strategy_type in {"opening_fade", "both"}
            ]

            orb_tickers = [candidate.ticker for candidate in orb_candidates]
            fade_tickers = [candidate.ticker for candidate in fade_candidates]
            all_tickers = list({*orb_tickers, *fade_tickers})

            if not all_tickers:
                log.warning("morning_scan.no_candidates")
                return await _complete_task(db, "morning_scan", {"scanned": 0})

            # Update each strategy's watchlist, routing by strategy type
            for strategy in strategies:
                stype = strategy.type  # "orb" | "vwap_reclaim" | "opening_fade"

                # ORB and VWAP-reclaim get the ORB-eligible pool.
                pool = fade_tickers if stype == "opening_fade" else orb_tickers

                strategy_candidates = [
                    t for t in pool if not strategy.allowed_tickers or t in strategy.allowed_tickers
                ]
                if strategy_candidates:
                    candidate_pool = orb_candidates if stype != "opening_fade" else fade_candidates
                    candidate_context = {
                        candidate.ticker: {
                            "score": candidate.score,
                            "pre_market_rvol": candidate.pre_market_rvol,
                            "gap_pct": candidate.gap_pct,
                            "strategy_type": candidate.strategy_type,
                            "reason": candidate.reason,
                            "catalyst_score": candidate.catalyst_score,
                            "catalyst_event_type": candidate.catalyst_event_type,
                            "catalyst_summary": candidate.catalyst_summary,
                            "catalyst_source": candidate.catalyst_source,
                        }
                        for candidate in candidate_pool
                        if candidate.ticker in strategy_candidates
                    }
                    strategy.params = {
                        **strategy.params,
                        "todays_watchlist": strategy_candidates,
                        "watchlist_candidates": candidate_context,
                        "watchlist_updated_at": datetime.now(UTC).isoformat(),
                    }
                    db.add(
                        AuditLog(
                            action="watchlist_updated",
                            entity_type="strategy",
                            entity_id=str(strategy.id),
                            actor="morning_scan",
                            payload={
                                "tickers": strategy_candidates,
                                "watchlist_candidates": candidate_context,
                                "strategy_type": stype,
                                "orb_pool": orb_tickers,
                                "fade_pool": fade_tickers,
                            },
                            occurred_at=datetime.now(UTC),
                        )
                    )

            log.info(
                "morning_scan.complete",
                orb_tickers=orb_tickers,
                fade_tickers=fade_tickers,
            )
            return await _complete_task(
                db,
                "morning_scan",
                {
                    "scanned": len(all_tickers),
                    "orb": orb_tickers,
                    "fade": fade_tickers,
                },
            )

    return run_monitored_task("morning_scan", _run, exclusive_ttl_seconds=150, task=self)


# ── Data retention / archival (03:00 UTC daily) ──────────────────────────────


@celery_app.task(
    name="app.workers.tasks.purge_old_records", bind=True, time_limit=300, soft_time_limit=240
)
def purge_old_records(self: Any) -> dict[str, Any]:
    """
    Delete audit logs and risk events older than the configured retention window.

    Defaults (overridable via env):
      AUDIT_LOG_RETENTION_DAYS  = 90  (3 months — covers typical regulatory lookback)
      RISK_EVENT_RETENTION_DAYS = 30  (1 month — operational data)

    Runs at 03:00 UTC to avoid overlap with market-hours tasks.
    Uses chunked deletes (1 000 rows per loop) to avoid long-lock table scans
    on large Postgres tables.
    """

    async def _run() -> dict[str, Any]:
        from datetime import timedelta

        from sqlalchemy import delete, select

        from app.core.config import settings as app_settings
        from app.db.models import AuditLog, RiskEvent
        from app.db.session import AsyncSessionLocal

        audit_days: int = app_settings.AUDIT_LOG_RETENTION_DAYS
        risk_days: int = app_settings.RISK_EVENT_RETENTION_DAYS
        chunk = 1_000

        async with AsyncSessionLocal() as db:
            now = datetime.now(UTC)
            audit_cutoff = now - timedelta(days=audit_days)
            risk_cutoff = now - timedelta(days=risk_days)

            # ── AuditLog purge ────────────────────────────────────────────────
            audit_deleted = 0
            while True:
                # Fetch a batch of IDs to delete (avoids full-table lock)
                id_rows = (
                    (
                        await db.execute(
                            select(AuditLog.id)
                            .where(AuditLog.occurred_at < audit_cutoff)
                            .limit(chunk)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not id_rows:
                    break
                result = await db.execute(delete(AuditLog).where(AuditLog.id.in_(id_rows)))
                audit_deleted += int(getattr(result, "rowcount", 0))
                await db.flush()

            # ── RiskEvent purge ───────────────────────────────────────────────
            risk_deleted = 0
            while True:
                id_rows = (
                    (
                        await db.execute(
                            select(RiskEvent.id)
                            .where(RiskEvent.occurred_at < risk_cutoff)
                            .limit(chunk)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not id_rows:
                    break
                result = await db.execute(delete(RiskEvent).where(RiskEvent.id.in_(id_rows)))
                risk_deleted += int(getattr(result, "rowcount", 0))
                await db.flush()

            summary = {
                "audit_deleted": audit_deleted,
                "risk_deleted": risk_deleted,
                "audit_cutoff_days": audit_days,
                "risk_cutoff_days": risk_days,
            }
            log.info("tasks.purge_old_records", **summary)
            return await _complete_task(db, "purge_old_records", summary)

    return run_monitored_task("purge_old_records", _run, exclusive_ttl_seconds=330, task=self)
