"""Read-only operator control tower status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from celery.schedules import crontab
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from app.api.deps import get_current_user
from app.api.schemas import (
    LiveReadinessStatus,
    OperatorBlockingReasonOut,
    OperatorDcaStatusOut,
    OperatorKrakenStatusOut,
    OperatorPaperExecutionStatusOut,
    OperatorProtectiveStopEventOut,
    OperatorProtectiveStopsOut,
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
    BrokerConnection,
    DcaConfig,
    DcaPlanState,
    Order,
    RiskEvent,
    Strategy,
    User,
    VenueConfig,
    WorkerHeartbeat,
)
from app.db.repositories.venue_config_repo import VenueConfigRepository
from app.db.repositories.worker_heartbeat_repo import WorkerHeartbeatRepository
from app.db.session import get_db
from app.execution.paper_engine import paper_execution_summary
from app.services.live_readiness import LiveReadinessError, LiveReadinessService
from app.services.worker_health import build_worker_health
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
_STRATEGY_SIGNALS_BEAT_KEY = "strategy-signals"
_STRATEGY_SIGNALS_TASK = "app.workers.tasks.run_strategy_signals"
_EXPECTED_VENUES = ("t212", "kraken")

# Persisted RiskEvent types that represent protective-stop activity. This is a
# read-only allowlist for operator visibility — surfacing an event here changes
# no enforcement behaviour. Kept explicit so unrelated event types are never
# leaked through the operator surface by accident.
_PROTECTIVE_RISK_EVENT_TYPES = (
    "kill_switch_on",
    "kill_switch_off",
    "kill_switch_block",
    "cash_guard_block",
    "daily_loss_breach",
    "consecutive_loss",
    "duplicate_order_block",
    "stale_data",
    "cooldown_block",
    "eod_flatten",
    "max_positions_block",
    "position_size_block",
    "max_trades_block",
    "sector_limit_block",
    "correlation_block",
    "cfd_size_block",
    "cfd_daily_loss_block",
    "cfd_leverage_block",
    "cfd_margin_block",
)
_KILL_SWITCH_EVENT_TYPES = ("kill_switch_on", "kill_switch_off")
_PROTECTIVE_EVENT_LIMIT = 10


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


def _strategy_signals_entry() -> dict[str, Any] | None:
    return _beat_entry(_STRATEGY_SIGNALS_BEAT_KEY, _STRATEGY_SIGNALS_TASK)


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


def _compute_blocking_reasons(
    *,
    cash_only_mode: bool,
    any_venue_kill_switch_active: bool,
    global_kill_switch_active: bool | None,
    missing_expected_venue_configs: bool,
    any_venue_degraded: bool,
    live_readiness_status: LiveReadinessStatus | None,
    worker_health_known: bool,
) -> list[OperatorBlockingReasonOut]:
    """Read-only explanation of ``overall_status``.

    Mirrors the exact boolean inputs the route already uses to compute
    ``overall_status`` (see the if/elif chain below in ``operator_status``).
    This must never introduce a condition that isn't already part of that
    computation, so the two can never drift apart.
    """
    reasons: list[OperatorBlockingReasonOut] = []
    if not cash_only_mode:
        reasons.append(
            OperatorBlockingReasonOut(
                code="cash_only_mode_disabled",
                severity="blocked",
                message="CASH_ONLY_MODE is disabled. Trading is blocked until cash-only mode is restored.",
            )
        )
    if any_venue_kill_switch_active:
        reasons.append(
            OperatorBlockingReasonOut(
                code="kill_switch_active",
                severity="blocked",
                message="A venue kill switch is active. Trading is blocked until it is cleared.",
            )
        )
    if global_kill_switch_active is True:
        reasons.append(
            OperatorBlockingReasonOut(
                code="global_kill_switch_active",
                severity="blocked",
                message=(
                    "The global kill switch is active. All trading is blocked "
                    "until it is cleared from Emergency Controls."
                ),
            )
        )
    if missing_expected_venue_configs:
        reasons.append(
            OperatorBlockingReasonOut(
                code="missing_venue_config",
                severity="degraded",
                message="One or more expected venue configs are missing. Status is fail-closed/degraded.",
            )
        )
    if any_venue_degraded:
        reasons.append(
            OperatorBlockingReasonOut(
                code="venue_degraded",
                severity="degraded",
                message="At least one venue is reporting degraded mode.",
            )
        )
    if live_readiness_status is None:
        reasons.append(
            OperatorBlockingReasonOut(
                code="live_readiness_unavailable",
                severity="degraded",
                message="Live readiness status could not be evaluated.",
            )
        )
    if not worker_health_known:
        reasons.append(
            OperatorBlockingReasonOut(
                code="worker_health_unknown",
                severity="degraded",
                message="Background worker health is not confirmed healthy (stale, missing, or unknown).",
            )
        )
    return reasons


def _protective_event_out(event: RiskEvent) -> OperatorProtectiveStopEventOut:
    """Sanitized, read-only projection of a persisted RiskEvent.

    Only coarse display fields are exposed — never the raw payload, which may
    carry position sizes or cash amounts beyond what the operator card needs.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    actor = payload.get("actor")
    return OperatorProtectiveStopEventOut(
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        message=event.message,
        ticker=event.ticker,
        actor=actor if isinstance(actor, str) and actor else None,
    )


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
    live_approved_at = promotion.get("live_approved_at") if isinstance(promotion, dict) else None
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
        "no_broker_order_sent",
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


async def _resolve_credential_source(
    db: AsyncSession,
    app_mode: str,
) -> Literal["stored_connection", "environment_fallback", "mock", "none"]:
    """Safe, read-only summary of which broker-credential source the runtime
    would use for the active ``APP_MODE``.

    Returns a coarse enum only — never a key, secret, encrypted blob, or
    decrypted value. The resolution order mirrors ``app.api.deps.get_broker``
    (mock → stored active connection → environment fallback → none) without
    performing any broker call or credential read, so this stays purely
    informational and changes no trading, provider, or auth behaviour.
    """
    if app_mode == "mock":
        return "mock"
    stored_connection_id = (
        await db.execute(
            select(BrokerConnection.id)
            .where(BrokerConnection.is_active.is_(True))
            .where(BrokerConnection.environment == app_mode)
            .limit(1)
        )
    ).scalar_one_or_none()
    if stored_connection_id is not None:
        return "stored_connection"
    if app_mode == "demo" and settings.T212_DEMO_API_KEY and settings.T212_DEMO_API_SECRET:
        return "environment_fallback"
    if app_mode == "live" and settings.T212_LIVE_API_KEY and settings.T212_LIVE_API_SECRET:
        return "environment_fallback"
    return "none"


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
        _venue_status_from_row(venues_by_name.get(venue), venue) for venue in _EXPECTED_VENUES
    ]

    strategy_rows = list((await db.execute(select(Strategy))).scalars().all())
    t212_strategies = [strategy for strategy in strategy_rows if strategy.venue == "t212"]
    kraken_strategies = [strategy for strategy in strategy_rows if strategy.venue == "kraken"]

    latest_t212_order = (
        await db.execute(
            select(Order).where(Order.venue == "t212").order_by(desc(Order.created_at)).limit(1)
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
        )
        .scalars()
        .all()
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

    strategy_signals_entry = _strategy_signals_entry()
    strategy_signals_registered = strategy_signals_entry is not None
    strategy_signals_cadence = _readable_schedule(strategy_signals_entry)
    task_health_report = await build_worker_health(db)
    strategy_signals_task_health = next(
        (
            task
            for task in task_health_report["tasks"]
            if task["task_name"] == "run_strategy_signals"
        ),
        None,
    )
    strategy_signals_observation_status = cast(
        'Literal["ok", "stale", "unknown"]',
        strategy_signals_task_health["status"] if strategy_signals_task_health else "unknown",
    )
    strategy_signals_last_seen_at = (
        strategy_signals_task_health["last_seen_at"] if strategy_signals_task_health else None
    )
    strategy_signals_observation_detail = (
        strategy_signals_task_health["detail"]
        if strategy_signals_task_health
        else "Task heartbeat has not been recorded yet."
    )

    recent_audit_rows = list(
        (await db.execute(select(AuditLog).order_by(desc(AuditLog.occurred_at)).limit(audit_limit)))
        .scalars()
        .all()
    )

    app_settings = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one_or_none()
    app_live_trading_unlocked = bool(
        app_settings.live_trading_unlocked if app_settings is not None else False
    )
    global_kill_switch_active = (
        bool(app_settings.kill_switch_active) if app_settings is not None else None
    )
    global_auto_trading_enabled = (
        bool(app_settings.auto_trading_enabled) if app_settings is not None else None
    )

    recent_protective_events = list(
        (
            await db.execute(
                select(RiskEvent)
                .where(RiskEvent.event_type.in_(_PROTECTIVE_RISK_EVENT_TYPES))
                .order_by(desc(RiskEvent.occurred_at))
                .limit(_PROTECTIVE_EVENT_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    last_kill_switch_event = (
        await db.execute(
            select(RiskEvent)
            .where(RiskEvent.event_type.in_(_KILL_SWITCH_EVENT_TYPES))
            .order_by(desc(RiskEvent.occurred_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if global_kill_switch_active is None:
        protective_stops_status: Literal["ok", "triggered", "unknown"] = "unknown"
    elif global_kill_switch_active:
        protective_stops_status = "triggered"
    else:
        protective_stops_status = "ok"

    any_venue_kill_switch_active = any(
        status.kill_switch_active is True for status in venue_statuses
    )
    any_venue_degraded = any(status.degraded_mode_active is True for status in venue_statuses)
    missing_expected_venue_configs = any(not status.present for status in venue_statuses)
    worker_health_known = worker_health == "healthy"
    credential_source = await _resolve_credential_source(db, settings.APP_MODE)
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

    if (
        not settings.CASH_ONLY_MODE
        or any_venue_kill_switch_active
        or global_kill_switch_active is True
    ):
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

    why_blocked = _compute_blocking_reasons(
        cash_only_mode=settings.CASH_ONLY_MODE,
        any_venue_kill_switch_active=any_venue_kill_switch_active,
        global_kill_switch_active=global_kill_switch_active,
        missing_expected_venue_configs=missing_expected_venue_configs,
        any_venue_degraded=any_venue_degraded,
        live_readiness_status=live_readiness_status,
        worker_health_known=worker_health_known,
    )

    return OperatorStatusOut(
        subsystem="operator",
        mode="read_only_status",
        generated_at=now,
        overall_status=overall_status,
        why_blocked=why_blocked,
        protective_stops=OperatorProtectiveStopsOut(
            status=protective_stops_status,
            global_kill_switch_active=global_kill_switch_active,
            global_auto_trading_enabled=global_auto_trading_enabled,
            last_kill_switch_event=(
                _protective_event_out(last_kill_switch_event)
                if last_kill_switch_event is not None
                else None
            ),
            recent_events=[_protective_event_out(event) for event in recent_protective_events],
            safety_notes=[
                "Read-only surface. No reset, clear, enable, or disable controls exist here.",
                "Global kill switch is persisted app state; per-venue kill switches are "
                "shown separately in the venue cards.",
                "Circuit-breaker activations appear as kill-switch events with actor "
                "'circuit_breaker:<name>'. In-process circuit state is not persisted "
                "and is not shown.",
            ],
        ),
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
                count for code, count in decision_counts.items() if code.startswith("BLOCKED_")
            ),
            skipped_count=sum(
                count for code, count in decision_counts.items() if code.startswith("SKIP_")
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
            tickers=sorted(
                {
                    *(config.ticker for config in dca_configs),
                    *(state.ticker for state in dca_states),
                    *audit_tickers,
                }
            ),
        ),
        paper_execution=OperatorPaperExecutionStatusOut(**(await paper_execution_summary(db))),
        schedulers=OperatorSchedulersStatusOut(
            dca_paper_evaluate_registered=scheduler_registered,
            dca_paper_evaluate_cadence=scheduler_cadence,
            heartbeat_registered=heartbeat_registered,
            heartbeat_cadence=heartbeat_cadence,
            worker_health=worker_health,
            heartbeat_component=HEARTBEAT_COMPONENT,
            heartbeat_last_seen_at=heartbeat_last_seen_at,
            heartbeat_stale_after_seconds=HEARTBEAT_STALE_AFTER_SECONDS,
            strategy_signals_registered=strategy_signals_registered,
            strategy_signals_cadence=strategy_signals_cadence,
            strategy_signals_task_name=_STRATEGY_SIGNALS_TASK,
            strategy_signals_observation_status=strategy_signals_observation_status,
            strategy_signals_last_seen_at=strategy_signals_last_seen_at,
            strategy_signals_observation_detail=strategy_signals_observation_detail,
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
            unrealized_pnl_failure_policy=settings.POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY,
            credentials_configured=credential_source != "none",
            credential_source=credential_source,
        ),
    )
