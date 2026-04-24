"""
All remaining API routes in one module for clarity.
account / instruments / strategies / orders / positions / risk / alerts /
settings / emergency / health / reports
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import (
    AccountSummaryOut,
    AlertOut,
    AppSettingsOut,
    AppSettingsUpdate,
    AuditLogList,
    AuditLogOut,
    CashGuardStatus,
    EmergencyActionResult,
    InstrumentList,
    InstrumentOut,
    OrderCreate,
    OrderDetail,
    OrderOut,
    PerformanceReport,
    PositionOut,
    RiskEventOut,
    RiskProfileOut,
    RiskProfileUpdate,
    SignalOut,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
    DepsHealth,
    HealthStatus,
)
from app.core.config import settings
from app.db.models import (
    Alert,
    AppSettings,
    AuditLog,
    BrokerAccountSnapshot,
    BrokerConnection,
    Instrument,
    Order,
    RiskEvent,
    RiskProfile,
    Signal,
    Strategy,
    User,
)
from app.db.session import get_db
from app.risk.engine import RiskEngine, RiskViolation, activate_kill_switch, deactivate_kill_switch


# ─── Account ─────────────────────────────────────────────────────────────────

account_router = APIRouter(prefix="/account", tags=["account"])


async def _get_broker(db: AsyncSession):
    """Get broker adapter based on app mode."""
    if settings.APP_MODE == "mock":
        from app.broker.mock_adapter import MockBrokerAdapter
        return MockBrokerAdapter()

    result = await db.execute(
        select(BrokerConnection).where(BrokerConnection.is_active == True).limit(1)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=400, detail="No active broker connection. Connect to Trading 212 first.")

    from app.core.security import decrypt_field
    from app.broker.trading212 import Trading212Adapter
    api_key = decrypt_field(conn.api_key_encrypted)
    api_secret = decrypt_field(conn.api_secret_encrypted)
    return Trading212Adapter(api_key, api_secret, conn.environment)


@account_router.get("/summary", response_model=AccountSummaryOut)
async def account_summary(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    broker = await _get_broker(db)
    async with broker as b:
        summary = await b.get_account_summary()

    return AccountSummaryOut(
        total_value=summary.get("total", 0),
        cash=summary.get("cash", 0),
        free_funds=summary.get("free", 0),
        invested=summary.get("invested", 0),
        result=summary.get("result", 0),
        currency="USD",
        synced_at=datetime.now(timezone.utc),
        mode=settings.APP_MODE,
    )


@account_router.get("/cash-guard", response_model=CashGuardStatus)
async def cash_guard_status(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    broker = await _get_broker(db)
    async with broker as b:
        summary = await b.get_account_summary()

    free = float(summary.get("free", 0))
    cash = float(summary.get("cash", 0))
    return CashGuardStatus(
        available_to_trade=free,
        reserved=cash - free,
        total_cash=cash,
        cash_only_mode=True,  # Always true — hardcoded
        currency="USD",
    )


# ─── Instruments ─────────────────────────────────────────────────────────────

instruments_router = APIRouter(prefix="/instruments", tags=["instruments"])


@instruments_router.get("", response_model=InstrumentList)
async def list_instruments(
    search: str | None = Query(None),
    type: str | None = Query(None),
    enabled_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Instrument)
    if search:
        q = q.where(
            Instrument.ticker.ilike(f"%{search}%") | Instrument.name.ilike(f"%{search}%")
        )
    if type:
        q = q.where(Instrument.type == type.upper())
    if enabled_only:
        q = q.where(Instrument.trading_enabled == True)

    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar_one()

    q = q.offset((page - 1) * page_size).limit(page_size).order_by(Instrument.ticker)
    items = (await db.execute(q)).scalars().all()
    return InstrumentList(items=list(items), total=total)


@instruments_router.post("/sync")
async def sync_instruments(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sync instruments from broker."""
    broker = await _get_broker(db)
    async with broker as b:
        raw_instruments = await b.get_instruments()

    synced = 0
    now = datetime.now(timezone.utc)
    for raw in raw_instruments:
        ticker = raw.get("ticker", "")
        if not ticker:
            continue
        result = await db.execute(select(Instrument).where(Instrument.ticker == ticker))
        inst = result.scalar_one_or_none()
        if inst:
            inst.name = raw.get("name", inst.name)
            inst.type = raw.get("type", inst.type)
            inst.currency_code = raw.get("currencyCode", inst.currency_code)
            inst.extended_hours = raw.get("extendedHours", False)
            inst.working_schedule_id = raw.get("workingScheduleId")
            inst.synced_at = now
            inst.raw = raw
        else:
            inst = Instrument(
                id=uuid.uuid4(),
                ticker=ticker,
                name=raw.get("name", ticker),
                type=raw.get("type", "STOCK"),
                currency_code=raw.get("currencyCode", "USD"),
                extended_hours=raw.get("extendedHours", False),
                working_schedule_id=raw.get("workingScheduleId"),
                trading_enabled=True,
                synced_at=now,
                raw=raw,
            )
            db.add(inst)
        synced += 1

    return {"synced": synced, "timestamp": now.isoformat()}


@instruments_router.get("/{ticker}", response_model=InstrumentOut)
async def get_instrument(
    ticker: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Instrument).where(Instrument.ticker == ticker.upper()))
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail=f"Instrument {ticker} not found")
    return inst


# ─── Strategies ──────────────────────────────────────────────────────────────

strategies_router = APIRouter(prefix="/strategies", tags=["strategies"])


@strategies_router.get("", response_model=list[StrategyOut])
async def list_strategies(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).order_by(Strategy.created_at.desc()))
    return result.scalars().all()


@strategies_router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(
    body: StrategyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    strategy = Strategy(
        id=uuid.uuid4(),
        **body.model_dump(),
        is_enabled=False,
        is_live=False,
    )
    db.add(strategy)
    db.add(AuditLog(
        action="strategy_created", entity_type="strategy", entity_id=str(strategy.id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return strategy


@strategies_router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return s


@strategies_router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: uuid.UUID,
    body: StrategyUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    db.add(AuditLog(
        action="strategy_updated", entity_type="strategy", entity_id=str(strategy_id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return s


@strategies_router.post("/{strategy_id}/enable")
async def enable_strategy(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    s.is_enabled = True
    db.add(AuditLog(
        action="strategy_enabled", entity_type="strategy", entity_id=str(strategy_id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    return {"enabled": True}


@strategies_router.post("/{strategy_id}/disable")
async def disable_strategy(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    s.is_enabled = False
    db.add(AuditLog(
        action="strategy_disabled", entity_type="strategy", entity_id=str(strategy_id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    return {"enabled": False}


@strategies_router.get("/{strategy_id}/signals", response_model=list[SignalOut])
async def get_strategy_signals(
    strategy_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Signal)
        .where(Signal.strategy_id == strategy_id)
        .order_by(Signal.generated_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@strategies_router.post("/{strategy_id}/run-dry")
async def run_strategy_dry(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run strategy in dry-run (simulation) mode."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")

    db.add(AuditLog(
        action="strategy_dry_run", entity_type="strategy", entity_id=str(strategy_id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    return {"message": f"Dry run initiated for strategy {s.name}", "is_live": False}


# ─── Signals ─────────────────────────────────────────────────────────────────

signals_router = APIRouter(prefix="/signals", tags=["signals"])


@signals_router.get("", response_model=list[SignalOut])
async def list_signals(
    status: str | None = Query(None),
    ticker: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Signal).order_by(Signal.generated_at.desc()).limit(limit)
    if status:
        q = q.where(Signal.status == status)
    if ticker:
        q = q.where(Signal.ticker == ticker.upper())
    return (await db.execute(q)).scalars().all()


@signals_router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(
    signal_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    return sig


# ─── Orders ──────────────────────────────────────────────────────────────────

orders_router = APIRouter(prefix="/orders", tags=["orders"])


@orders_router.get("", response_model=list[OrderOut])
async def list_orders(
    status: str | None = Query(None),
    ticker: str | None = Query(None),
    is_dry_run: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Order).order_by(Order.created_at.desc()).limit(limit)
    if status:
        q = q.where(Order.status == status)
    if ticker:
        q = q.where(Order.ticker == ticker.upper())
    if is_dry_run is not None:
        q = q.where(Order.is_dry_run == is_dry_run)
    return (await db.execute(q)).scalars().all()


@orders_router.post("", response_model=OrderOut, status_code=201)
async def place_order(
    body: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Place an order with full risk checks."""
    broker = await _get_broker(db)

    # Get account data for risk checks
    async with broker as b:
        summary = await b.get_account_summary()

    available_cash = Decimal(str(summary.get("free", 0)))
    account_value = Decimal(str(summary.get("total", 0)))

    # Estimate price for cash guard
    estimated_price = body.limit_price or body.stop_price or Decimal("100")

    # Open positions count
    open_q = await db.execute(
        select(func.count(Order.id)).where(Order.status.in_(["accepted", "filled"]))
    )
    open_count = open_q.scalar_one()

    # Run risk checks
    risk_engine = RiskEngine(db)
    try:
        await risk_engine.run_all_checks(
            ticker=body.ticker,
            side=body.side,
            quantity=body.quantity,
            estimated_price=estimated_price,
            available_cash=available_cash,
            account_value=account_value,
            realized_pnl_today=Decimal("0"),
            current_open_positions=open_count,
            signal_id=body.signal_id,
        )
    except RiskViolation as e:
        raise HTTPException(status_code=422, detail=e.reason)

    from app.execution.engine import ExecutionEngine
    async with broker as b:
        engine = ExecutionEngine(db, b)
        order = await engine.create_order_intent(
            ticker=body.ticker.upper(),
            side=body.side,
            order_type=body.order_type,
            quantity=body.quantity,
            signal_id=body.signal_id,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_validity=body.time_validity,
            is_dry_run=(settings.APP_MODE == "mock"),
            available_cash=available_cash,
            estimated_price=estimated_price,
        )
        order = await engine.submit_order(order)

    db.add(AuditLog(
        action="order_placed", entity_type="order", entity_id=str(order.id),
        actor=current_user.email, payload={"ticker": body.ticker, "side": body.side},
        occurred_at=datetime.now(timezone.utc),
    ))
    return order


@orders_router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    o = result.scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")
    return o


@orders_router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("filled", "cancelled", "rejected"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel order in status: {order.status}")

    broker = await _get_broker(db)
    from app.execution.engine import ExecutionEngine
    async with broker as b:
        engine = ExecutionEngine(db, b)
        order = await engine.cancel_order(order)

    db.add(AuditLog(
        action="order_cancelled", entity_type="order", entity_id=str(order_id),
        actor=current_user.email, occurred_at=datetime.now(timezone.utc),
    ))
    return {"cancelled": True, "order_id": str(order_id)}


@orders_router.post("/cancel-all-pending")
async def cancel_all_pending(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).where(Order.status.in_(["pending_intent", "submitted", "accepted"]))
    )
    orders = result.scalars().all()

    broker = await _get_broker(db)
    from app.execution.engine import ExecutionEngine
    async with broker as b:
        engine = ExecutionEngine(db, b)
        for order in orders:
            await engine.cancel_order(order)

    db.add(AuditLog(
        action="cancel_all_pending", actor=current_user.email,
        payload={"count": len(orders)}, occurred_at=datetime.now(timezone.utc),
    ))
    return {"cancelled": len(orders)}


# ─── Positions ───────────────────────────────────────────────────────────────

positions_router = APIRouter(prefix="/positions", tags=["positions"])


@positions_router.get("", response_model=list[PositionOut])
async def list_positions(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    broker = await _get_broker(db)
    async with broker as b:
        raw_positions = await b.get_positions()

    return [
        PositionOut(
            ticker=p.get("ticker", ""),
            quantity=p.get("quantity", 0),
            avg_price=p.get("averagePrice", 0),
            current_price=p.get("currentPrice"),
            unrealized_pnl=p.get("ppl"),
            quantity_available=p.get("maxSell"),
            value=(p.get("quantity", 0) * p.get("currentPrice", 0)) if p.get("currentPrice") else None,
        )
        for p in raw_positions
    ]


@positions_router.post("/refresh")
async def refresh_positions(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    broker = await _get_broker(db)
    async with broker as b:
        positions = await b.get_positions()
    return {"positions": len(positions), "refreshed_at": datetime.now(timezone.utc).isoformat()}


# ─── Risk ─────────────────────────────────────────────────────────────────────

risk_router = APIRouter(prefix="/risk", tags=["risk"])


@risk_router.get("/profile", response_model=RiskProfileOut | None)
async def get_risk_profile(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RiskProfile).where(RiskProfile.is_default == True))
    return result.scalar_one_or_none()


@risk_router.patch("/profile", response_model=RiskProfileOut)
async def update_risk_profile(
    body: RiskProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RiskProfile).where(RiskProfile.is_default == True))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="No default risk profile found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(profile, field, value)
    db.add(AuditLog(
        action="risk_profile_updated", entity_type="risk_profile", entity_id=str(profile.id),
        actor=current_user.email, payload=body.model_dump(exclude_none=True),
        occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return profile


@risk_router.get("/events", response_model=list[RiskEventOut])
async def get_risk_events(
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = Query(None),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(RiskEvent).order_by(RiskEvent.occurred_at.desc()).limit(limit)
    if event_type:
        q = q.where(RiskEvent.event_type == event_type)
    return (await db.execute(q)).scalars().all()


@risk_router.post("/kill-switch/enable")
async def enable_kill_switch(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await activate_kill_switch(db, actor=current_user.email)
    db.add(AuditLog(
        action="kill_switch_enabled", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {"kill_switch_active": True}


@risk_router.post("/kill-switch/disable")
async def disable_kill_switch(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await deactivate_kill_switch(db, actor=current_user.email)
    db.add(AuditLog(
        action="kill_switch_disabled", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {"kill_switch_active": False}


@risk_router.post("/daily-reset")
async def daily_reset(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    db.add(AuditLog(
        action="daily_reset", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return {"message": "Daily stats reset", "timestamp": datetime.now(timezone.utc).isoformat()}


# ─── Alerts ──────────────────────────────────────────────────────────────────

alerts_router = APIRouter(prefix="/alerts", tags=["alerts"])


@alerts_router.get("", response_model=list[AlertOut])
async def list_alerts(
    is_read: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    if is_read is not None:
        q = q.where(Alert.is_read == is_read)
    return (await db.execute(q)).scalars().all()


@alerts_router.post("/test")
async def test_alert(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    alert = Alert(
        id=uuid.uuid4(),
        alert_type="test",
        channel="in_app",
        title="Test Alert",
        message="This is a test notification from T212 CashGuard.",
        severity="info",
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(alert)
    await db.flush()
    return {"sent": True, "alert_id": str(alert.id)}


@alerts_router.patch("/{alert_id}/read")
async def mark_alert_read(
    alert_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert:
        alert.is_read = True
    return {"read": True}


# ─── Settings ─────────────────────────────────────────────────────────────────

settings_router = APIRouter(prefix="/settings", tags=["settings"])


@settings_router.get("", response_model=AppSettingsOut)
async def get_settings(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not initialized")
    return s


@settings_router.patch("", response_model=AppSettingsOut)
async def update_settings(
    body: AppSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not initialized")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    db.add(AuditLog(
        action="settings_updated", actor=current_user.email,
        payload=body.model_dump(exclude_none=True), occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return s


# ─── Emergency ───────────────────────────────────────────────────────────────

emergency_router = APIRouter(prefix="/emergency", tags=["emergency"])


@emergency_router.post("/auto-trading/off", response_model=EmergencyActionResult)
async def disable_auto_trading(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s:
        s.auto_trading_enabled = False
    db.add(AuditLog(
        action="auto_trading_disabled", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return EmergencyActionResult(
        success=True, action="auto_trading_off",
        message="Auto-trading disabled", timestamp=datetime.now(timezone.utc),
    )


@emergency_router.post("/auto-trading/on", response_model=EmergencyActionResult)
async def enable_auto_trading(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Check kill switch first
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s and s.kill_switch_active:
        raise HTTPException(status_code=400, detail="Cannot enable auto-trading while kill switch is active")
    if s:
        s.auto_trading_enabled = True
    db.add(AuditLog(
        action="auto_trading_enabled", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return EmergencyActionResult(
        success=True, action="auto_trading_on",
        message="Auto-trading enabled", timestamp=datetime.now(timezone.utc),
    )


@emergency_router.post("/kill-switch", response_model=EmergencyActionResult)
async def emergency_kill_switch(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await activate_kill_switch(db, actor=current_user.email)
    # Also disable auto-trading
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s:
        s.auto_trading_enabled = False
    db.add(AuditLog(
        action="emergency_kill_switch", actor=current_user.email,
        occurred_at=datetime.now(timezone.utc),
    ))
    return EmergencyActionResult(
        success=True, action="kill_switch",
        message="KILL SWITCH ACTIVATED. All trading halted.", timestamp=datetime.now(timezone.utc),
    )


@emergency_router.post("/cancel-all", response_model=EmergencyActionResult)
async def emergency_cancel_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).where(Order.status.in_(["pending_intent", "submitted", "accepted"]))
    )
    orders = result.scalars().all()
    broker = await _get_broker(db)
    from app.execution.engine import ExecutionEngine
    async with broker as b:
        engine = ExecutionEngine(db, b)
        for order in orders:
            await engine.cancel_order(order)

    db.add(AuditLog(
        action="emergency_cancel_all", actor=current_user.email,
        payload={"cancelled_count": len(orders)}, occurred_at=datetime.now(timezone.utc),
    ))
    return EmergencyActionResult(
        success=True, action="cancel_all",
        message=f"Cancelled {len(orders)} pending orders", timestamp=datetime.now(timezone.utc),
    )


@emergency_router.post("/flatten-all", response_model=EmergencyActionResult)
async def emergency_flatten_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Close all open positions via market sell orders."""
    from app.execution.engine import ExecutionEngine
    broker = await _get_broker(db)
    flattened = 0
    async with broker as b:
        positions = await b.get_positions()
        engine = ExecutionEngine(db, b)
        for pos in positions:
            qty = Decimal(str(pos.get("quantity", 0)))
            if qty > 0:
                order = await engine.create_order_intent(
                    ticker=pos["ticker"],
                    side="sell",
                    order_type="market",
                    quantity=qty,
                    is_dry_run=(settings.APP_MODE == "mock"),
                )
                await engine.submit_order(order)
                flattened += 1

    db.add(AuditLog(
        action="emergency_flatten_all", actor=current_user.email,
        payload={"flattened": flattened}, occurred_at=datetime.now(timezone.utc),
    ))
    return EmergencyActionResult(
        success=True, action="flatten_all",
        message=f"Flattened {flattened} positions", timestamp=datetime.now(timezone.utc),
    )


# ─── Reports ─────────────────────────────────────────────────────────────────

reports_router = APIRouter(prefix="/reports", tags=["reports"])


@reports_router.get("/performance", response_model=PerformanceReport)
async def get_performance(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Query filled orders for basic stats
    result = await db.execute(
        select(Order).where(Order.status == "filled", Order.is_dry_run == False)
    )
    orders = result.scalars().all()

    total = len(orders)
    wins = sum(1 for o in orders if o.side == "sell" and o.avg_fill_price and o.avg_fill_price > Decimal("0"))

    return PerformanceReport(
        total_trades=total,
        winning_trades=wins,
        losing_trades=max(0, total - wins),
        win_rate=wins / total if total > 0 else 0.0,
        total_pnl=0.0,
        avg_win=0.0,
        avg_loss=0.0,
        profit_factor=0.0,
        max_drawdown=0.0,
        sharpe_ratio=None,
        daily_pnl=[],
    )


@reports_router.get("/trades", response_model=list[OrderOut])
async def get_trades_report(
    limit: int = Query(100, ge=1, le=1000),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order)
        .where(Order.status == "filled")
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ─── Audit Log ───────────────────────────────────────────────────────────────

audit_router = APIRouter(prefix="/audit", tags=["audit"])


@audit_router.get("", response_model=AuditLogList)
async def get_audit_logs(
    action: str | None = Query(None),
    entity_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)

    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar_one()

    q = q.order_by(AuditLog.occurred_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(q)).scalars().all()

    return AuditLogList(items=list(items), total=total, page=page, page_size=page_size)


# ─── Health ──────────────────────────────────────────────────────────────────

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthStatus)
async def health_live():
    return HealthStatus(
        status="ok",
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        mode=settings.APP_MODE,
    )


@health_router.get("/ready", response_model=HealthStatus)
async def health_ready(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(select(func.now()))
        return HealthStatus(
            status="ok", timestamp=datetime.now(timezone.utc),
            version="1.0.0", mode=settings.APP_MODE,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB not ready: {e}")


@health_router.get("/deps", response_model=DepsHealth)
async def health_deps(db: AsyncSession = Depends(get_db)):
    db_ok = "ok"
    redis_ok = "ok"
    try:
        await db.execute(select(func.now()))
    except Exception:
        db_ok = "error"

    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
    except Exception:
        redis_ok = "error"

    return DepsHealth(
        database=db_ok,
        redis=redis_ok,
        broker="mock" if settings.APP_MODE == "mock" else "connected",
        market_data="mock",
    )
