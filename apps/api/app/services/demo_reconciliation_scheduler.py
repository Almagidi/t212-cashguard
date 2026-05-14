"""Scheduled Trading 212 DEMO reconciliation wrapper.

This scheduler owns timing, no-overlap protection, and scheduler-level
observability only. Order selection and broker history reconciliation remain in
``DemoReconciliationWorker`` so the existing read-only safety model stays in one
place.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.serialization import to_jsonable
from app.db.models import AppSettings, AuditLog
from app.services.demo_reconciliation_worker import (
    DemoReconciliationWorker,
    DemoReconciliationWorkerRunSummary,
)
from app.services.safety_policy import SafetyPolicyViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


SCHEDULER_STATE_KEY = "demo_reconciliation_scheduler"
_RUN_LOCK = asyncio.Lock()
_BACKGROUND_TASK: asyncio.Task[None] | None = None
log = structlog.get_logger()


@dataclass(frozen=True)
class DemoReconciliationSchedulerRunResult:
    run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    outcome: str
    enabled: bool
    worker_enabled: bool
    running: bool
    app_mode: str
    broker_environment: str | None
    live_trading_enabled: bool
    interval_seconds: int
    backoff_seconds: int
    initial_delay_seconds: int
    run_on_startup: bool
    no_broker_order_sent: bool
    read_only_broker_calls: bool
    worker_summary: dict[str, Any] | None = None
    skip_reason: str | None = None
    next_run_at: datetime | None = None
    next_run_not_before: datetime | None = None
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0
    total_runs: int = 0
    total_successful_runs: int = 0
    total_failed_runs: int = 0
    total_rate_limited_runs: int = 0
    last_error_message: str | None = None
    audit_event_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class DemoReconciliationSchedulerStatus:
    enabled: bool
    running: bool
    app_mode: str
    broker_environment: str | None
    live_trading_enabled: bool
    worker_enabled: bool
    interval_seconds: int
    backoff_seconds: int
    initial_delay_seconds: int
    run_on_startup: bool
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_duration_ms: int | None
    last_run_outcome: str | None
    last_run_summary: dict[str, Any] | None
    next_run_at: datetime | None
    next_run_not_before: datetime | None
    consecutive_failures: int
    consecutive_rate_limits: int
    total_runs: int
    total_successful_runs: int
    total_failed_runs: int
    total_rate_limited_runs: int
    last_error_message: str | None
    safety_state: str
    warnings: list[str]
    no_broker_order_sent: bool = True
    read_only_broker_calls: bool = True


WorkerFactory = Callable[..., Any]


class DemoReconciliationScheduler:
    """Safe in-process scheduler for the one-shot demo reconciliation worker."""

    def __init__(
        self,
        db: AsyncSession,
        broker: Any,
        *,
        actor: str = "demo_reconciliation_scheduler",
        worker_factory: WorkerFactory = DemoReconciliationWorker,
    ) -> None:
        self.db = db
        self.broker = broker
        self.actor = actor
        self.worker_factory = worker_factory
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        await self._audit("demo_reconciliation_scheduler_started", self._base_payload())
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._audit("demo_reconciliation_scheduler_stopped", self._base_payload())

    def is_running(self) -> bool:
        return _RUN_LOCK.locked()

    async def should_run_now(self, *, respect_interval: bool = True) -> tuple[bool, str | None]:
        if not settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED:
            return False, "scheduler_disabled"
        if not settings.DEMO_RECONCILIATION_WORKER_ENABLED:
            return False, "worker_disabled"
        self._require_safe_boundaries()
        if _RUN_LOCK.locked():
            return False, "already_running"

        status = await self.get_status()
        now = datetime.now(UTC)
        if status.next_run_not_before and status.next_run_not_before > now:
            return False, "backoff_active"
        if respect_interval and status.next_run_at and status.next_run_at > now:
            return False, "not_due"
        return True, None

    async def run_once(self) -> DemoReconciliationSchedulerRunResult:
        return await self.tick(respect_interval=False)

    async def tick(
        self,
        *,
        respect_interval: bool = True,
    ) -> DemoReconciliationSchedulerRunResult:
        started_at = datetime.now(UTC)
        run_id = uuid.uuid4()
        can_run, skip_reason = await self.should_run_now(respect_interval=respect_interval)
        if not can_run:
            return await self._record_skipped_run(
                run_id=run_id,
                started_at=started_at,
                skip_reason=skip_reason or "not_allowed",
            )

        if _RUN_LOCK.locked():
            return await self._record_skipped_run(
                run_id=run_id,
                started_at=started_at,
                skip_reason="already_running",
            )

        async with _RUN_LOCK:
            audit_event_ids = [
                await self._audit(
                    "demo_reconciliation_scheduler_tick_started",
                    {
                        **self._base_payload(),
                        "run_id": str(run_id),
                    },
                )
            ]
            try:
                worker = self.worker_factory(
                    self.db,
                    self.broker,
                    actor=self.actor,
                )
                worker_summary = await asyncio.wait_for(
                    worker.run_once(),
                    timeout=max(1, settings.DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS),
                )
            except Exception as exc:
                return await self._record_failed_run(
                    run_id=run_id,
                    started_at=started_at,
                    exc=exc,
                    audit_event_ids=audit_event_ids,
                )

            return await self.record_run_summary(
                run_id=run_id,
                started_at=started_at,
                worker_summary=worker_summary,
                audit_event_ids=audit_event_ids,
            )

    async def record_run_summary(
        self,
        *,
        run_id: uuid.UUID,
        started_at: datetime,
        worker_summary: DemoReconciliationWorkerRunSummary,
        audit_event_ids: list[uuid.UUID],
    ) -> DemoReconciliationSchedulerRunResult:
        finished_at = datetime.now(UTC)
        previous = await self._latest_result_payload()
        worker_payload = self._worker_summary_payload(worker_summary)
        rate_limited = int(worker_payload.get("rate_limited") or 0) > 0
        outcome = (
            "rate_limited" if rate_limited else str(worker_payload.get("outcome", "completed"))
        )
        total_runs = int(previous.get("total_runs") or 0) + 1
        total_successful_runs = int(previous.get("total_successful_runs") or 0)
        total_failed_runs = int(previous.get("total_failed_runs") or 0)
        total_rate_limited_runs = int(previous.get("total_rate_limited_runs") or 0)
        consecutive_failures = 0
        consecutive_rate_limits = 0
        next_run_not_before = None

        if rate_limited:
            total_rate_limited_runs += 1
            consecutive_rate_limits = int(previous.get("consecutive_rate_limits") or 0) + 1
            next_run_not_before = finished_at + timedelta(
                seconds=settings.DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS
            )
            action = "demo_reconciliation_scheduler_rate_limited"
        else:
            total_successful_runs += 1
            action = "demo_reconciliation_scheduler_tick_completed"

        next_run_at = next_run_not_before or (
            finished_at + timedelta(seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS)
        )
        result = DemoReconciliationSchedulerRunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            outcome=outcome,
            enabled=settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED,
            worker_enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            running=False,
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            interval_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS,
            backoff_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS,
            initial_delay_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS,
            run_on_startup=settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP,
            no_broker_order_sent=True,
            read_only_broker_calls=True,
            worker_summary=worker_payload,
            next_run_at=next_run_at,
            next_run_not_before=next_run_not_before,
            consecutive_failures=consecutive_failures,
            consecutive_rate_limits=consecutive_rate_limits,
            total_runs=total_runs,
            total_successful_runs=total_successful_runs,
            total_failed_runs=total_failed_runs,
            total_rate_limited_runs=total_rate_limited_runs,
            audit_event_ids=audit_event_ids,
        )
        audit_event_ids.append(await self._audit(action, self._result_audit_payload(result)))
        result = DemoReconciliationSchedulerRunResult(
            **{**asdict(result), "audit_event_ids": audit_event_ids}
        )
        await self._persist_latest_result(result)
        await self.db.flush()
        return result

    async def get_status(self) -> DemoReconciliationSchedulerStatus:
        latest = await self._latest_result_payload()
        warnings = self._safety_warnings()
        last_summary = latest.get("worker_summary")
        return DemoReconciliationSchedulerStatus(
            enabled=settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED,
            running=_RUN_LOCK.locked(),
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            worker_enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            interval_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS,
            backoff_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS,
            initial_delay_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS,
            run_on_startup=settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP,
            last_run_started_at=self._parse_datetime(latest.get("started_at")),
            last_run_finished_at=self._parse_datetime(latest.get("finished_at")),
            last_run_duration_ms=cast(int | None, latest.get("duration_ms")),
            last_run_outcome=cast(str | None, latest.get("outcome")),
            last_run_summary=last_summary if isinstance(last_summary, dict) else None,
            next_run_at=self._parse_datetime(latest.get("next_run_at")),
            next_run_not_before=self._parse_datetime(latest.get("next_run_not_before")),
            consecutive_failures=int(latest.get("consecutive_failures") or 0),
            consecutive_rate_limits=int(latest.get("consecutive_rate_limits") or 0),
            total_runs=int(latest.get("total_runs") or 0),
            total_successful_runs=int(latest.get("total_successful_runs") or 0),
            total_failed_runs=int(latest.get("total_failed_runs") or 0),
            total_rate_limited_runs=int(latest.get("total_rate_limited_runs") or 0),
            last_error_message=cast(str | None, latest.get("last_error_message")),
            safety_state="safe" if not warnings else "blocked",
            warnings=warnings,
            no_broker_order_sent=True,
            read_only_broker_calls=True,
        )

    async def _run_loop(self) -> None:
        if (
            settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS > 0
            and not settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP
        ):
            await asyncio.sleep(settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS)
        while not self._stopping.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
                await self.db.commit()
            await asyncio.sleep(max(1, settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS))

    async def _record_skipped_run(
        self,
        *,
        run_id: uuid.UUID,
        started_at: datetime,
        skip_reason: str,
    ) -> DemoReconciliationSchedulerRunResult:
        finished_at = datetime.now(UTC)
        previous = await self._latest_result_payload()
        result = DemoReconciliationSchedulerRunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            outcome="skipped",
            enabled=settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED,
            worker_enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            running=_RUN_LOCK.locked(),
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            interval_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS,
            backoff_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS,
            initial_delay_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS,
            run_on_startup=settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP,
            no_broker_order_sent=True,
            read_only_broker_calls=True,
            skip_reason=skip_reason,
            next_run_at=self._parse_datetime(previous.get("next_run_at")),
            next_run_not_before=self._parse_datetime(previous.get("next_run_not_before")),
            consecutive_failures=int(previous.get("consecutive_failures") or 0),
            consecutive_rate_limits=int(previous.get("consecutive_rate_limits") or 0),
            total_runs=int(previous.get("total_runs") or 0),
            total_successful_runs=int(previous.get("total_successful_runs") or 0),
            total_failed_runs=int(previous.get("total_failed_runs") or 0),
            total_rate_limited_runs=int(previous.get("total_rate_limited_runs") or 0),
            last_error_message=cast(str | None, previous.get("last_error_message")),
        )
        audit_id = await self._audit(
            "demo_reconciliation_scheduler_tick_skipped",
            {
                **self._result_audit_payload(result),
                "skip_reason": skip_reason,
            },
        )
        result = DemoReconciliationSchedulerRunResult(
            **{**asdict(result), "audit_event_ids": [audit_id]}
        )
        await self._persist_latest_result(result)
        await self.db.flush()
        return result

    async def _record_failed_run(
        self,
        *,
        run_id: uuid.UUID,
        started_at: datetime,
        exc: Exception,
        audit_event_ids: list[uuid.UUID],
    ) -> DemoReconciliationSchedulerRunResult:
        finished_at = datetime.now(UTC)
        previous = await self._latest_result_payload()
        result = DemoReconciliationSchedulerRunResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            outcome="failed",
            enabled=settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED,
            worker_enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            running=False,
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            interval_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS,
            backoff_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_BACKOFF_SECONDS,
            initial_delay_seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS,
            run_on_startup=settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP,
            no_broker_order_sent=True,
            read_only_broker_calls=True,
            consecutive_failures=int(previous.get("consecutive_failures") or 0) + 1,
            consecutive_rate_limits=0,
            total_runs=int(previous.get("total_runs") or 0) + 1,
            total_successful_runs=int(previous.get("total_successful_runs") or 0),
            total_failed_runs=int(previous.get("total_failed_runs") or 0) + 1,
            total_rate_limited_runs=int(previous.get("total_rate_limited_runs") or 0),
            last_error_message=type(exc).__name__,
            next_run_at=finished_at
            + timedelta(seconds=settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS),
        )
        audit_event_ids.append(
            await self._audit(
                "demo_reconciliation_scheduler_failed",
                self._result_audit_payload(result),
            )
        )
        result = DemoReconciliationSchedulerRunResult(
            **{**asdict(result), "audit_event_ids": audit_event_ids}
        )
        await self._persist_latest_result(result)
        await self.db.flush()
        return result

    def _require_safe_boundaries(self) -> None:
        warnings = self._safety_warnings()
        if warnings:
            raise SafetyPolicyViolation(
                f"Demo reconciliation scheduler blocked: {warnings[0]}",
                decision_code="demo_reconciliation_scheduler_safety_block",
            )

    def _safety_warnings(self) -> list[str]:
        warnings: list[str] = []
        broker_environment = getattr(self.broker, "environment", settings.T212_ENVIRONMENT)
        if settings.APP_MODE != "demo":
            warnings.append("APP_MODE must be demo.")
        if settings.T212_ENVIRONMENT != "demo":
            warnings.append("T212_ENVIRONMENT must be demo.")
        if bool(settings.LIVE_TRADING_ENABLED):
            warnings.append("LIVE_TRADING_ENABLED must be false.")
        if broker_environment != "demo":
            warnings.append("Broker environment must be demo.")
        if not settings.DEMO_RECONCILIATION_WORKER_ENABLED:
            warnings.append("Demo reconciliation worker must be enabled.")
        return warnings

    async def _audit(self, action: str, payload: dict[str, Any]) -> uuid.UUID:
        audit_id = uuid.uuid4()
        self.db.add(
            AuditLog(
                id=audit_id,
                action=action,
                entity_type="demo_reconciliation_scheduler",
                entity_id=str(payload.get("run_id", "")) or None,
                actor=self.actor,
                payload={
                    **self._base_payload(),
                    **payload,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()
        return audit_id

    def _base_payload(self) -> dict[str, Any]:
        return {
            "scheduler_enabled": settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED,
            "worker_enabled": settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            "app_mode": settings.APP_MODE,
            "broker_environment": getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            "live_trading_enabled": bool(settings.LIVE_TRADING_ENABLED),
            "interval_seconds": settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS,
            "no_broker_order_sent": True,
            "read_only_broker_calls": True,
        }

    def _result_audit_payload(self, result: DemoReconciliationSchedulerRunResult) -> dict[str, Any]:
        worker_summary = result.worker_summary or {}
        return {
            **self._base_payload(),
            "run_id": str(result.run_id),
            "outcome": result.outcome,
            "skip_reason": result.skip_reason,
            "candidates_found": worker_summary.get("candidates_found", 0),
            "attempted": worker_summary.get("attempted", 0),
            "succeeded": worker_summary.get("succeeded", 0),
            "missing": worker_summary.get("missing", 0),
            "failed": worker_summary.get("failed", 0),
            "rate_limited": worker_summary.get("rate_limited", 0),
            "skipped": worker_summary.get("skipped", 0),
        }

    async def _persist_latest_result(self, result: DemoReconciliationSchedulerRunResult) -> None:
        row = await self._app_settings()
        if row is None:
            return
        extra = dict(row.extra or {})
        extra[SCHEDULER_STATE_KEY] = {
            "last_run_result": self._result_payload(result),
        }
        row.extra = extra

    async def _latest_result_payload(self) -> dict[str, Any]:
        row = await self._app_settings()
        if row is None:
            return {}
        state = (row.extra or {}).get(SCHEDULER_STATE_KEY)
        if not isinstance(state, dict):
            return {}
        result = state.get("last_run_result")
        return result if isinstance(result, dict) else {}

    async def _app_settings(self) -> AppSettings | None:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        return result.scalar_one_or_none()

    @staticmethod
    def _result_payload(result: DemoReconciliationSchedulerRunResult) -> dict[str, Any]:
        return cast(dict[str, Any], to_jsonable(asdict(result)))

    @staticmethod
    def _worker_summary_payload(summary: DemoReconciliationWorkerRunSummary) -> dict[str, Any]:
        return cast(dict[str, Any], to_jsonable(asdict(summary)))

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(value)
        return None


class _StatusBroker:
    environment = "demo"


async def start_global_demo_reconciliation_scheduler() -> asyncio.Task[None] | None:
    """Start the disabled-by-default app-level scheduler loop."""
    global _BACKGROUND_TASK

    if not settings.DEMO_RECONCILIATION_SCHEDULER_ENABLED:
        return None
    if not settings.DEMO_RECONCILIATION_WORKER_ENABLED:
        log.info("demo_reconciliation_scheduler.not_started", reason="worker_disabled")
        return None
    if settings.APP_MODE != "demo":
        log.info("demo_reconciliation_scheduler.not_started", reason="app_mode_not_demo")
        return None
    if settings.T212_ENVIRONMENT != "demo":
        log.info("demo_reconciliation_scheduler.not_started", reason="t212_environment_not_demo")
        return None
    if bool(settings.LIVE_TRADING_ENABLED):
        log.warning("demo_reconciliation_scheduler.not_started", reason="live_trading_enabled")
        return None

    if _BACKGROUND_TASK and not _BACKGROUND_TASK.done():
        return _BACKGROUND_TASK

    await _audit_global_lifecycle("demo_reconciliation_scheduler_started")

    async def _loop() -> None:
        from app.broker.trading212 import Trading212Adapter
        from app.db.session import AsyncSessionLocal

        if (
            settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS > 0
            and not settings.DEMO_RECONCILIATION_SCHEDULER_RUN_ON_STARTUP
        ):
            await asyncio.sleep(settings.DEMO_RECONCILIATION_SCHEDULER_INITIAL_DELAY_SECONDS)

        while True:
            try:
                api_key = settings.T212_DEMO_API_KEY or settings.T212_API_KEY
                api_secret = settings.T212_DEMO_API_SECRET or settings.T212_API_SECRET
                if not api_key or not api_secret:
                    log.warning("demo_reconciliation_scheduler.credentials_missing")
                else:
                    async with (
                        AsyncSessionLocal() as db,
                        Trading212Adapter(api_key, api_secret, "demo") as broker,
                    ):
                        scheduler = DemoReconciliationScheduler(
                            db,
                            broker,
                            actor="background:demo_reconciliation_scheduler",
                        )
                        await scheduler.tick()
                        await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "demo_reconciliation_scheduler.loop_error",
                    error_type=type(exc).__name__,
                )

            await asyncio.sleep(max(1, settings.DEMO_RECONCILIATION_SCHEDULER_INTERVAL_SECONDS))

    _BACKGROUND_TASK = asyncio.create_task(_loop())
    return _BACKGROUND_TASK


async def stop_global_demo_reconciliation_scheduler() -> None:
    global _BACKGROUND_TASK
    had_task = _BACKGROUND_TASK is not None
    if _BACKGROUND_TASK and not _BACKGROUND_TASK.done():
        _BACKGROUND_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _BACKGROUND_TASK
    _BACKGROUND_TASK = None
    if had_task:
        await _audit_global_lifecycle("demo_reconciliation_scheduler_stopped")


async def build_scheduler_status(db: AsyncSession) -> DemoReconciliationSchedulerStatus:
    broker = _StatusBroker()
    broker.environment = settings.T212_ENVIRONMENT
    return await DemoReconciliationScheduler(db, broker).get_status()


async def _audit_global_lifecycle(action: str) -> None:
    from app.db.session import AsyncSessionLocal

    broker = _StatusBroker()
    broker.environment = settings.T212_ENVIRONMENT
    async with AsyncSessionLocal() as db:
        scheduler = DemoReconciliationScheduler(
            db,
            broker,
            actor="background:demo_reconciliation_scheduler",
        )
        await scheduler._audit(action, scheduler._base_payload())
        await db.commit()
