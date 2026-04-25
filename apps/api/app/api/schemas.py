"""
Pydantic v2 schemas for all API request/response models.
"""
from __future__ import annotations

import uuid  # noqa: TC003
from datetime import datetime  # noqa: TC003
from decimal import Decimal  # noqa: TC003
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── Base ───────────────────────────────────────────────────────────────────

class BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ─── Auth ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def normalize_login_identifier(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("Enter a valid login email or identifier")
        return normalized


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    email: str
    is_admin: bool


class UserOut(BaseSchema):
    id: uuid.UUID
    email: str
    is_active: bool
    is_admin: bool
    created_at: datetime


# ─── Broker ──────────────────────────────────────────────────────────────────

class BrokerConnectRequest(BaseModel):
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    environment: Literal["demo", "live"] = "demo"

    @field_validator("api_key", "api_secret")
    @classmethod
    def strip_broker_credentials(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Credential value cannot be blank")
        return normalized


class BrokerDiagnosticCause(BaseModel):
    key: Literal["wrong_environment", "invalid_credentials", "ip_restriction"]
    label: str
    likelihood: Literal["likely", "possible"]
    detail: str


class BrokerDiagnostics(BaseModel):
    code: Literal["broker_auth_rejected"]
    title: str
    summary: str
    environment: Literal["demo", "live"]
    broker_host: str
    http_status: int
    causes: list[BrokerDiagnosticCause]
    note: str


class BrokerStatusOut(BaseSchema):
    id: uuid.UUID
    broker: str
    environment: str
    is_active: bool
    credential_state: Literal["mock", "configured", "reconnect_required", "not_connected"]
    recovery_hint: str | None = None
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_sync_at: datetime | None
    account_id: str | None
    account_currency: str | None
    created_at: datetime


class BrokerTestResult(BaseModel):
    is_ok: bool
    account_id: str | None
    currency: str | None
    error: str | None
    diagnostics: BrokerDiagnostics | None = None


# ─── Account ─────────────────────────────────────────────────────────────────

class AccountSummaryOut(BaseModel):
    total_value: float
    cash: float
    free_funds: float
    invested: float
    result: float
    currency: str
    synced_at: datetime | None
    mode: str


class CashGuardStatus(BaseModel):
    available_to_trade: float
    reserved: float
    total_cash: float
    cash_only_mode: bool
    currency: str


# ─── Instruments ─────────────────────────────────────────────────────────────

class InstrumentOut(BaseSchema):
    id: uuid.UUID
    ticker: str
    name: str
    type: str
    currency_code: str
    isin: str | None
    extended_hours: bool
    working_schedule_id: int | None
    trading_enabled: bool
    synced_at: datetime | None


class InstrumentList(BaseModel):
    items: list[InstrumentOut]
    total: int


# ─── Risk Profiles ───────────────────────────────────────────────────────────

class RiskProfileOut(BaseSchema):
    id: uuid.UUID
    name: str
    max_risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    max_open_positions: int
    max_position_size_pct: Decimal
    max_trades_per_day: int
    stop_after_consecutive_losses: int
    symbol_cooldown_seconds: int
    force_flat_eod: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime
    # CFD-specific risk fields (added in migration 0003)
    cfd_max_risk_per_trade_pct: Decimal | None = None
    cfd_max_daily_loss_pct: Decimal | None = None
    max_overnight_cfd_exposure_pct: Decimal | None = None
    min_free_margin_pct: Decimal | None = None
    cfd_max_leverage: Decimal | None = None


class RiskProfileUpdate(BaseModel):
    name: str | None = None
    max_risk_per_trade_pct: Decimal | None = Field(None, ge=0.1, le=10.0)
    max_daily_loss_pct: Decimal | None = Field(None, ge=0.1, le=20.0)
    max_open_positions: int | None = Field(None, ge=1, le=50)
    max_position_size_pct: Decimal | None = Field(None, ge=0.5, le=100.0)
    max_trades_per_day: int | None = Field(None, ge=1, le=200)
    stop_after_consecutive_losses: int | None = Field(None, ge=0, le=20)
    symbol_cooldown_seconds: int | None = Field(None, ge=0, le=86400)
    force_flat_eod: bool | None = None


# ─── Strategies ──────────────────────────────────────────────────────────────

StrategyType = Literal[
    "orb",
    "opening_fade",
    "vwap_reclaim",
    "closing_momentum",
    "intraday_periodicity",
    "mean_reversion",
    "momentum",
    "buy_hold_core",
    "equal_weight_rebalance",
    "cross_sectional_momentum",
    "low_volatility_tilt",
    "trend_following_tactical",
]

StrategyPresetKey = Literal[
    "orb",
    "opening_fade",
    "vwap_reclaim",
    "closing_momentum",
    "intraday_periodicity",
]


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: StrategyType
    description: str | None = None
    risk_profile_id: uuid.UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    allowed_tickers: list[str] = Field(default_factory=list)
    session_start: str = "09:30"
    session_end: str = "16:00"
    extended_hours: bool = False
    eod_flatten: bool = True


class StrategyPresetCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    allowed_tickers: list[str] | None = None


class StrategyPresetInfo(BaseModel):
    key: StrategyPresetKey
    label: str
    strategy_type: StrategyType
    description: str
    style: str
    session_window: str
    default_tickers: list[str]
    default_params: dict[str, Any]
    risk_template_name: str
    risk_summary: str


class StrategyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    risk_profile_id: uuid.UUID | None = None
    params: dict[str, Any] | None = None
    allowed_tickers: list[str] | None = None
    session_start: str | None = None
    session_end: str | None = None
    extended_hours: bool | None = None
    eod_flatten: bool | None = None
    is_live: bool | None = None


class StrategyOut(BaseSchema):
    id: uuid.UUID
    name: str
    type: str
    description: str | None
    is_enabled: bool
    is_live: bool
    risk_profile_id: uuid.UUID | None
    params: dict[str, Any]
    allowed_tickers: list[str]
    session_start: str
    session_end: str
    extended_hours: bool
    eod_flatten: bool
    last_signal_at: datetime | None
    risk_profile: RiskProfileOut | None = None
    created_at: datetime
    updated_at: datetime


class StrategyPromotionCheck(BaseModel):
    phase: Literal["demo", "live"]
    key: str
    label: str
    status: Literal["pass", "fail"]
    detail: str
    verified_at: datetime | None = None


class StrategyPromotionMetrics(BaseModel):
    dry_run_signal_count: int
    dry_run_order_count: int
    dry_run_days: int
    dry_run_reviewed_at: str | None = None
    demo_order_count: int
    demo_filled_count: int
    demo_rejected_count: int
    demo_error_count: int
    demo_cancelled_count: int
    demo_days: int
    demo_fill_rate: float
    demo_error_rate: float
    demo_signal_count: int
    demo_risk_block_count: int
    demo_risk_block_rate: float
    demo_promoted_at: str | None = None
    demo_reviewed_at: str | None = None
    live_approved_at: str | None = None


class StrategyPromotionStatus(BaseModel):
    strategy_id: uuid.UUID
    strategy_name: str
    current_stage: Literal["dry_run", "demo", "live_approved"]
    broker_execution_enabled: bool
    demo_execution_enabled: bool
    live_execution_approved: bool
    eligible_for_demo: bool
    eligible_for_live: bool
    recommended_next_action: Literal[
        "record_dry_run_review",
        "promote_to_demo",
        "record_demo_review",
        "promote_to_live",
        "demote_to_dry_run",
        "revoke_live_promotion",
    ] | None = None
    blockers: list[str]
    checks: list[StrategyPromotionCheck]
    metrics: StrategyPromotionMetrics


class StrategyPromotionActionRequest(BaseModel):
    action: Literal[
        "record_dry_run_review",
        "promote_to_demo",
        "record_demo_review",
        "promote_to_live",
        "demote_to_dry_run",
        "revoke_live_promotion",
    ]
    notes: str | None = Field(default=None, max_length=500)


class MarketRegimeOut(BaseModel):
    regime: str
    label: str
    color: str
    adx: float
    vol_percentile: float
    confidence: float
    breadth_pct: float | None = None
    primary_trend: str | None = None
    active_strategies: list[str] = Field(default_factory=list)
    suppressed_strategies: list[str] = Field(default_factory=list)
    detail: str | None = None


class WatchlistNewsItemOut(BaseModel):
    id: str
    source: str
    title: str
    summary: str
    url: str | None = None
    published_at: str | None = None
    tickers: list[str] = Field(default_factory=list)
    event_type: str
    sentiment_score: float
    urgency_score: float
    credibility_score: float
    impact_horizon: str
    catalyst_score: float


class WatchlistCandidateContextOut(BaseModel):
    ticker: str
    score: float = 0.0
    reason: str | None = None
    strategy_type: str | None = None
    pre_market_rvol: float | None = None
    gap_pct: float | None = None
    catalyst_score: float | None = None
    catalyst_event_type: str | None = None
    catalyst_summary: str | None = None
    catalyst_source: str | None = None
    feed_status: str = "unknown"
    blocked_reason: str | None = None
    trade_safe: bool = True


class AllocatorDecisionOut(BaseModel):
    ticker: str
    side: str
    strategy_id: str
    strategy_name: str
    strategy_type: str
    signal_type: str
    status: Literal["allocated", "rejected"]
    score: float
    threshold: float
    reason: str
    rank: int | None = None
    components: dict[str, float] = Field(default_factory=dict)
    penalties: dict[str, float] = Field(default_factory=dict)
    allocated_risk_pct: float = 0.0
    projected_gross_exposure_pct: float = 0.0
    projected_symbol_exposure_pct: float = 0.0
    regime_cap_pct: float = 0.0
    generated_at: datetime | None = None


class WatchlistIntelligenceOut(BaseModel):
    watchlist: list[str]
    news: list[WatchlistNewsItemOut] = Field(default_factory=list)
    count: int


class StrategyIntelligenceOut(BaseModel):
    strategy_id: uuid.UUID
    strategy_name: str
    strategy_type: str
    regime: MarketRegimeOut
    feed_health: MarketDataHealth
    watchlist: list[WatchlistCandidateContextOut] = Field(default_factory=list)
    recent_risk_blocks: list[RiskEventOut] = Field(default_factory=list)
    recent_allocation_decisions: list[AllocatorDecisionOut] = Field(default_factory=list)


class PortfolioWeightSnapshot(BaseModel):
    ticker: str
    target_weight: float | None = None
    current_weight: float | None = None
    delta_weight: float | None = None


class PortfolioRebalanceOrderOut(BaseModel):
    order_id: uuid.UUID
    signal_id: uuid.UUID | None
    ticker: str
    side: str
    status: str
    quantity: float
    avg_fill_price: float | None = None
    target_weight: float | None = None
    allocation_status: Literal["allocated", "rejected"] | None = None
    allocation_score: float | None = None
    allocation_reason: str | None = None
    is_dry_run: bool
    created_at: datetime


class PortfolioStrategyMonitoringOut(BaseModel):
    strategy_id: uuid.UUID
    strategy_name: str
    strategy_type: str
    is_enabled: bool
    is_live: bool
    last_status: str | None = None
    last_reason: str | None = None
    last_run_at: datetime | None = None
    last_rebalance_at: datetime | None = None
    last_mode: str | None = None
    last_orders_submitted: int = 0
    last_dry_run_orders: int = 0
    last_risk_blocks: int = 0
    last_allocation_blocks: int = 0
    last_allocation_decisions: list[AllocatorDecisionOut] = Field(default_factory=list)
    weights: list[PortfolioWeightSnapshot] = Field(default_factory=list)
    recent_orders: list[PortfolioRebalanceOrderOut] = Field(default_factory=list)


class PortfolioTimelinePointOut(BaseModel):
    date: str
    equity_pnl: float
    benchmark_pnl: float = 0.0
    realized_pnl: float
    unrealized_pnl: float
    cash_balance: float
    gross_exposure: float
    drawdown_pct: float = 0.0
    benchmark_drawdown_pct: float = 0.0
    turnover_notional: float
    order_count: int


class PortfolioTickerAttributionOut(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    weight_pct: float


class PortfolioRebalanceWeightChangeOut(BaseModel):
    ticker: str
    target_weight: float | None = None
    before_weight: float | None = None
    after_weight: float | None = None
    before_gap: float | None = None
    after_gap: float | None = None


class PortfolioRebalanceEventOut(BaseModel):
    date: str
    order_count: int
    turnover_notional: float
    total_pnl_after: float
    weights: list[PortfolioRebalanceWeightChangeOut] = Field(default_factory=list)


class PortfolioStrategyAttributionSummaryOut(BaseModel):
    strategy_id: uuid.UUID
    strategy_name: str
    strategy_type: str
    computed_at: datetime
    benchmark_name: str
    total_return_pct: float
    benchmark_return_pct: float
    alpha_vs_benchmark_pct: float
    max_drawdown_pct: float
    benchmark_max_drawdown_pct: float
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    cash_balance: float
    current_market_value: float
    turnover_notional: float
    buys_notional: float
    sells_notional: float
    rebalance_days: int
    order_count: int
    recent_timeline: list[PortfolioTimelinePointOut] = Field(default_factory=list)


class PortfolioStrategyAttributionOut(PortfolioStrategyAttributionSummaryOut):
    timeline: list[PortfolioTimelinePointOut] = Field(default_factory=list)
    ticker_attribution: list[PortfolioTickerAttributionOut] = Field(default_factory=list)
    rebalance_events: list[PortfolioRebalanceEventOut] = Field(default_factory=list)


# ─── Signals ─────────────────────────────────────────────────────────────────

class SignalOut(BaseSchema):
    id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str | None = None
    strategy_type_name: str | None = None
    ticker: str
    side: str
    signal_type: str
    status: str
    entry_price: Decimal | None
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    suggested_quantity: Decimal | None
    confidence: Decimal | None
    reason: str | None
    risk_rejected: bool
    risk_rejection_reason: str | None
    params_snapshot: dict[str, Any] | None = None
    generated_at: datetime
    expires_at: datetime | None
    executed_at: datetime | None


# ─── Orders ──────────────────────────────────────────────────────────────────

class OrderCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=50)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(None, gt=0)
    stop_price: Decimal | None = Field(None, gt=0)
    time_validity: Literal["DAY", "GTC"] = "DAY"
    signal_id: uuid.UUID | None = None

    @field_validator("limit_price")
    @classmethod
    def validate_limit_price(cls, v, info):
        if info.data.get("order_type") in ("limit", "stop_limit") and v is None:
            raise ValueError("limit_price is required for limit and stop_limit orders")
        return v

    @field_validator("stop_price")
    @classmethod
    def validate_stop_price(cls, v, info):
        if info.data.get("order_type") in ("stop", "stop_limit") and v is None:
            raise ValueError("stop_price is required for stop and stop_limit orders")
        return v


class OrderOut(BaseSchema):
    id: uuid.UUID
    signal_id: uuid.UUID | None
    strategy_id: uuid.UUID | None = None
    strategy_name: str | None = None
    strategy_type_name: str | None = None
    client_order_key: str
    ticker: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_validity: str
    status: str
    broker_order_id: str | None
    filled_quantity: Decimal | None
    avg_fill_price: Decimal | None
    execution_environment: str | None = None
    expected_fill_price: Decimal | None = None
    slippage_pct: Decimal | None = None
    slippage_value: Decimal | None = None
    submitted_at: datetime | None = None
    first_ack_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    rejected_at: datetime | None = None
    broker_latency_ms: int | None = None
    fill_latency_ms: int | None = None
    reconciliation_latency_ms: int | None = None
    execution_quality_score: Decimal | None = None
    execution_quality_grade: str | None = None
    execution_quality_notes: dict[str, Any] | None = None
    is_dry_run: bool
    cash_used: Decimal | None
    error_message: str | None
    signal_reason: str | None = None
    signal_confidence: Decimal | None = None
    signal_risk_rejected: bool | None = None
    signal_risk_rejection_reason: str | None = None
    retry_count: int
    last_reconciled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OrderEventOut(BaseSchema):
    id: uuid.UUID
    event_type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime


class OrderDetail(OrderOut):
    broker_request: dict[str, Any] | None
    broker_response: dict[str, Any] | None
    signal_snapshot: SignalOut | None = None
    events: list[OrderEventOut] = Field(default_factory=list)


# ─── Positions ───────────────────────────────────────────────────────────────

class PositionOut(BaseModel):
    ticker: str
    quantity: float
    avg_price: float
    current_price: float | None
    unrealized_pnl: float | None
    quantity_available: float | None
    value: float | None


# ─── Risk Events ─────────────────────────────────────────────────────────────

class RiskEventOut(BaseSchema):
    id: uuid.UUID
    event_type: str
    ticker: str | None
    message: str | None
    occurred_at: datetime


# ─── Alerts ──────────────────────────────────────────────────────────────────

class AlertOut(BaseSchema):
    id: uuid.UUID
    alert_type: str
    channel: str
    title: str
    message: str
    severity: str
    is_read: bool
    created_at: datetime


# ─── Settings ────────────────────────────────────────────────────────────────

class AppSettingsOut(BaseSchema):
    id: int
    theme: str
    timezone: str
    market_data_provider: str
    auto_trading_enabled: bool
    kill_switch_active: bool
    live_trading_unlocked: bool
    daily_stats_reset_time: str
    updated_at: datetime


class AppSettingsUpdate(BaseModel):
    theme: Literal["dark", "light"] | None = None
    timezone: str | None = None
    market_data_provider: str | None = None
    daily_stats_reset_time: str | None = None


class LiveReadinessCheck(BaseModel):
    key: str
    label: str
    status: Literal["pass", "fail"]
    detail: str
    verified_at: datetime | None = None


class LiveReadinessStatus(BaseModel):
    mode: str
    live_execution_enabled: bool
    live_trading_unlocked: bool
    eligible_for_unlock: bool
    ready_for_live: bool
    blockers: list[str]
    checks: list[LiveReadinessCheck]


class LiveReadinessActionRequest(BaseModel):
    action: Literal[
        "record_demo_validation",
        "record_broker_test",
        "record_telegram_test",
        "record_kill_switch_test",
        "unlock_live",
        "lock_live",
    ]
    notes: str | None = Field(default=None, max_length=500)


class TelegramStatusOut(BaseModel):
    bot_configured: bool
    alert_chat_configured: bool
    webhook_secret_configured: bool
    control_enabled: bool
    allowed_chat_count: int
    allowed_user_count: int
    confirmation_window_seconds: int
    supported_commands: list[str]


class TelegramWebhookResult(BaseModel):
    ok: bool = True
    handled: bool
    authorized: bool
    action: str | None = None
    requires_confirmation: bool = False
    executed: bool = False
    reply_text: str | None = None


class TelegramTestResult(BaseModel):
    sent: bool
    message: str


# ─── Audit Log ───────────────────────────────────────────────────────────────

class AuditLogOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: str | None
    actor: str
    ip_address: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime


class AuditLogList(BaseModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int


# ─── Reports ─────────────────────────────────────────────────────────────────

class PerformanceReport(BaseModel):
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float | None
    daily_pnl: list[dict[str, Any]]


class ExecutionQualitySummary(BaseModel):
    status: Literal["ok", "watch", "degraded", "no_data"]
    status_reason: str
    total_orders: int
    filled_orders: int
    rejected_orders: int
    cancelled_orders: int
    error_orders: int
    fill_rate: float
    reject_rate: float
    cancel_rate: float
    error_rate: float
    avg_score: float | None
    score_delta: float | None
    avg_slippage_pct: float | None
    total_slippage_value: float
    adverse_slippage_rate: float
    abnormal_slippage_count: int
    avg_broker_latency_ms: float | None
    avg_fill_latency_ms: float | None
    avg_reconciliation_latency_ms: float | None
    environments: list[str]


class ExecutionQualityBucket(BaseModel):
    environment: str
    ticker: str
    order_type: str
    order_count: int
    filled_count: int
    rejected_count: int
    cancelled_count: int
    error_count: int
    fill_rate: float
    avg_score: float | None
    avg_slippage_pct: float | None
    total_slippage_value: float
    avg_broker_latency_ms: float | None
    avg_fill_latency_ms: float | None
    worst_slippage_pct: float | None


class ExecutionQualityPattern(BaseModel):
    status: str
    ticker: str
    order_type: str
    reason: str
    count: int
    last_seen_at: datetime


class ExecutionQualityWorstOrder(BaseModel):
    id: uuid.UUID
    ticker: str
    side: str
    order_type: str
    environment: str
    status: str
    expected_fill_price: float | None
    avg_fill_price: float | None
    slippage_pct: float | None
    slippage_value: float | None
    broker_latency_ms: int | None
    fill_latency_ms: int | None
    score: float | None
    grade: str
    created_at: datetime


class ExecutionQualityReport(BaseModel):
    period_days: int
    generated_at: datetime
    include_dry_run: bool
    summary: ExecutionQualitySummary
    by_symbol_order_type: list[ExecutionQualityBucket]
    reject_cancel_patterns: list[ExecutionQualityPattern]
    worst_orders: list[ExecutionQualityWorstOrder]


# ─── Health ──────────────────────────────────────────────────────────────────

class HealthStatus(BaseModel):
    status: str
    timestamp: datetime
    version: str
    mode: str


class DepsHealth(BaseModel):
    database: str
    redis: str
    broker: str
    market_data: str
    workers: str | None = None
    startup: str | None = None


class StartupCheck(BaseModel):
    key: str
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class StartupHealth(BaseModel):
    status: Literal["pass", "warn", "fail"]
    mode: str
    failures: int
    warnings: int
    checks: list[StartupCheck]


class WorkerTaskHealth(BaseModel):
    task_name: str
    status: Literal["ok", "stale", "unknown"]
    detail: str
    last_seen_at: datetime | None = None
    age_seconds: int | None = None


class WorkersHealth(BaseModel):
    status: Literal["ok", "stale", "unknown"]
    tasks: list[WorkerTaskHealth]


class MarketDataSymbolHealth(BaseModel):
    ticker: str
    status: Literal["ok", "degraded", "stale", "fallback", "error", "unknown"]
    detail: str
    used_source: str
    validator_source: str | None = None
    fallback_used: bool = False
    primary_timestamp: datetime | None = None
    validator_timestamp: datetime | None = None
    divergence_pct: float | None = None
    checked_at: datetime | None = None


class MarketDataHealth(BaseModel):
    status: Literal["ok", "degraded", "stale", "fallback", "error", "unknown"]
    provider: str
    checked_at: datetime | None = None
    detail: str
    symbols: list[MarketDataSymbolHealth]


# ─── Trade Journal ───────────────────────────────────────────────────────────

class TradeJournalUpdate(BaseModel):
    notes: str | None = Field(None, max_length=5000)
    tags: list[str] | None = Field(None, max_length=20)
    emotion: str | None = Field(None, pattern="^(calm|anxious|confident|fearful|greedy|neutral)$")
    rating: int | None = Field(None, ge=1, le=5)


class TradeOut(BaseSchema):
    id: uuid.UUID
    ticker: str
    side: str
    quantity: Decimal
    open_price: Decimal
    close_price: Decimal | None
    realized_pnl: Decimal | None
    opened_at: datetime
    closed_at: datetime | None
    is_dry_run: bool
    journal_notes: str | None = None
    journal_tags: list[str] | None = None
    journal_emotion: str | None = None
    journal_rating: int | None = None
    journal_updated_at: datetime | None = None


class TradeList(BaseModel):
    items: list[TradeOut]
    total: int
    page: int
    page_size: int


# ─── Emergency ───────────────────────────────────────────────────────────────

class EmergencyActionResult(BaseModel):
    success: bool
    action: str
    message: str
    timestamp: datetime
