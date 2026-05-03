"""Paper-only scheduled DCA evaluation tasks.

This module is intentionally separate from ``app.workers.tasks`` and the
strategy runner. DCA remains a planner/evaluator, not a runnable signal strategy.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, select

from app.db.models import AuditLog, BrokerAccountSnapshot, BrokerConnection
from app.db.repositories.dca_config_repo import DcaConfigRepository, dca_config_from_row
from app.db.repositories.dca_plan_state_repo import DcaPlanStateRepository, dca_state_from_row
from app.market_data import get_kraken_provider
from app.strategies import kraken_dca_planner as dca_planner
from app.strategies.indicators import Bar
from app.workers.celery_app import celery_app
from app.workers.tasks import run_monitored_task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


def _assert_paper_only_invariants() -> None:
    if dca_planner.KrakenDCAPlanner.RUNNABLE is not False:
        raise RuntimeError("DCA scheduler fail-safe: KrakenDCAPlanner.RUNNABLE must remain False")
    if dca_planner.KrakenDCAPlanner.PAPER_ONLY is not True:
        raise RuntimeError("DCA scheduler fail-safe: KrakenDCAPlanner.PAPER_ONLY must remain True")


async def _latest_kraken_account_values(db: AsyncSession) -> tuple[Decimal, Decimal]:
    result = await db.execute(
        select(BrokerAccountSnapshot)
        .join(BrokerConnection, BrokerAccountSnapshot.connection_id == BrokerConnection.id)
        .where(
            BrokerConnection.broker == "kraken",
            BrokerConnection.is_active.is_(True),
        )
        .order_by(desc(BrokerAccountSnapshot.snapshotted_at))
        .limit(1)
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        log.warning(
            "dca_scheduler.no_kraken_account_snapshot",
            message="DCA evaluation will use zero cash/account values and remain blocked.",
        )
        return Decimal("0"), Decimal("0")
    return snapshot.free_funds, snapshot.total_value


async def _fetch_kraken_market_inputs(
    provider_factory: Callable[[], Any],
    config: dca_planner.DCAConfig,
) -> tuple[Decimal, list[Bar]]:
    async with provider_factory() as provider:
        quote = await provider.get_quote(config.ticker)
        raw_bars = await provider.get_bars(
            config.ticker,
            multiplier=1440,
            timespan="minute",
            limit=config.dip_ema_period,
        )
    bars = [
        Bar(
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in raw_bars
    ]
    return quote.last, bars


def _updates_for_decision(
    state: dca_planner.DCAState,
    decision: dca_planner.DCADecision,
    now: date,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "last_decision_at": now,
        "last_decision_code": decision.code.value,
        "last_reason": decision.reason,
    }
    if decision.code == dca_planner.DCADecisionCode.BUY_DUE:
        updates.update(
            {
                "last_buy_at": now,
                "total_allocated_usd": state.total_allocated_usd + decision.amount_usd,
                "executions_count": state.executions_count + 1,
            }
        )
    return updates


def _audit_decision(
    db: AsyncSession,
    config: dca_planner.DCAConfig,
    decision: dca_planner.DCADecision,
    current_price: Decimal,
    now: date,
) -> None:
    db.add(
        AuditLog(
            action="dca_paper_decision",
            entity_type="dca_plan_state",
            entity_id=f"{config.venue}:{config.ticker}",
            actor="worker:dca_scheduler",
            payload={
                "ticker": config.ticker,
                "venue": config.venue,
                "paper_only": config.paper_only,
                "decision_code": decision.code.value,
                "amount_usd": decision.amount_usd,
                "mode": decision.mode,
                "reason": decision.reason,
                "next_scheduled_date": decision.next_scheduled_date,
                "current_price": current_price,
                "evaluated_on": now.isoformat(),
            },
        )
    )


async def evaluate_due_plans(
    db: AsyncSession,
    *,
    now: date | None = None,
    provider_factory: Callable[[], Any] = get_kraken_provider,
    config_loader: Callable[[], Sequence[dca_planner.DCAConfig]] | None = None,
    planner_factory: Callable[[], dca_planner.KrakenDCAPlanner] = dca_planner.KrakenDCAPlanner,
    available_cash: Decimal | None = None,
    account_value: Decimal | None = None,
) -> dict[str, Any]:
    """Evaluate all configured DCA plans and persist paper-only decisions."""
    _assert_paper_only_invariants()

    run_date = now or date.today()
    configs: Sequence[dca_planner.DCAConfig]
    if config_loader is None:
        config_rows = await DcaConfigRepository(db).list_enabled()
        configs = [dca_config_from_row(row) for row in config_rows]
    else:
        configs = config_loader()
    summary: dict[str, Any] = {
        "evaluated": 0,
        "buy_due": 0,
        "non_buy": 0,
        "errors": [],
        "paper_only": True,
    }
    if not configs:
        await db.commit()
        log.info("dca_scheduler.complete", **summary)
        return summary

    cash, total = (
        (available_cash, account_value)
        if available_cash is not None and account_value is not None
        else await _latest_kraken_account_values(db)
    )
    repo = DcaPlanStateRepository(db)
    planner = planner_factory()

    for config in configs:
        if not config.enabled:
            continue
        if not config.paper_only:
            log.error(
                "dca_scheduler.config_not_paper_only",
                ticker=config.ticker,
                venue=config.venue,
            )
            summary["errors"].append({
                "ticker": config.ticker,
                "error": "DCA config must remain paper_only",
            })
            continue
        try:
            row = await repo.get_by_ticker_venue(config.ticker, config.venue)
            state = dca_state_from_row(row) if row is not None else dca_planner.DCAState(
                ticker=config.ticker,
                venue=config.venue,
            )
            current_price, bars = await _fetch_kraken_market_inputs(provider_factory, config)
            decision = planner.evaluate_plan(
                config=config,
                state=state,
                current_price=current_price,
                available_cash=cash,
                account_value=total,
                bars=bars,
                now=run_date,
            )
            await repo.upsert(
                config.ticker,
                config.venue,
                _updates_for_decision(state, decision, run_date),
            )
            _audit_decision(db, config, decision, current_price, run_date)
            summary["evaluated"] += 1
            if decision.code == dca_planner.DCADecisionCode.BUY_DUE:
                summary["buy_due"] += 1
            else:
                summary["non_buy"] += 1
        except Exception as exc:
            log.exception("dca_scheduler.plan_failed", ticker=config.ticker, error=str(exc))
            summary["errors"].append({"ticker": config.ticker, "error": str(exc)})

    await db.commit()
    log.info("dca_scheduler.complete", **summary)
    return summary


@celery_app.task(name="app.workers.tasks_dca.evaluate_due_plans_task", bind=True, max_retries=0, time_limit=120)
def evaluate_due_plans_task(self: Any) -> Any:
    """Daily paper-only DCA scheduler entrypoint."""

    async def _run() -> dict[str, Any]:
        from app.core.redis import task_lock
        from app.db.session import AsyncSessionLocal

        async with task_lock("evaluate_due_dca_plans", ttl_seconds=900) as acquired:
            if not acquired:
                log.debug("dca_scheduler.skipped_locked")
                return {"skipped": True, "reason": "already_running"}
            async with AsyncSessionLocal() as db:
                return await evaluate_due_plans(db)

    return run_monitored_task("evaluate_due_dca_plans", _run)
