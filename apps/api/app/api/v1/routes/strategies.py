"""Strategies routes."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import aliased

from app.api.deps import get_current_admin, get_current_user
from app.api.schemas import (
    AllocatorDecisionOut,
    MarketDataHealth,
    MarketDataSymbolHealth,
    MarketRegimeOut,
    PortfolioRebalanceOrderOut,
    PortfolioStrategyAttributionOut,
    PortfolioStrategyAttributionSummaryOut,
    PortfolioStrategyMonitoringOut,
    PortfolioWeightSnapshot,
    RiskEventOut,
    SignalOut,
    StrategyCreate,
    StrategyIntelligenceOut,
    StrategyPromotionActionRequest,
    StrategyPromotionStatus,
    StrategyOut,
    StrategyPresetCreate,
    StrategyPresetInfo,
    StrategyPresetKey,
    StrategyUpdate,
    WatchlistCandidateContextOut,
)
from app.backtest.portfolio_strategies import is_portfolio_strategy_type
from app.db.models import AuditLog, Order, RiskEvent, Signal, Strategy, User
from app.db.repositories import StrategyRepository
from app.db.session import get_db
from app.services.feed_health import get_feed_health_snapshot
from app.services.market_regime import MarketRegimeService
from app.services.portfolio_attribution_service import PortfolioAttributionService
from app.services.portfolio_execution_service import PortfolioExecutionService
from app.services.strategy_presets import (
    build_unique_strategy_name,
    ensure_preset_risk_profile,
    get_strategy_preset,
    list_strategy_presets,
)
from app.services.strategy_promotion import StrategyPromotionError, StrategyPromotionService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _parse_portfolio_state(strategy: Strategy) -> dict[str, object]:
    return dict((strategy.params or {}).get("portfolio_execution", {}))


def _parse_state_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _build_weight_snapshots(strategy: Strategy) -> list[PortfolioWeightSnapshot]:
    state = _parse_portfolio_state(strategy)
    target_weights_raw = state.get("last_target_weights")
    current_weights_raw = state.get("last_current_weights")
    target_weights = target_weights_raw if isinstance(target_weights_raw, dict) else {}
    current_weights = current_weights_raw if isinstance(current_weights_raw, dict) else {}
    tickers = sorted(
        {
            str(ticker).upper()
            for ticker in (
                list(target_weights.keys())
                + list(current_weights.keys())
                + list(strategy.allowed_tickers)
            )
        }
    )
    snapshots: list[PortfolioWeightSnapshot] = []
    for ticker in tickers:
        target = target_weights.get(ticker)
        current = current_weights.get(ticker)
        target_weight = float(target) if target is not None else None
        current_weight = float(current) if current is not None else None
        delta_weight = None
        if target_weight is not None or current_weight is not None:
            delta_weight = (target_weight or 0.0) - (current_weight or 0.0)
        snapshots.append(
            PortfolioWeightSnapshot(
                ticker=ticker,
                target_weight=target_weight,
                current_weight=current_weight,
                delta_weight=delta_weight,
            )
        )
    return snapshots


def _allocation_decision_from_payload(payload: object) -> AllocatorDecisionOut | None:
    if not isinstance(payload, dict):
        return None
    try:
        return AllocatorDecisionOut(**payload)
    except Exception:
        return None


def _feed_symbol_map() -> dict[str, dict[str, object]]:
    snapshot = get_feed_health_snapshot()
    return {
        str(symbol.get("ticker") or "").upper(): symbol
        for symbol in snapshot.get("symbols", [])
        if symbol.get("ticker")
    }


def _watchlist_candidate_context(
    strategy: Strategy,
    *,
    regime: dict[str, object],
    feed_symbols: dict[str, dict[str, object]],
) -> list[WatchlistCandidateContextOut]:
    params = strategy.params or {}
    raw_watchlist = params.get("todays_watchlist")
    watchlist = (
        [str(item).upper() for item in raw_watchlist]
        if isinstance(raw_watchlist, list) and raw_watchlist
        else [str(item).upper() for item in strategy.allowed_tickers]
    )
    raw_context = params.get("watchlist_candidates")
    context_map = raw_context if isinstance(raw_context, dict) else {}
    suppressed = {
        str(item)
        for item in regime.get("suppressed_strategies", [])
        if item
    }
    candidates: list[WatchlistCandidateContextOut] = []
    for ticker in watchlist:
        ctx = context_map.get(ticker) if isinstance(context_map.get(ticker), dict) else {}
        feed_symbol = feed_symbols.get(ticker, {})
        feed_status = str(feed_symbol.get("status") or "unknown")
        catalyst_score = float(ctx.get("catalyst_score", 0.0) or 0.0) if ctx else None
        catalyst_event_type = str(ctx.get("catalyst_event_type") or "") or None if ctx else None
        blocked_reason: str | None = None
        if strategy.type in suppressed:
            blocked_reason = f"Suppressed in {regime.get('label', regime.get('regime', 'current'))} regime."
        elif feed_status not in {"ok", "fallback", "unknown"}:
            blocked_reason = str(feed_symbol.get("detail") or f"Feed health is {feed_status}.")
        elif (
            strategy.type in {"opening_fade", "vwap_reclaim"}
            and (catalyst_score or 0.0) >= 0.7
            and catalyst_event_type in {"earnings", "guidance", "m&a", "legal_regulatory"}
        ):
            blocked_reason = f"Fresh {catalyst_event_type} catalyst weakens mean-reversion quality."
        candidates.append(
            WatchlistCandidateContextOut(
                ticker=ticker,
                score=float(ctx.get("score", 0.0) or 0.0),
                reason=str(ctx.get("reason") or "") or None,
                strategy_type=str(ctx.get("strategy_type") or "") or None,
                pre_market_rvol=float(ctx["pre_market_rvol"]) if ctx.get("pre_market_rvol") is not None else None,
                gap_pct=float(ctx["gap_pct"]) if ctx.get("gap_pct") is not None else None,
                catalyst_score=catalyst_score,
                catalyst_event_type=catalyst_event_type,
                catalyst_summary=str(ctx.get("catalyst_summary") or "") or None,
                catalyst_source=str(ctx.get("catalyst_source") or "") or None,
                feed_status=feed_status,
                blocked_reason=blocked_reason,
                trade_safe=blocked_reason is None,
            )
        )
    return sorted(candidates, key=lambda item: item.score, reverse=True)


async def _recent_strategy_risk_blocks(
    db: AsyncSession,
    strategy: Strategy,
    *,
    limit: int,
) -> list[RiskEventOut]:
    params = strategy.params or {}
    raw_watchlist = params.get("todays_watchlist")
    watchlist = (
        [str(item).upper() for item in raw_watchlist]
        if isinstance(raw_watchlist, list) and raw_watchlist
        else [str(item).upper() for item in strategy.allowed_tickers]
    )
    query = (
        select(RiskEvent)
        .where(
            RiskEvent.ticker.in_(watchlist) if watchlist else RiskEvent.ticker.isnot(None),
            RiskEvent.event_type.in_([
                "regime_block",
                "feed_health_block",
                "feed_symbol_block",
                "event_risk_block",
                "sector_limit_block",
                "correlation_block",
                "portfolio_heat_block",
                "duplicate_order_block",
                "cooldown_block",
                "max_positions_block",
                "max_trades_block",
            ]),
        )
        .order_by(desc(RiskEvent.occurred_at))
        .limit(limit)
    )
    return [RiskEventOut.model_validate(item) for item in (await db.execute(query)).scalars().all()]


async def _recent_strategy_allocation_decisions(
    db: AsyncSession,
    strategy: Strategy,
    *,
    limit: int,
) -> list[AllocatorDecisionOut]:
    result = await db.execute(
        select(Signal)
        .where(Signal.strategy_id == strategy.id)
        .order_by(desc(Signal.generated_at))
        .limit(max(limit * 4, 20))
    )
    decisions: list[AllocatorDecisionOut] = []
    for signal in result.scalars().all():
        decision = _allocation_decision_from_payload((signal.params_snapshot or {}).get("allocation"))
        if decision is not None:
            decisions.append(decision)
        if len(decisions) >= limit:
            break
    return decisions


async def _fetch_recent_rebalance_orders(
    db: AsyncSession,
    strategy_id: uuid.UUID,
    *,
    limit: int,
) -> list[PortfolioRebalanceOrderOut]:
    signal_alias = aliased(Signal)
    result = await db.execute(
        select(Order, signal_alias)
        .join(signal_alias, Order.signal_id == signal_alias.id)
        .where(
            signal_alias.strategy_id == strategy_id,
            signal_alias.signal_type == "portfolio_rebalance",
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    recent_orders: list[PortfolioRebalanceOrderOut] = []
    for order, signal in rows:
        params_snapshot = signal.params_snapshot or {}
        target_weight = params_snapshot.get("target_weight")
        allocation = params_snapshot.get("allocation") if isinstance(params_snapshot.get("allocation"), dict) else {}
        recent_orders.append(
            PortfolioRebalanceOrderOut(
                order_id=order.id,
                signal_id=order.signal_id,
                ticker=order.ticker,
                side=order.side,
                status=order.status,
                quantity=float(order.quantity),
                avg_fill_price=float(order.avg_fill_price) if order.avg_fill_price is not None else None,
                target_weight=float(target_weight) if target_weight is not None else None,
                allocation_status=allocation.get("status") if allocation else None,
                allocation_score=float(allocation["score"]) if allocation.get("score") is not None else None,
                allocation_reason=str(allocation.get("reason") or "") or None,
                is_dry_run=order.is_dry_run,
                created_at=order.created_at,
            )
        )
    return recent_orders


async def _build_portfolio_monitoring(
    db: AsyncSession,
    strategy: Strategy,
    *,
    order_limit: int,
) -> PortfolioStrategyMonitoringOut:
    state = _parse_portfolio_state(strategy)
    raw_decisions = state.get("last_allocation_decisions")
    allocation_decisions = [
        decision
        for decision in (
            _allocation_decision_from_payload(item)
            for item in (raw_decisions if isinstance(raw_decisions, list) else [])
        )
        if decision is not None
    ]
    return PortfolioStrategyMonitoringOut(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        strategy_type=strategy.type,
        is_enabled=strategy.is_enabled,
        is_live=strategy.is_live,
        last_status=str(state.get("last_status")) if state.get("last_status") is not None else None,
        last_reason=str(state.get("last_reason")) if state.get("last_reason") is not None else None,
        last_run_at=_parse_state_datetime(state.get("last_run_at")),
        last_rebalance_at=_parse_state_datetime(state.get("last_rebalance_signal_at")),
        last_mode=str(state.get("last_mode")) if state.get("last_mode") is not None else None,
        last_orders_submitted=int(state.get("last_orders_submitted", 0) or 0),
        last_dry_run_orders=int(state.get("last_dry_run_orders", 0) or 0),
        last_risk_blocks=int(state.get("last_risk_blocks", 0) or 0),
        last_allocation_blocks=int(state.get("last_allocation_blocks", 0) or 0),
        last_allocation_decisions=allocation_decisions,
        weights=_build_weight_snapshots(strategy),
        recent_orders=await _fetch_recent_rebalance_orders(db, strategy.id, limit=order_limit),
    )


@router.get("", response_model=list[StrategyOut])
async def list_strategies(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    return await repo.list_all()


@router.get("/presets", response_model=list[StrategyPresetInfo])
async def get_strategy_presets(
    _: User = Depends(get_current_user),
):
    presets = list_strategy_presets()
    return [
        StrategyPresetInfo(
            key=preset.key,
            label=preset.label,
            strategy_type=preset.key,
            description=preset.description,
            style=preset.style,
            session_window=preset.session_window,
            default_tickers=list(preset.default_tickers),
            default_params=dict(preset.strategy_params),
            risk_template_name=preset.risk_template.name,
            risk_summary=preset.risk_template.summary,
        )
        for preset in presets
    ]


@router.post("/presets/{preset_key}", response_model=StrategyOut, status_code=201)
async def create_strategy_from_preset(
    preset_key: StrategyPresetKey,
    body: StrategyPresetCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    preset = get_strategy_preset(preset_key)
    risk_profile = await ensure_preset_risk_profile(db, preset.key)
    requested_name = body.name.strip() if body.name else f"{preset.label} Demo"
    strategy_name = await build_unique_strategy_name(db, requested_name)
    tickers = body.allowed_tickers if body.allowed_tickers else list(preset.default_tickers)
    params = {
        **dict(preset.strategy_params),
        "preset_metadata": {
            "preset_key": preset.key,
            "preset_label": preset.label,
            "risk_template_name": risk_profile.name,
        },
        "execution_metadata": {
            "created_from_preset_at": datetime.now(UTC).isoformat(),
            "created_from_preset_by": current_user.email,
        },
    }
    strategy = Strategy(
        id=uuid.uuid4(),
        name=strategy_name,
        type=preset.key,
        description=preset.description,
        is_enabled=False,
        is_live=False,
        risk_profile_id=risk_profile.id,
        params=params,
        allowed_tickers=tickers,
        session_start=preset.session_start,
        session_end=preset.session_end,
        extended_hours=preset.extended_hours,
        eod_flatten=preset.eod_flatten,
    )
    repo = StrategyRepository(db)
    await repo.create(strategy)
    db.add(AuditLog(
        action="strategy_created_from_preset",
        entity_type="strategy",
        entity_id=str(strategy.id),
        actor=current_user.email,
        payload={
            "preset_key": preset.key,
            "risk_profile_id": str(risk_profile.id),
            "risk_profile_name": risk_profile.name,
        },
        occurred_at=datetime.now(UTC),
    ))
    await db.refresh(strategy)
    hydrated = await repo.get_by_id(strategy.id)
    return hydrated or strategy


@router.get("/portfolio-monitoring", response_model=list[PortfolioStrategyMonitoringOut])
async def list_portfolio_monitoring(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    strategies = await repo.list_all()
    portfolio_strategies = [
        strategy
        for strategy in strategies
        if is_portfolio_strategy_type(strategy.type)
    ]
    return [
        await _build_portfolio_monitoring(db, strategy, order_limit=3)
        for strategy in portfolio_strategies
    ]


@router.get("/portfolio-attribution", response_model=list[PortfolioStrategyAttributionSummaryOut])
async def list_portfolio_attribution(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    strategies = await repo.list_all()
    attribution = PortfolioAttributionService(db)
    portfolio_strategies = [
        strategy
        for strategy in strategies
        if is_portfolio_strategy_type(strategy.type)
    ]
    return [
        await attribution.build_summary(strategy)
        for strategy in portfolio_strategies
    ]


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(
    body: StrategyCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    strategy = Strategy(id=uuid.uuid4(), **body.model_dump(), is_enabled=False, is_live=False)
    await repo.create(strategy)
    db.add(AuditLog(
        action="strategy_created", entity_type="strategy",
        entity_id=str(strategy.id), actor=current_user.email,
        occurred_at=datetime.now(UTC),
    ))
    await db.refresh(strategy)
    hydrated = await repo.get_by_id(strategy.id)
    return hydrated or strategy


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return s


@router.get("/{strategy_id}/promotion-status", response_model=StrategyPromotionStatus)
async def get_strategy_promotion_status(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        status = await StrategyPromotionService(db).evaluate(strategy_id)
    except StrategyPromotionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StrategyPromotionStatus(**status)


@router.post("/{strategy_id}/promotion", response_model=StrategyPromotionStatus)
async def update_strategy_promotion(
    strategy_id: uuid.UUID,
    body: StrategyPromotionActionRequest,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        status = await StrategyPromotionService(db).apply_action(
            strategy_id=strategy_id,
            action=body.action,
            actor=current_user.email,
            notes=body.notes,
        )
    except StrategyPromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StrategyPromotionStatus(**status)


@router.get("/{strategy_id}/intelligence", response_model=StrategyIntelligenceOut)
async def get_strategy_intelligence(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    strategy = await repo.get_by_id(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    regime_payload = await MarketRegimeService().evaluate()
    feed_snapshot = get_feed_health_snapshot()
    feed_symbols = _feed_symbol_map()

    return StrategyIntelligenceOut(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        strategy_type=strategy.type,
        regime=MarketRegimeOut(**regime_payload),
        feed_health=MarketDataHealth(
            status=feed_snapshot["status"],
            provider=feed_snapshot["provider"],
            checked_at=feed_snapshot["checked_at"],
            detail=feed_snapshot["detail"],
            symbols=[MarketDataSymbolHealth(**symbol) for symbol in feed_snapshot["symbols"]],
        ),
        watchlist=_watchlist_candidate_context(
            strategy,
            regime=regime_payload,
            feed_symbols=feed_symbols,
        ),
        recent_risk_blocks=await _recent_strategy_risk_blocks(db, strategy, limit=8),
        recent_allocation_decisions=await _recent_strategy_allocation_decisions(db, strategy, limit=8),
    )


@router.get("/{strategy_id}/portfolio-attribution", response_model=PortfolioStrategyAttributionOut)
async def get_portfolio_attribution(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    strategy = await repo.get_by_id(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if not is_portfolio_strategy_type(strategy.type):
        raise HTTPException(status_code=400, detail="Strategy is not a portfolio rebalance strategy")
    attribution = PortfolioAttributionService(db)
    return await attribution.build_strategy_attribution(strategy)


@router.get("/{strategy_id}/portfolio-monitoring", response_model=PortfolioStrategyMonitoringOut)
async def get_portfolio_monitoring(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if not is_portfolio_strategy_type(s.type):
        raise HTTPException(status_code=400, detail="Strategy is not a portfolio rebalance strategy")
    return await _build_portfolio_monitoring(db, s, order_limit=10)


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: uuid.UUID,
    body: StrategyUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    requested_updates = body.model_dump(exclude_none=True)
    if requested_updates.get("is_live") is True and not s.is_live:
        raise HTTPException(
            status_code=400,
            detail="Directly enabling broker execution is blocked. Use the strategy promotion flow instead.",
        )
    for field, value in requested_updates.items():
        setattr(s, field, value)
    db.add(AuditLog(
        action="strategy_updated", entity_type="strategy",
        entity_id=str(strategy_id), actor=current_user.email,
        occurred_at=datetime.now(UTC),
    ))
    await db.flush()
    await db.refresh(s)
    hydrated = await repo.get_by_id(strategy_id)
    return hydrated or s


@router.post("/{strategy_id}/enable")
async def enable_strategy(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    s.is_enabled = True
    db.add(AuditLog(action="strategy_enabled", entity_type="strategy",
                    entity_id=str(strategy_id), actor=current_user.email,
                    occurred_at=datetime.now(UTC)))
    return {"enabled": True}


@router.post("/{strategy_id}/disable")
async def disable_strategy(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    s.is_enabled = False
    db.add(AuditLog(action="strategy_disabled", entity_type="strategy",
                    entity_id=str(strategy_id), actor=current_user.email,
                    occurred_at=datetime.now(UTC)))
    return {"enabled": False}


@router.get("/{strategy_id}/signals", response_model=list[SignalOut])
async def get_strategy_signals(
    strategy_id: uuid.UUID,
    limit: int = 50,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    return await repo.get_signals(strategy_id, limit=limit)


@router.post("/{strategy_id}/run-dry")
async def run_strategy_dry(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = StrategyRepository(db)
    s = await repo.get_by_id(strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    params = dict(s.params or {})
    execution_metadata = dict(params.get("execution_metadata", {}))
    execution_metadata["last_dry_run_requested_at"] = datetime.now(UTC).isoformat()
    execution_metadata["last_dry_run_requested_by"] = current_user.email
    params["execution_metadata"] = execution_metadata
    s.params = params
    if is_portfolio_strategy_type(s.type):
        service = PortfolioExecutionService(db)
        summary = await service.run_strategy_by_id(
            s.id,
            force=True,
            actor=f"user:{current_user.email}:dry_run",
            override_is_live=False,
        )
        db.add(AuditLog(action="strategy_dry_run", entity_type="strategy",
                        entity_id=str(strategy_id), actor=current_user.email,
                        payload=summary,
                        occurred_at=datetime.now(UTC)))
        return {"message": f"Dry run executed for {s.name}", "is_live": False, "summary": summary}
    db.add(AuditLog(action="strategy_dry_run", entity_type="strategy",
                    entity_id=str(strategy_id), actor=current_user.email,
                    occurred_at=datetime.now(UTC)))
    return {"message": f"Dry run queued for {s.name}", "is_live": False}
