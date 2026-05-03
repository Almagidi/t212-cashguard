"""Operator visibility and safe config management for paper-only Kraken DCA."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from celery.schedules import crontab
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select

from app.api.deps import get_current_admin, get_current_user
from app.api.schemas import (
    DcaActivityConfigOut,
    DcaActivityReportOut,
    DcaActivitySafetyFlagsOut,
    DcaAuditEntryOut,
    DcaConfigCreate,
    DcaConfigOut,
    DcaConfigStatusOut,
    DcaConfigUpdate,
    DcaLatestStateOut,
    DcaOperatorStatusOut,
    DcaRecentDecisionOut,
    DcaSafetyFlagsOut,
    DcaTickerActivityOut,
)
from app.db.models import AuditLog, DcaConfig, DcaPlanState, Order, User
from app.db.repositories.dca_config_repo import DcaConfigRepository
from app.db.repositories.dca_plan_state_repo import DcaPlanStateRepository
from app.db.session import get_db
from app.strategies.kraken_dca_planner import KrakenDCAPlanner
from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/kraken/dca", tags=["kraken-dca"])

_DCA_BEAT_KEY = "dca-paper-evaluate"
_DCA_TASK = "app.workers.tasks_dca.evaluate_due_plans_task"


def _audit_config_change(
    db: AsyncSession,
    *,
    action: str,
    config: DcaConfig,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> None:
    audit_payload: dict[str, Any] = {
        "config_id": str(config.id),
        "ticker": config.ticker,
        "venue": config.venue,
        "paper_only": config.paper_only,
    }
    if payload:
        audit_payload.update(payload)
    db.add(
        AuditLog(
            action=action,
            entity_type="dca_config",
            entity_id=str(config.id),
            actor=actor,
            payload=audit_payload,
            occurred_at=datetime.now(UTC),
        )
    )


async def _get_config_or_404(
    repo: DcaConfigRepository,
    config_id: uuid.UUID,
) -> DcaConfig:
    config = await repo.get_by_id(config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="DCA config not found")
    return config


def _scheduler_entry() -> dict[str, Any] | None:
    entry = celery_app.conf.beat_schedule.get(_DCA_BEAT_KEY)
    if not isinstance(entry, dict):
        return None
    if entry.get("task") != _DCA_TASK:
        return None
    return entry


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


def _audit_payload(audit: AuditLog) -> dict[str, Any]:
    if isinstance(audit.payload, dict):
        return audit.payload
    return {}


def _payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _safe_decision_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "paper_only",
        "amount_usd",
        "mode",
        "next_scheduled_date",
        "evaluated_on",
    ):
        if key in payload:
            summary[key] = payload[key]
    return summary


def _decision_code(payload: dict[str, Any]) -> str | None:
    return _payload_str(payload, "decision_code")


def _count_decision(
    counts: dict[str, int],
    per_ticker_counts: dict[tuple[str, str], dict[str, int]],
    payload: dict[str, Any],
) -> None:
    code = _decision_code(payload)
    if code is None:
        return
    counts[code] = counts.get(code, 0) + 1

    ticker = _payload_str(payload, "ticker")
    venue = _payload_str(payload, "venue")
    if ticker is None or venue is None:
        return
    ticker_counts = per_ticker_counts.setdefault((ticker, venue), {})
    ticker_counts[code] = ticker_counts.get(code, 0) + 1


def _is_blocked_decision(code: str) -> bool:
    return code.startswith("BLOCKED_")


def _is_skipped_decision(code: str) -> bool:
    return code.startswith("SKIP_")


@router.get("/configs", response_model=list[DcaConfigOut])
async def list_dca_configs(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DcaConfig]:
    return list(await DcaConfigRepository(db).list_all())


@router.get("/configs/{config_id}", response_model=DcaConfigOut)
async def get_dca_config(
    config_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DcaConfig:
    return await _get_config_or_404(DcaConfigRepository(db), config_id)


@router.post(
    "/configs",
    response_model=DcaConfigOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_dca_config(
    body: DcaConfigCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> DcaConfig:
    repo = DcaConfigRepository(db)
    existing = await repo.get_by_ticker_venue(body.ticker, body.venue)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="DCA config already exists for ticker and venue",
        )

    config = await repo.create(
        DcaConfig(
            id=uuid.uuid4(),
            ticker=body.ticker,
            venue="kraken",
            cadence_days=body.cadence_days,
            fixed_cash_amount=body.fixed_cash_amount,
            dip_buy_enabled=body.dip_buy_enabled,
            dip_buy_multiplier=body.dip_buy_multiplier,
            min_cash_reserve=body.min_cash_reserve,
            max_position_percent=body.max_position_percent,
            paper_only=True,
            enabled=False,
        )
    )
    _audit_config_change(
        db,
        action="dca_config_created",
        config=config,
        actor=current_user.email,
        payload={"enabled": False},
    )
    await db.flush()
    await db.refresh(config)
    return config


@router.patch("/configs/{config_id}", response_model=DcaConfigOut)
async def update_dca_config(
    config_id: uuid.UUID,
    body: DcaConfigUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> DcaConfig:
    repo = DcaConfigRepository(db)
    config = await _get_config_or_404(repo, config_id)
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No DCA config changes supplied")
    if updates.get("enabled") is True and config.paper_only is not True:
        raise HTTPException(status_code=400, detail="Only paper-only DCA configs can be enabled")

    previous = {field: getattr(config, field) for field in updates}
    config = await repo.update(config, updates)
    _audit_config_change(
        db,
        action="dca_config_updated",
        config=config,
        actor=current_user.email,
        payload={
            "changed_fields": sorted(updates),
            "previous": previous,
            "new": updates,
        },
    )
    await db.flush()
    await db.refresh(config)
    return config


@router.post("/configs/{config_id}/enable", response_model=DcaConfigOut)
async def enable_dca_config(
    config_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> DcaConfig:
    repo = DcaConfigRepository(db)
    config = await _get_config_or_404(repo, config_id)
    if config.paper_only is not True:
        raise HTTPException(status_code=400, detail="Only paper-only DCA configs can be enabled")

    previous_enabled = config.enabled
    config = await repo.update(config, {"enabled": True})
    _audit_config_change(
        db,
        action="dca_config_enabled",
        config=config,
        actor=current_user.email,
        payload={
            "previous_enabled": previous_enabled,
            "new_enabled": True,
        },
    )
    await db.flush()
    await db.refresh(config)
    return config


@router.post("/configs/{config_id}/disable", response_model=DcaConfigOut)
async def disable_dca_config(
    config_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> DcaConfig:
    repo = DcaConfigRepository(db)
    config = await _get_config_or_404(repo, config_id)

    previous_enabled = config.enabled
    config = await repo.update(config, {"enabled": False})
    _audit_config_change(
        db,
        action="dca_config_disabled",
        config=config,
        actor=current_user.email,
        payload={
            "previous_enabled": previous_enabled,
            "new_enabled": False,
        },
    )
    await db.flush()
    await db.refresh(config)
    return config


@router.get("/status", response_model=DcaOperatorStatusOut)
async def dca_operator_status(
    audit_limit: int = Query(10, ge=1, le=20),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DcaOperatorStatusOut:
    config_result = await db.execute(
        select(DcaConfig).order_by(DcaConfig.ticker, DcaConfig.venue)
    )
    config_rows = list(config_result.scalars().all())

    state_rows = await DcaPlanStateRepository(db).list_all()
    states_by_key = {
        (state.ticker, state.venue): state
        for state in state_rows
    }

    audit_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "dca_paper_decision")
        .order_by(desc(AuditLog.occurred_at))
        .limit(audit_limit)
    )
    audit_rows = list(audit_result.scalars().all())

    scheduler_entry = _scheduler_entry()
    scheduler_registered = scheduler_entry is not None

    configs: list[DcaConfigStatusOut] = []
    for config in config_rows:
        state = states_by_key.get((config.ticker, config.venue))
        latest_state = None
        if state is not None:
            latest_state = DcaLatestStateOut.model_validate(state)
        configs.append(
            DcaConfigStatusOut.model_validate(
                {
                    "id": config.id,
                    "ticker": config.ticker,
                    "venue": config.venue,
                    "enabled": config.enabled,
                    "paper_only": config.paper_only,
                    "cadence_days": config.cadence_days,
                    "fixed_cash_amount": config.fixed_cash_amount,
                    "min_cash_reserve": config.min_cash_reserve,
                    "max_position_percent": config.max_position_percent,
                    "dip_buy_enabled": config.dip_buy_enabled,
                    "dip_buy_multiplier": config.dip_buy_multiplier,
                    "latest_state": latest_state,
                }
            )
        )

    return DcaOperatorStatusOut(
        subsystem="kraken_dca",
        mode="paper_only",
        runnable=False,
        live_enabled=False,
        scheduler_registered=scheduler_registered,
        scheduler_cadence=_readable_schedule(scheduler_entry),
        config_count=len(configs),
        enabled_config_count=sum(1 for config in config_rows if config.enabled),
        configs=configs,
        recent_audit_entries=[
            DcaAuditEntryOut(
                id=audit.id,
                created_at=audit.occurred_at,
                action=audit.action,
                entity_type=audit.entity_type,
                entity_id=audit.entity_id,
                actor=audit.actor,
                metadata=audit.payload,
            )
            for audit in audit_rows
        ],
        safety_flags=DcaSafetyFlagsOut(
            dca_planner_runnable_is_false=KrakenDCAPlanner.RUNNABLE is False,
            dca_planner_paper_only_is_true=KrakenDCAPlanner.PAPER_ONLY is True,
            main_runner_registered=False,
            order_creation_supported=False,
        ),
    )


@router.get("/activity", response_model=DcaActivityReportOut)
async def dca_paper_activity(
    audit_limit: int = Query(20, ge=1, le=100),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DcaActivityReportOut:
    config_rows = list(await DcaConfigRepository(db).list_all())
    state_rows = list(await DcaPlanStateRepository(db).list_all())
    states_by_key: dict[tuple[str, str], DcaPlanState] = {
        (state.ticker, state.venue): state
        for state in state_rows
    }
    configs_by_key = {
        (config.ticker, config.venue): config
        for config in config_rows
    }

    audit_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "dca_paper_decision")
        .order_by(desc(AuditLog.occurred_at))
    )
    audit_rows = list(audit_result.scalars().all())

    order_count_sanity = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.venue == "kraken")
        )
    ).scalar_one()

    decision_counts_by_code: dict[str, int] = {}
    per_ticker_counts: dict[tuple[str, str], dict[str, int]] = {}
    latest_audit_by_key: dict[tuple[str, str], AuditLog] = {}
    for audit in audit_rows:
        payload = _audit_payload(audit)
        _count_decision(decision_counts_by_code, per_ticker_counts, payload)
        ticker = _payload_str(payload, "ticker")
        venue = _payload_str(payload, "venue")
        if ticker is not None and venue is not None:
            latest_audit_by_key.setdefault((ticker, venue), audit)

    activity_keys = sorted(set(configs_by_key) | set(states_by_key) | set(per_ticker_counts))
    per_ticker_activity: list[DcaTickerActivityOut] = []
    for key in activity_keys:
        ticker, venue = key
        config = configs_by_key.get(key)
        state = states_by_key.get(key)
        latest_audit = latest_audit_by_key.get(key)
        latest_payload = _audit_payload(latest_audit) if latest_audit is not None else {}
        latest_code = _decision_code(latest_payload)
        latest_reason = _payload_str(latest_payload, "reason")
        if latest_code is None and state is not None:
            latest_code = state.last_decision_code
        if latest_reason is None and state is not None:
            latest_reason = state.last_reason

        per_ticker_activity.append(
            DcaTickerActivityOut(
                ticker=ticker,
                venue=venue,
                enabled=config.enabled if config is not None else False,
                latest_decision_code=latest_code,
                latest_decision_at=latest_audit.occurred_at if latest_audit is not None else None,
                latest_reason=latest_reason,
                total_allocated_usd=(
                    state.total_allocated_usd if state is not None else Decimal("0")
                ),
                executions_count=state.executions_count if state is not None else 0,
                last_buy_at=state.last_buy_at if state is not None else None,
                decision_counts_by_code=per_ticker_counts.get(key, {}),
            )
        )

    recent_decisions: list[DcaRecentDecisionOut] = []
    for audit in audit_rows[:audit_limit]:
        payload = _audit_payload(audit)
        recent_decisions.append(
            DcaRecentDecisionOut(
                audit_id=audit.id,
                occurred_at=audit.occurred_at,
                ticker=_payload_str(payload, "ticker"),
                venue=_payload_str(payload, "venue"),
                decision_code=_decision_code(payload),
                reason=_payload_str(payload, "reason"),
                payload_summary=_safe_decision_summary(payload),
            )
        )

    return DcaActivityReportOut(
        subsystem="kraken_dca",
        mode="paper_only",
        runnable=False,
        live_enabled=False,
        generated_at=datetime.now(UTC),
        config_count=len(config_rows),
        enabled_config_count=sum(1 for config in config_rows if config.enabled),
        decision_count_total=len(audit_rows),
        decision_counts_by_code=decision_counts_by_code,
        buy_due_count=decision_counts_by_code.get("BUY_DUE", 0),
        blocked_count=sum(
            count
            for code, count in decision_counts_by_code.items()
            if _is_blocked_decision(code)
        ),
        skipped_count=sum(
            count
            for code, count in decision_counts_by_code.items()
            if _is_skipped_decision(code)
        ),
        total_paper_allocated_usd=sum(
            (state.total_allocated_usd for state in state_rows),
            Decimal("0"),
        ),
        order_count_sanity=order_count_sanity,
        configs=[
            DcaActivityConfigOut.model_validate(config)
            for config in config_rows
        ],
        per_ticker_activity=per_ticker_activity,
        recent_decisions=recent_decisions,
        safety_flags=DcaActivitySafetyFlagsOut(
            dca_planner_runnable_is_false=KrakenDCAPlanner.RUNNABLE is False,
            dca_planner_paper_only_is_true=KrakenDCAPlanner.PAPER_ONLY is True,
            main_runner_registered=False,
            order_creation_supported=False,
            execution_called_by_report=False,
            provider_called_by_report=False,
            scheduler_triggered_by_report=False,
        ),
    )
