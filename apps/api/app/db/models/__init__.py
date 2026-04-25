"""
Full database schema for T212 CashGuard Trader.
All tables with proper constraints, indexes, and JSONB fields.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.serialization import to_jsonable
from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


JSONType = JSON().with_variant(JSONB, "postgresql")


# ──────────────────────────────────────────────────────────────────────────────
# Users & Sessions
# ──────────────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sessions: Mapped[list[Session]] = relationship("Session", back_populates="user")
    audit_logs: Mapped[list[AuditLog]] = relationship("AuditLog", back_populates="user")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship("User", back_populates="sessions")


# ──────────────────────────────────────────────────────────────────────────────
# Broker Connections
# ──────────────────────────────────────────────────────────────────────────────

class BrokerConnection(Base):
    __tablename__ = "broker_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    broker: Mapped[str] = mapped_column(String(50), nullable=False, default="trading212")
    environment: Mapped[str] = mapped_column(String(10), nullable=False)  # demo | live
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account_id: Mapped[str | None] = mapped_column(String(100))
    account_currency: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "broker", "environment", name="uq_broker_user_env"),
    )


class BrokerAccountSnapshot(Base):
    __tablename__ = "broker_accounts_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("broker_connections.id", ondelete="CASCADE"), nullable=False)
    snapshotted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    total_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    free_funds: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    invested: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    result: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    __table_args__ = (
        Index("ix_broker_snapshots_connection_time", "connection_id", "snapshotted_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Instruments
# ──────────────────────────────────────────────────────────────────────────────

class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # STOCK, ETF, etc.
    currency_code: Mapped[str] = mapped_column(String(10), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(20))
    extended_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    working_schedule_id: Mapped[int | None] = mapped_column(Integer)
    min_trade_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    max_open_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    buy_lot_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    sell_lot_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ExchangeSchedule(Base):
    __tablename__ = "exchange_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    open_time: Mapped[str] = mapped_column(String(10), nullable=False)  # HH:MM
    close_time: Mapped[str] = mapped_column(String(10), nullable=False)  # HH:MM
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────────────────────────
# Risk & Strategies
# ──────────────────────────────────────────────────────────────────────────────

class RiskProfile(Base):
    """
    Risk rules applied per strategy.

    Equity fields control standard cash-equity trading.
    CFD fields (cfd_*) override equity limits when the strategy operates
    CFDs (higher leverage, overnight funding, margin calls).
    When a cfd_* field is NULL it inherits the equivalent equity value.

    Scientific basis:
      Equity sizing  — Kelly (1956); Thorp (2006)
      CFD leverage   — FCA ESMA product intervention (2018): max 30:1 majors, 20:1 indices
      Overnight cost — Chan (2013): funding cost erodes mean-reversion edge overnight
      Heat cap       — Markowitz (1952); Lopez de Prado (2018)
    """
    __tablename__ = "risk_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # ── Equity / universal limits ────────────────────────────────────────────
    max_risk_per_trade_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("1.0"))
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("3.0"))
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_position_size_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("10.0"))
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    stop_after_consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    symbol_cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    force_flat_eod: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── CFD-specific limits (NULL = inherit equity equivalent) ───────────────
    # Tighter per-trade risk because CFD leverage amplifies losses quickly
    cfd_max_risk_per_trade_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="CFD per-trade risk cap (default: 0.5% — half of equity limit)",
    )
    cfd_max_daily_loss_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="CFD intraday loss limit (default: 2% — tighter than equity)",
    )
    # Maximum notional CFD exposure held overnight as % of account equity
    max_overnight_cfd_exposure_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0.0"),
        comment="0 = force-flat all CFDs before close; >0 allows carry with margin check",
    )
    # Minimum free margin % before new CFD positions are blocked
    min_free_margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("30.0"),
        comment="Block new CFD entries if free_margin / equity < this threshold",
    )
    # Maximum leverage multiplier (notional / equity) allowed per CFD position
    cfd_max_leverage: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("5.0"),
        comment="Hard cap on leverage (5x = conservative retail; FCA max 20-30x)",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # orb, vwap_reclaim, mean_reversion, momentum
    description: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)  # live vs dry-run
    risk_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("risk_profiles.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    allowed_tickers: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    session_start: Mapped[str] = mapped_column(String(10), default="09:30")  # HH:MM local exchange
    session_end: Mapped[str] = mapped_column(String(10), default="16:00")
    extended_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    eod_flatten: Mapped[bool] = mapped_column(Boolean, default=True)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    risk_profile: Mapped[RiskProfile | None] = relationship("RiskProfile")
    signals: Mapped[list[Signal]] = relationship("Signal", back_populates="strategy")
    runs: Mapped[list[StrategyRun]] = relationship("StrategyRun", back_populates="strategy")


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False)  # dry | live
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")  # running | completed | error
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    orders_placed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="runs")


# ──────────────────────────────────────────────────────────────────────────────
# Signals
# ──────────────────────────────────────────────────────────────────────────────

class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # buy | sell
    signal_type: Mapped[str] = mapped_column(String(50), nullable=False)  # entry | exit | stop | take_profit
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending | approved | rejected | executed | expired
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    suggested_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    reason: Mapped[str | None] = mapped_column(Text)
    risk_rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_rejection_reason: Mapped[str | None] = mapped_column(Text)
    params_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="signals")
    orders: Mapped[list[Order]] = relationship("Order", back_populates="signal")

    @property
    def strategy_name(self) -> str | None:
        return self.strategy.name if self.strategy else None

    @property
    def strategy_type_name(self) -> str | None:
        return self.strategy.type if self.strategy else None

    __table_args__ = (
        Index("ix_signals_strategy_ticker", "strategy_id", "ticker"),
        Index("ix_signals_status", "status"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Orders
# ──────────────────────────────────────────────────────────────────────────────

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("signals.id"))
    client_order_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # buy | sell
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # market | limit | stop | stop_limit
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    # For T212: sell uses negative quantity — stored positive, negated on submission
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    time_validity: Mapped[str] = mapped_column(String(10), default="DAY")  # DAY | GTC
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_intent")
    # Lifecycle: pending_intent → submitted → accepted → filled | cancelled | rejected | error
    broker_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    filled_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    execution_environment: Mapped[str | None] = mapped_column(String(20))
    expected_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    slippage_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    slippage_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_latency_ms: Mapped[int | None] = mapped_column(Integer)
    fill_latency_ms: Mapped[int | None] = mapped_column(Integer)
    reconciliation_latency_ms: Mapped[int | None] = mapped_column(Integer)
    execution_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    execution_quality_grade: Mapped[str | None] = mapped_column(String(20))
    execution_quality_notes: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    cash_used: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    available_cash_at_submission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    broker_request: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    broker_response: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    signal: Mapped[Signal | None] = relationship("Signal", back_populates="orders")
    events: Mapped[list[OrderEvent]] = relationship("OrderEvent", back_populates="order")

    @property
    def strategy_id(self) -> uuid.UUID | None:
        return self.signal.strategy_id if self.signal else None

    @property
    def strategy_name(self) -> str | None:
        if self.signal and self.signal.strategy:
            return self.signal.strategy.name
        return None

    @property
    def strategy_type_name(self) -> str | None:
        if self.signal and self.signal.strategy:
            return self.signal.strategy.type
        return None

    @property
    def signal_reason(self) -> str | None:
        return self.signal.reason if self.signal else None

    @property
    def signal_confidence(self) -> Decimal | None:
        return self.signal.confidence if self.signal else None

    @property
    def signal_risk_rejected(self) -> bool | None:
        return self.signal.risk_rejected if self.signal else None

    @property
    def signal_risk_rejection_reason(self) -> str | None:
        return self.signal.risk_rejection_reason if self.signal else None

    @property
    def signal_snapshot(self) -> Signal | None:
        return self.signal

    __table_args__ = (
        Index("ix_orders_ticker_status", "ticker", "status"),
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_execution_quality", "execution_environment", "ticker", "order_type", "created_at"),
    )


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str | None] = mapped_column(String(30))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship("Order", back_populates="events")


# ──────────────────────────────────────────────────────────────────────────────
# Positions
# ──────────────────────────────────────────────────────────────────────────────

class PositionSnapshot(Base):
    __tablename__ = "positions_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("broker_connections.id", ondelete="CASCADE"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    quantity_available: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    snapshotted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_positions_connection_ticker", "connection_id", "ticker"),
        Index("ix_positions_snapshotted_at", "snapshotted_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Trades (closed positions)
# ──────────────────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # buy | sell
    open_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"))
    close_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Trade Journal fields (migration 0005) ────────────────────────────────
    journal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal_tags: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True, default=list)
    journal_emotion: Mapped[str | None] = mapped_column(String(50), nullable=True)
    journal_rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    journal_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_trades_ticker_opened", "ticker", "opened_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# CFD Funding Costs
# ──────────────────────────────────────────────────────────────────────────────

class CFDFundingCost(Base):
    """
    Daily financing charge on a CFD position held overnight.

    Formula: daily_charge = notional x (annual_rate_pct / 100) / 360
    where notional = quantity x price_at_close.

    Scientific basis (Chan 2013):
      Overnight funding cost is the dominant drag on intraday mean-reversion
      strategies when positions are carried.  Explicit tracking allows the
      walk-forward backtester to deduct realistic carrying costs.
    """
    __tablename__ = "cfd_funding_costs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price_at_close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    annual_rate_pct: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    daily_charge: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_cfd_funding_ticker_date", "ticker", "recorded_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Risk Events
# ──────────────────────────────────────────────────────────────────────────────

class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Types: kill_switch_on | kill_switch_off | daily_loss_breach | consecutive_loss |
    #        cash_guard_block | duplicate_order_block | stale_data | cooldown_block | eod_flatten
    ticker: Mapped[str | None] = mapped_column(String(50))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ──────────────────────────────────────────────────────────────────────────────
# Alerts
# ──────────────────────────────────────────────────────────────────────────────

class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # in_app | email | telegram
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")  # info | warning | error | critical
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ──────────────────────────────────────────────────────────────────────────────
# Telegram Control
# ──────────────────────────────────────────────────────────────────────────────

class TelegramControlRequest(Base):
    __tablename__ = "telegram_control_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confirmation_code: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_telegram_control_status_expiry", "status", "expires_at"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# App Settings
# ──────────────────────────────────────────────────────────────────────────────

class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    theme: Mapped[str] = mapped_column(String(20), default="dark")
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    market_data_provider: Mapped[str] = mapped_column(String(50), default="mock")
    auto_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)
    live_trading_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_stats_reset_time: Mapped[str] = mapped_column(String(10), default="00:00")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ──────────────────────────────────────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    ip_address: Mapped[str | None] = mapped_column(String(45))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    user: Mapped[User | None] = relationship("User", back_populates="audit_logs")

    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    @validates("payload")
    def _normalize_payload(self, _key: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        return to_jsonable(payload)
