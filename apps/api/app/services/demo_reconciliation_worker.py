"""One-shot Trading 212 DEMO order reconciliation worker.

The worker is intentionally scheduler-neutral. It selects local demo orders that
are due for read-only broker reconciliation, delegates each order to the existing
order reconciler, and records batch-level audit/observability metadata.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.serialization import to_jsonable
from app.db.models import AppSettings, AuditLog, Order
from app.execution.paper_engine import PAPER_EXECUTION_ENVIRONMENT
from app.services.demo_order_reconciliation import DemoOrderReconciler
from app.services.safety_policy import SafetyPolicyViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "rejected", "expired", "failed", "error"}
WORKER_STATE_KEY = "demo_reconciliation_worker"


@dataclass(frozen=True)
class DemoReconciliationWorkerRunSummary:
    run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    outcome: str
    worker_enabled: bool
    read_only_broker_calls: bool
    no_broker_order_sent: bool
    app_mode: str
    broker_environment: str | None
    live_trading_enabled: bool
    batch_size: int
    candidates_found: int = 0
    attempted: int = 0
    succeeded: int = 0
    missing: int = 0
    skipped: int = 0
    rate_limited: int = 0
    failed: int = 0
    unchanged: int = 0
    updated_order_ids: list[uuid.UUID] = field(default_factory=list)
    failed_order_ids: list[uuid.UUID] = field(default_factory=list)
    rate_limited_order_ids: list[uuid.UUID] = field(default_factory=list)
    order_results: list[dict[str, Any]] = field(default_factory=list)
    audit_event_ids: list[uuid.UUID] = field(default_factory=list)
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DemoReconciliationWorkerStatus:
    enabled: bool
    app_mode: str
    broker_environment: str | None
    live_trading_enabled: bool
    batch_size: int
    min_interval_seconds: int
    lookback_hours: int
    max_attempts_per_run: int
    history_limit: int
    last_run_at: datetime | None
    last_run_summary: dict[str, Any] | None
    safety_state: str
    warnings: list[str] = field(default_factory=list)


def normalise_status(status: str | None) -> str:
    return (status or "").strip().lower()


def is_terminal_order_status(status: str | None) -> bool:
    return normalise_status(status) in TERMINAL_ORDER_STATUSES


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_reconciliation_candidate(
    order: Order,
    *,
    now: datetime,
    min_interval_seconds: int,
    lookback_hours: int,
) -> bool:
    if order.execution_environment != "demo":
        return False
    if order.execution_environment == PAPER_EXECUTION_ENVIRONMENT or order.is_dry_run:
        return False
    if not order.broker_order_id:
        return False
    if is_terminal_order_status(order.status):
        return False

    created_at = _aware(order.created_at)
    if created_at is not None and created_at < now - timedelta(hours=lookback_hours):
        return False

    last_reconciled_at = _aware(order.last_reconciled_at)
    if last_reconciled_at is not None:
        min_age = timedelta(seconds=min_interval_seconds)
        return last_reconciled_at <= now - min_age
    return True


class DemoReconciliationWorker:
    """Run a bounded, read-only Trading 212 DEMO reconciliation pass."""

    def __init__(self, db: AsyncSession, broker: Any, *, actor: str = "demo_reconciliation_worker"):
        self.db = db
        self.broker = broker
        self.actor = actor

    async def run_once(self, *, require_enabled: bool = True) -> DemoReconciliationWorkerRunSummary:
        started_at = datetime.now(UTC)
        run_id = uuid.uuid4()
        audit_event_ids: list[uuid.UUID] = []
        self._require_safe_worker_boundaries(require_enabled=require_enabled)

        candidates = await self.select_reconciliation_candidates()
        audit_event_ids.append(
            await self._audit(
                "demo_reconciliation_worker_started",
                {
                    "run_id": str(run_id),
                    "candidates_found": len(candidates),
                },
            )
        )

        attempted = succeeded = missing = skipped = rate_limited = failed = unchanged = 0
        updated_order_ids: list[uuid.UUID] = []
        failed_order_ids: list[uuid.UUID] = []
        rate_limited_order_ids: list[uuid.UUID] = []
        order_results: list[dict[str, Any]] = []
        outcome = "completed"
        message: str | None = None
        warnings: list[str] = []
        max_attempts = max(0, settings.DEMO_RECONCILIATION_MAX_ATTEMPTS_PER_RUN)

        reconciler = DemoOrderReconciler(
            self.db,
            self.broker,
            actor=self.actor,
            history_limit=settings.DEMO_RECONCILIATION_HISTORY_LIMIT,
        )
        for order in candidates:
            if attempted >= max_attempts:
                skipped += 1
                warnings.append("Maximum attempts per run reached.")
                break
            if not is_reconciliation_candidate(
                order,
                now=datetime.now(UTC),
                min_interval_seconds=settings.DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS,
                lookback_hours=settings.DEMO_RECONCILIATION_LOOKBACK_HOURS,
            ):
                skipped += 1
                continue

            attempted += 1
            previous_status = order.status
            result = await reconciler.reconcile_order(order)
            order_results.append(
                {
                    "order_id": str(order.id),
                    "broker_order_id": order.broker_order_id,
                    "ticker": order.ticker,
                    "previous_status": result.previous_status,
                    "broker_status": result.broker_status,
                    "new_status": result.new_status,
                    "matched": result.matched,
                    "outcome": result.outcome,
                }
            )
            if result.outcome == "rate_limited":
                rate_limited += 1
                rate_limited_order_ids.append(order.id)
                outcome = "rate_limited"
                message = "Trading 212 rate-limited the read-only history request."
                audit_event_ids.append(
                    await self._audit(
                        "demo_reconciliation_worker_rate_limited",
                        {
                            "run_id": str(run_id),
                            "order_id": str(order.id),
                            "broker_order_id": order.broker_order_id,
                            "stop_batch": True,
                        },
                    )
                )
                break
            if result.outcome == "missing":
                missing += 1
                continue
            if result.outcome in {"failed", "unknown_status"}:
                if result.outcome == "unknown_status":
                    unchanged += 1
                else:
                    failed += 1
                    failed_order_ids.append(order.id)
                continue

            succeeded += 1
            updated_order_ids.append(order.id)
            if order.status == previous_status:
                unchanged += 1

        finished_at = datetime.now(UTC)
        if failed and outcome == "completed":
            outcome = "completed_with_failures"

        summary = DemoReconciliationWorkerRunSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            outcome=outcome,
            worker_enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            read_only_broker_calls=True,
            no_broker_order_sent=True,
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", None),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            batch_size=settings.DEMO_RECONCILIATION_BATCH_SIZE,
            candidates_found=len(candidates),
            attempted=attempted,
            succeeded=succeeded,
            missing=missing,
            skipped=skipped,
            rate_limited=rate_limited,
            failed=failed,
            unchanged=unchanged,
            updated_order_ids=updated_order_ids,
            failed_order_ids=failed_order_ids,
            rate_limited_order_ids=rate_limited_order_ids,
            order_results=order_results,
            audit_event_ids=audit_event_ids,
            message=message,
            warnings=warnings,
        )
        completed_action = (
            "demo_reconciliation_worker_rate_limited"
            if outcome == "rate_limited"
            else "demo_reconciliation_worker_completed"
        )
        summary_payload = self._summary_payload(summary)
        audit_event_ids.append(await self._audit(completed_action, summary_payload))
        summary = DemoReconciliationWorkerRunSummary(
            **{**asdict(summary), "audit_event_ids": audit_event_ids}
        )
        await self._persist_latest_summary(summary)
        await self.db.flush()
        return summary

    async def select_reconciliation_candidates(self) -> list[Order]:
        now = datetime.now(UTC)
        min_interval_cutoff = now - timedelta(
            seconds=settings.DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS
        )
        lookback_cutoff = now - timedelta(hours=settings.DEMO_RECONCILIATION_LOOKBACK_HOURS)
        result = await self.db.execute(
            select(Order)
            .where(Order.execution_environment == "demo")
            .where(Order.venue == "t212")
            .where(Order.is_dry_run.is_(False))
            .where(Order.broker_order_id.is_not(None))
            .where(Order.status.not_in(TERMINAL_ORDER_STATUSES))
            .where(Order.created_at >= lookback_cutoff)
            .where(
                or_(
                    Order.last_reconciled_at.is_(None),
                    Order.last_reconciled_at <= min_interval_cutoff,
                )
            )
            .order_by(Order.created_at.asc(), Order.id.asc())
            .limit(settings.DEMO_RECONCILIATION_BATCH_SIZE)
        )
        return list(result.scalars().all())

    async def reconcile_due_orders(self) -> DemoReconciliationWorkerRunSummary:
        return await self.run_once()

    async def get_worker_status(self) -> DemoReconciliationWorkerStatus:
        warnings = self._safety_warnings()
        latest = await self._latest_summary()
        last_run_at = None
        if latest and isinstance(latest.get("finished_at"), str):
            try:
                last_run_at = datetime.fromisoformat(latest["finished_at"])
            except ValueError:
                last_run_at = None
        return DemoReconciliationWorkerStatus(
            enabled=settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            app_mode=settings.APP_MODE,
            broker_environment=getattr(self.broker, "environment", settings.T212_ENVIRONMENT),
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
            batch_size=settings.DEMO_RECONCILIATION_BATCH_SIZE,
            min_interval_seconds=settings.DEMO_RECONCILIATION_MIN_INTERVAL_SECONDS,
            lookback_hours=settings.DEMO_RECONCILIATION_LOOKBACK_HOURS,
            max_attempts_per_run=settings.DEMO_RECONCILIATION_MAX_ATTEMPTS_PER_RUN,
            history_limit=settings.DEMO_RECONCILIATION_HISTORY_LIMIT,
            last_run_at=last_run_at,
            last_run_summary=latest,
            safety_state="safe" if not warnings else "blocked",
            warnings=warnings,
        )

    def _require_safe_worker_boundaries(self, *, require_enabled: bool) -> None:
        if require_enabled and not settings.DEMO_RECONCILIATION_WORKER_ENABLED:
            raise SafetyPolicyViolation(
                "Demo reconciliation worker is disabled.",
                decision_code="demo_reconciliation_worker_disabled",
            )
        warnings = self._safety_warnings()
        if warnings:
            raise SafetyPolicyViolation(
                f"Demo reconciliation worker blocked: {warnings[0]}",
                decision_code="demo_reconciliation_worker_safety_block",
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
        return warnings

    async def _audit(self, action: str, payload: dict[str, Any]) -> uuid.UUID:
        audit_id = uuid.uuid4()
        db_payload = {
            "app_mode": settings.APP_MODE,
            "broker_environment": getattr(self.broker, "environment", None),
            "live_trading_enabled": bool(settings.LIVE_TRADING_ENABLED),
            "worker_enabled": settings.DEMO_RECONCILIATION_WORKER_ENABLED,
            "batch_size": settings.DEMO_RECONCILIATION_BATCH_SIZE,
            "no_broker_order_sent": True,
            "read_only_broker_calls": True,
            **payload,
        }
        self.db.add(
            AuditLog(
                id=audit_id,
                action=action,
                entity_type="demo_reconciliation_worker",
                entity_id=str(payload.get("run_id", "")) or None,
                actor=self.actor,
                payload=db_payload,
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()
        return audit_id

    async def _persist_latest_summary(self, summary: DemoReconciliationWorkerRunSummary) -> None:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        app_settings = result.scalar_one_or_none()
        if app_settings is None:
            return
        extra = dict(app_settings.extra or {})
        extra[WORKER_STATE_KEY] = {
            "last_run_at": summary.finished_at.isoformat(),
            "last_run_summary": self._summary_payload(summary),
        }
        app_settings.extra = extra

    async def _latest_summary(self) -> dict[str, Any] | None:
        result = await self.db.execute(select(AppSettings).where(AppSettings.id == 1))
        app_settings = result.scalar_one_or_none()
        if app_settings is None:
            return None
        state = (app_settings.extra or {}).get(WORKER_STATE_KEY)
        if not isinstance(state, dict):
            return None
        summary = state.get("last_run_summary")
        return summary if isinstance(summary, dict) else None

    @staticmethod
    def _summary_payload(summary: DemoReconciliationWorkerRunSummary) -> dict[str, Any]:
        return cast(dict[str, Any], to_jsonable(asdict(summary)))
