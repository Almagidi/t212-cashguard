"""Read-only operator control tower status."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from celery.schedules import crontab
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from app.api.deps import get_current_user
from app.api.schemas import (
    LiveReadinessStatus,
    OperatorDcaStatusOut,
    OperatorKrakenStatusOut,
    OperatorRecentActivityOut,
    OperatorSafetyFlagsOut,
    OperatorSchedulersStatusOut,
    OperatorStatusOut,
    OperatorTrading212StatusOut,
    OperatorVenueStatusOut,
)
from app.core.config import settings
from app.db.models import (
    AppSettings,
    AuditLog,
    DcaConfig,
    DcaPlanState,
    Order,
    Strategy,
    User,
    VenueConfig,
    WorkerHeartbeat,
)
from app.db.repositories.venue_config_repo import VenueConfigRepository
from app.db.repositories.worker_heartbeat_repo import WorkerHeartbeatRepository
from app.db.session import get_db
from app.services.live_readiness import LiveReadinessError, LiveReadinessService
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers.celery_app import celery_app
from app.workers.tasks_heartbeat import (
    HEARTBEAT_COMPONENT,
    HEARTBEAT_STALE_AFTER_SECONDS,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/operator", tags=["operator"])

_ACTIVE_ORDER_STATUSES = ("pending_intent", "submitted", "accepted")
_DCA_BEAT_KEY = "dca-paper-evaluate"
_DCA_TASK = "app.workers.tasks_dca.evaluate_due_plans_task"
_HEARTBEAT_BEAT_KEY = "worker-heartbeat"
_HEARTBEAT_TASK = "app.workers.tasks_heartbeat.record_worker_heartbeat_task"
_EXPECTED_VENUES = ("t212", "kraken")


def _beat_entry(key: str, task: str) -> dict[str, Any] | None:
    entry = celery_app.conf.beat_schedule.get(key)
    if not isinstance(entry, dict):
        return None
    if entry.get("task") != task:
        return None
    return entry


def _scheduler_entry() -> dict[str, Any] | None:
    return _beat_entry(_DCA_BEAT_KEY, _DCA_TASK)


def _heartbeat_entry() -> dict[str, Any] | None:
    return _beat_entry(_HEARTBEAT_BEAT_KEY, _HEARTBEAT_TASK)


def _readable_schedule(entry: dict[str, Any] | None) -> str | None:
    if entry is None:
        return None
    schedule = entry.get("schedule")
    if isinstance(schedule, crontab):
        hour = str(schedule._orig_hour).zfill(2)
        minute = str(schedule._orig_minute).zfill(2)
        return f"daily at {hour}:{minute} UTC"
    if schedule is None:
        return None
    return str(schedule)


def _evaluate_worker_health(
    heartbeat: WorkerHeartbeat | None,
    *,
    now: datetime,
) -> Literal["healthy", "stale", "missing", "unknown"]:
    if heartbeat is None:
        return "missing"
    last_seen_at: datetime | None = getattr(heartbeat, "last_seen_at", None)
    if last_seen_at is None:
        return "unknown"
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=UTC)
    else:
        last_seen_at = last_seen_at.astimezone(UTC)
    if last_seen_at > now + timedelta(seconds=5):
        return "unknown"
    if now - last_seen_at <= timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS):
        return "healthy"
    return "stale"


def _venue_status_from_row(row: VenueConfig | None, venue: str) -> OperatorVenueStatusOut:
    if row is None:
        return OperatorVenueStatusOut(
            venue=venue,
            present=False,
            kill_switch_active=None,
            auto_trading_enabled=None,
            degraded_mode_active=None,
            note="venue_config row is missing; status is fail-closed",
            updated_at=None,
        )
    return OperatorVenueStatusOut(
        venue=row.venue,
        present=True,
        kill_switch_active=row.kill_switch_active,
        auto_trading_enabled=row.auto_trading_enabled,
        degraded_mode_active=row.degraded_mode_active,
        note=row.note,
        updated_at=row.updated_at,
    )


def _is_live_approved(strategy: Strategy) -> bool:
    params = strategy.params if isinstance(strategy.params, dict) else {}
    promotion = params.get("promotion")
    live_approved_at = (
        promotion.get("live_approved_at")
        if isinstance(promotion, dict)
        else None
    )
    return bool(strategy.is_live or live_approved_at)


def _audit_payload(audit: AuditLog) -> dict[str, Any]:
    return audit.payload if isinstance(audit.payload, dict) else {}


def _payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _decision_code(payload: dict[str, Any]) -> str | None:
    return _payload_str(payload, "decision_code")


def _safe_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "ticker",
        "venue",
        "paper_only",
        "decision_code",
        "reason",
        "mode",
        "amount_usd",
        "evaluated_on",
        "config_id",
        "changed_fields",
        "previous_enabled",
        "new_enabled",
        "broker",
        "environment",
        "test_ok",
    ):
        if key in payload:
            summary[key] = payload[key]
    return summary


async def _count_orders(
    db: AsyncSession,
    *,
    venue: str,
    statuses: tuple[str, ...] | None = None,
    since: datetime | None = None,
) -> int:
    query = select(func.count()).select_from(Order).where(Order.venue == venue)
    if statuses is not None:
        query = query.where(Order.status.in_(statuses))
    if since is not None:
        query = query.where(Order.created_at >= since)
    return int((await db.execute(query)).scalar_one())


@router.get("/status", response_model=OperatorStatusOut)
async def operator_status(
    audit_limit: int = Query(20, ge=1, le=100),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OperatorStatusOut:
    now = datetime.now(UTC)
    recent_since = now - timedelta(days=1)

    venue_rows = list(await VenueConfigRepository(db).list_all())
    venues_by_name = {row.venue: row for row in venue_rows}
    venue_statuses = [
        _venue_status_from_row(venues_by_name.get(venue), venue)
        for venue in _EXPECTED_VENUES
    ]

    strategy_rows = list((await db.execute(select(Strategy))).scalars().all())
    t212_strategies = [strategy for strategy in strategy_rows if strategy.venue == "t212"]
    kraken_strategies = [strategy for strategy in strategy_rows if strategy.venue == "kraken"]

    latest_t212_order = (
        await db.execute(
            select(Order)
            .where(Order.venue == "t212")
            .order_by(desc(Order.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    try:
        live_readiness_payload = await LiveReadinessService(db).evaluate()
        live_readiness_status = LiveReadinessStatus(**live_readiness_payload)
    except LiveReadinessError:
        live_readiness_status = None

    dca_configs = list((await db.execute(select(DcaConfig))).scalars().all())
    dca_states = list((await db.execute(select(DcaPlanState))).scalars().all())
    dca_audits = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "dca_paper_decision")
                .order_by(desc(AuditLog.occurred_at))
            )
        ).scalars().all()
    )

    decision_counts: dict[str, int] = {}
    audit_tickers: set[str] = set()
    for audit in dca_audits:
        payload = _audit_payload(audit)
        code = _decision_code(payload)
        if code is not None:
            decision_counts[code] = decision_counts.get(code, 0) + 1
        ticker = _payload_str(payload, "ticker")
        if ticker is not None:
            audit_tickers.add(ticker)

    scheduler_entry = _scheduler_entry()
    scheduler_registered = scheduler_entry is not None
    scheduler_cadence = _readable_schedule(scheduler_entry)
    heartbeat_entry = _heartbeat_entry()
    heartbeat_registered = heartbeat_entry is not None
    heartbeat_cadence = _readable_schedule(heartbeat_entry)
    heartbeat = await WorkerHeartbeatRepository(db).get_by_component(HEARTBEAT_COMPONENT)
    worker_health = _evaluate_worker_health(heartbeat, now=now)
    heartbeat_last_seen_at = heartbeat.last_seen_at if heartbeat is not None else None

    recent_audit_rows = list(
        (
            await db.execute(
                select(AuditLog)
                .order_by(desc(AuditLog.occurred_at))
                .limit(audit_limit)
            )
        ).scalars().all()
    )

    app_settings = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one_or_none()
    app_live_trading_unlocked = bool(
        app_settings.live_trading_unlocked if app_settings is not None else False
    )

    any_venue_kill_switch_active = any(
        status.kill_switch_active is True
        for status in venue_statuses
    )
    any_venue_degraded = any(
        status.degraded_mode_active is True
        for status in venue_statuses
    )
    missing_expected_venue_configs = any(not status.present for status in venue_statuses)
    worker_health_known = worker_health == "healthy"
    kraken_live_enabled = False
    dca_runnable = KrakenDCAPlanner.RUNNABLE is True
    dca_live_enabled = False
    live_trading_possible = bool(
        settings.CASH_ONLY_MODE
        and settings.LIVE_TRADING_ENABLED
        and app_live_trading_unlocked
        and not any_venue_kill_switch_active
        and not missing_expected_venue_configs
    )
    live_trading_enabled_anywhere = bool(
        settings.LIVE_TRADING_ENABLED
        or app_live_trading_unlocked
        or any(status.auto_trading_enabled is True for status in venue_statuses)
    )

    if not settings.CASH_ONLY_MODE or any_venue_kill_switch_active:
        overall_status: Literal["ok", "degraded", "blocked"] = "blocked"
    elif (
        missing_expected_venue_configs
        or any_venue_degraded
        or live_readiness_status is None
        or not worker_health_known
    ):
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return OperatorStatusOut(
        subsystem="operator",
        mode="read_only_status",
        generated_at=now,
        overall_status=overall_status,
        live_trading_possible=live_trading_possible,
        live_trading_enabled_anywhere=live_trading_enabled_anywhere,
        venues=venue_statuses,
        trading212=OperatorTrading212StatusOut(
            strategies_count=len(t212_strategies),
            live_approved_strategies_count=sum(
                1 for strategy in t212_strategies if _is_live_approved(strategy)
            ),
            active_orders_count=await _count_orders(
                db,
                venue="t212",
                statuses=_ACTIVE_ORDER_STATUSES,
            ),
            recent_orders_count=await _count_orders(db, venue="t212", since=recent_since),
            latest_order_status=latest_t212_order.status if latest_t212_order else None,
            live_readiness_status=live_readiness_status,
            safety_notes=[
                "Trading212 summary uses persisted local state only.",
                "Live readiness is based on stored checklist and broker-test metadata.",
            ],
        ),
        kraken=OperatorKrakenStatusOut(
            strategies_count=len(kraken_strategies),
            paper_only_strategies_count=sum(
                1 for strategy in kraken_strategies if not _is_live_approved(strategy)
            ),
            live_enabled=kraken_live_enabled,
            recent_orders_count=await _count_orders(db, venue="kraken", since=recent_since),
            active_orders_count=await _count_orders(
                db,
                venue="kraken",
                statuses=_ACTIVE_ORDER_STATUSES,
            ),
            venue_config=next(
                (status for status in venue_statuses if status.venue == "kraken"),
                None,
            ),
            safety_notes=[
                "Kraken live execution remains disabled/unproven.",
                "No accepted Kraken live-readiness gate is recorded by this endpoint.",
            ],
        ),
        dca=OperatorDcaStatusOut(
            config_count=len(dca_configs),
            enabled_config_count=sum(1 for config in dca_configs if config.enabled),
            decision_count_total=len(dca_audits),
            buy_due_count=decision_counts.get("BUY_DUE", 0),
            blocked_count=sum(
                count
                for code, count in decision_counts.items()
                if code.startswith("BLOCKED_")
            ),
            skipped_count=sum(
                count
                for code, count in decision_counts.items()
                if code.startswith("SKIP_")
            ),
            total_paper_allocated_usd=sum(
                (state.total_allocated_usd for state in dca_states),
                Decimal("0"),
            ),
            scheduler_registered=scheduler_registered,
            scheduler_cadence=scheduler_cadence,
            worker_health=worker_health,
            runnable=False,
            live_enabled=dca_live_enabled,
            paper_only=True,
            tickers=sorted({
                *(config.ticker for config in dca_configs),
                *(state.ticker for state in dca_states),
                *audit_tickers,
            }),
        ),
        schedulers=OperatorSchedulersStatusOut(
            dca_paper_evaluate_registered=scheduler_registered,
            dca_paper_evaluate_cadence=scheduler_cadence,
            heartbeat_registered=heartbeat_registered,
            heartbeat_cadence=heartbeat_cadence,
            worker_health=worker_health,
            heartbeat_component=HEARTBEAT_COMPONENT,
            heartbeat_last_seen_at=heartbeat_last_seen_at,
            heartbeat_stale_after_seconds=HEARTBEAT_STALE_AFTER_SECONDS,
        ),
        recent_activity=[
            OperatorRecentActivityOut(
                id=audit.id,
                occurred_at=audit.occurred_at,
                action=audit.action,
                entity_type=audit.entity_type,
                entity_id=audit.entity_id,
                actor=audit.actor,
                payload_summary=_safe_payload_summary(_audit_payload(audit)),
            )
            for audit in recent_audit_rows
        ],
        safety_flags=OperatorSafetyFlagsOut(
            endpoint_read_only=True,
            creates_orders=False,
            calls_brokers=False,
            triggers_schedulers=False,
            runs_strategies=False,
            dca_runnable=dca_runnable,
            dca_live_enabled=dca_live_enabled,
            kraken_live_enabled=kraken_live_enabled,
            cash_only_mode=settings.CASH_ONLY_MODE,
            live_trading_enabled_setting=settings.LIVE_TRADING_ENABLED,
            app_live_trading_unlocked=app_live_trading_unlocked,
            any_venue_kill_switch_active=any_venue_kill_switch_active,
            any_venue_degraded=any_venue_degraded,
            missing_expected_venue_configs=missing_expected_venue_configs,
            worker_health_known=worker_health_known,
        ),
    )
