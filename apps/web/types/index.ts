// ── Auth ─────────────────────────────────────────────────────────────────────
export interface LoginRequest {
  email: string;
  password: string;
}
export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user_id: string;
  email: string;
  is_admin: boolean;
}
export interface User {
  id: string;
  email: string;
  is_active: boolean;
  is_admin: boolean;
  created_at: string;
}

// ── Broker ────────────────────────────────────────────────────────────────────
export type Environment = "demo" | "live" | "mock";
export type BrokerCredentialState =
  | "mock"
  | "configured"
  | "reconnect_required"
  | "not_connected";
export type BrokerDiagnosticLikelihood = "likely" | "possible";
export type BrokerDiagnosticKey =
  | "wrong_environment"
  | "invalid_credentials"
  | "ip_restriction";
export interface BrokerDiagnosticCause {
  key: BrokerDiagnosticKey;
  label: string;
  likelihood: BrokerDiagnosticLikelihood;
  detail: string;
}
export interface BrokerDiagnostics {
  code: "broker_auth_rejected";
  title: string;
  summary: string;
  environment: "demo" | "live";
  broker_host: string;
  http_status: number;
  causes: BrokerDiagnosticCause[];
  note: string;
}
export interface BrokerStatus {
  id: string;
  broker: string;
  environment: Environment;
  is_active: boolean;
  credential_state: BrokerCredentialState;
  recovery_hint: string | null;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_sync_at: string | null;
  account_id: string | null;
  account_currency: string | null;
  created_at: string;
}
export interface BrokerTestResult {
  is_ok: boolean;
  account_id: string | null;
  currency: string | null;
  error: string | null;
  diagnostics: BrokerDiagnostics | null;
}

// ── Account ───────────────────────────────────────────────────────────────────
export interface AccountSummary {
  total_value: number;
  cash: number;
  free_funds: number;
  invested: number;
  result: number;
  currency: string;
  synced_at: string | null;
  mode: string;
}
export interface CashGuardStatus {
  available_to_trade: number;
  reserved: number;
  total_cash: number;
  cash_only_mode: boolean;
  currency: string;
}

// ── Instruments ───────────────────────────────────────────────────────────────
export interface Instrument {
  id: string;
  ticker: string;
  name: string;
  type: string;
  currency_code: string;
  isin: string | null;
  extended_hours: boolean;
  working_schedule_id: number | null;
  trading_enabled: boolean;
  synced_at: string | null;
}
export interface InstrumentList {
  items: Instrument[];
  total: number;
}

// ── Risk ──────────────────────────────────────────────────────────────────────
export interface RiskProfile {
  id: string;
  name: string;
  max_risk_per_trade_pct: string;
  max_daily_loss_pct: string;
  max_open_positions: number;
  max_position_size_pct: string;
  max_trades_per_day: number;
  stop_after_consecutive_losses: number;
  symbol_cooldown_seconds: number;
  force_flat_eod: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

// ── Strategies ────────────────────────────────────────────────────────────────
export type StrategyType =
  | "orb"
  | "opening_fade"
  | "vwap_reclaim"
  | "closing_momentum"
  | "intraday_periodicity"
  | "mean_reversion"
  | "momentum"
  | "buy_hold_core"
  | "equal_weight_rebalance"
  | "cross_sectional_momentum"
  | "low_volatility_tilt"
  | "trend_following_tactical";
export type StrategyPresetKey =
  | "orb"
  | "opening_fade"
  | "vwap_reclaim"
  | "closing_momentum"
  | "intraday_periodicity";
export interface Strategy {
  id: string;
  name: string;
  type: StrategyType;
  description: string | null;
  is_enabled: boolean;
  is_live: boolean;
  venue: string;
  risk_profile_id: string | null;
  risk_profile?: RiskProfile | null;
  params: Record<string, unknown>;
  allowed_tickers: string[];
  session_start: string;
  session_end: string;
  extended_hours: boolean;
  eod_flatten: boolean;
  last_signal_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface StrategyPresetInfo {
  key: StrategyPresetKey;
  strategy_type: StrategyType;
  label: string;
  description: string;
  style: string;
  session_window: string;
  default_tickers: string[];
  default_params: Record<string, unknown>;
  risk_template_name: string;
  risk_summary: string;
}
export interface CreateStrategyPresetPayload {
  name?: string;
  allowed_tickers?: string[];
}
export interface CreateStrategyPayload {
  name: string;
  type: StrategyType;
  description?: string;
  params?: Record<string, unknown>;
  allowed_tickers?: string[];
  session_start?: string;
  session_end?: string;
  extended_hours?: boolean;
  eod_flatten?: boolean;
  risk_profile_id?: string;
  is_live?: boolean;
}
export interface StrategyDryRunResult {
  message: string;
  is_live: boolean;
  summary?: Record<string, unknown>;
}
export type StrategyPromotionStage = "dry_run" | "demo" | "live_approved";
export type StrategyPromotionAction =
  | "record_dry_run_review"
  | "promote_to_demo"
  | "record_demo_review"
  | "promote_to_live"
  | "demote_to_dry_run"
  | "revoke_live_promotion";
export interface StrategyPromotionCheck {
  phase: "demo" | "live";
  key: string;
  label: string;
  status: "pass" | "fail";
  detail: string;
  verified_at: string | null;
}
export interface StrategyPromotionMetrics {
  dry_run_signal_count: number;
  dry_run_order_count: number;
  dry_run_days: number;
  dry_run_reviewed_at: string | null;
  demo_order_count: number;
  demo_filled_count: number;
  demo_rejected_count: number;
  demo_error_count: number;
  demo_cancelled_count: number;
  demo_days: number;
  demo_fill_rate: number;
  demo_error_rate: number;
  demo_signal_count: number;
  demo_risk_block_count: number;
  demo_risk_block_rate: number;
  demo_promoted_at: string | null;
  demo_reviewed_at: string | null;
  live_approved_at: string | null;
}
export interface StrategyPromotionStatus {
  strategy_id: string;
  strategy_name: string;
  current_stage: StrategyPromotionStage;
  broker_execution_enabled: boolean;
  demo_execution_enabled: boolean;
  live_execution_approved: boolean;
  eligible_for_demo: boolean;
  eligible_for_live: boolean;
  recommended_next_action: StrategyPromotionAction | null;
  blockers: string[];
  checks: StrategyPromotionCheck[];
  metrics: StrategyPromotionMetrics;
}
export interface PortfolioWeightSnapshot {
  ticker: string;
  target_weight: number | null;
  current_weight: number | null;
  delta_weight: number | null;
}
export interface PortfolioRebalanceOrder {
  order_id: string;
  signal_id: string | null;
  ticker: string;
  side: string;
  status: string;
  quantity: number;
  avg_fill_price: number | null;
  target_weight: number | null;
  allocation_status: "allocated" | "rejected" | null;
  allocation_score: number | null;
  allocation_reason: string | null;
  is_dry_run: boolean;
  created_at: string;
}
export interface PortfolioStrategyMonitoring {
  strategy_id: string;
  strategy_name: string;
  strategy_type: StrategyType;
  is_enabled: boolean;
  is_live: boolean;
  last_status: string | null;
  last_reason: string | null;
  last_run_at: string | null;
  last_rebalance_at: string | null;
  last_mode: string | null;
  last_orders_submitted: number;
  last_dry_run_orders: number;
  last_risk_blocks: number;
  last_allocation_blocks: number;
  last_allocation_decisions: AllocatorDecision[];
  weights: PortfolioWeightSnapshot[];
  recent_orders: PortfolioRebalanceOrder[];
}
export interface PortfolioTimelinePoint {
  date: string;
  equity_pnl: number;
  benchmark_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  cash_balance: number;
  gross_exposure: number;
  drawdown_pct: number;
  benchmark_drawdown_pct: number;
  turnover_notional: number;
  order_count: number;
}
export interface PortfolioRebalanceWeightChange {
  ticker: string;
  target_weight: number | null;
  before_weight: number | null;
  after_weight: number | null;
  before_gap: number | null;
  after_gap: number | null;
}
export interface PortfolioRebalanceEvent {
  date: string;
  order_count: number;
  turnover_notional: number;
  total_pnl_after: number;
  weights: PortfolioRebalanceWeightChange[];
}
export interface PortfolioTickerAttribution {
  ticker: string;
  quantity: number;
  avg_cost: number;
  market_price: number;
  market_value: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  weight_pct: number;
}
export interface PortfolioStrategyAttributionSummary {
  strategy_id: string;
  strategy_name: string;
  strategy_type: StrategyType;
  computed_at: string;
  benchmark_name: string;
  total_return_pct: number;
  benchmark_return_pct: number;
  alpha_vs_benchmark_pct: number;
  max_drawdown_pct: number;
  benchmark_max_drawdown_pct: number;
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  cash_balance: number;
  current_market_value: number;
  turnover_notional: number;
  buys_notional: number;
  sells_notional: number;
  rebalance_days: number;
  order_count: number;
  recent_timeline: PortfolioTimelinePoint[];
}
export interface PortfolioStrategyAttribution
  extends PortfolioStrategyAttributionSummary {
  timeline: PortfolioTimelinePoint[];
  ticker_attribution: PortfolioTickerAttribution[];
  rebalance_events: PortfolioRebalanceEvent[];
}
export interface MarketRegime {
  regime: string;
  label: string;
  color: string;
  adx: number;
  vol_percentile: number;
  confidence: number;
  breadth_pct: number | null;
  primary_trend: string | null;
  active_strategies: string[];
  suppressed_strategies: string[];
  detail: string | null;
}
export interface WatchlistNewsItem {
  id: string;
  source: string;
  title: string;
  summary: string;
  url: string | null;
  published_at: string | null;
  tickers: string[];
  event_type: string;
  sentiment_score: number;
  urgency_score: number;
  credibility_score: number;
  impact_horizon: string;
  catalyst_score: number;
}
export interface WatchlistIntelligence {
  watchlist: string[];
  news: WatchlistNewsItem[];
  count: number;
}
export interface WatchlistCandidateContext {
  ticker: string;
  score: number;
  reason: string | null;
  strategy_type: string | null;
  pre_market_rvol: number | null;
  gap_pct: number | null;
  catalyst_score: number | null;
  catalyst_event_type: string | null;
  catalyst_summary: string | null;
  catalyst_source: string | null;
  feed_status: string;
  blocked_reason: string | null;
  trade_safe: boolean;
}
export interface AllocatorDecision {
  ticker: string;
  side: string;
  strategy_id: string;
  strategy_name: string;
  strategy_type: string;
  signal_type: string;
  status: "allocated" | "rejected";
  score: number;
  threshold: number;
  reason: string;
  rank: number | null;
  components: Record<string, number>;
  penalties: Record<string, number>;
  allocated_risk_pct: number;
  projected_gross_exposure_pct: number;
  projected_symbol_exposure_pct: number;
  regime_cap_pct: number;
  generated_at: string | null;
}
export interface StrategyIntelligence {
  strategy_id: string;
  strategy_name: string;
  strategy_type: StrategyType;
  regime: MarketRegime;
  feed_health: MarketDataHealth;
  watchlist: WatchlistCandidateContext[];
  recent_risk_blocks: RiskEvent[];
  recent_allocation_decisions: AllocatorDecision[];
}

// ── Signals ───────────────────────────────────────────────────────────────────
export interface Signal {
  id: string;
  strategy_id: string;
  strategy_name: string | null;
  strategy_type_name: string | null;
  ticker: string;
  side: "buy" | "sell";
  signal_type: string;
  status: string;
  entry_price: string | null;
  stop_price: string | null;
  take_profit_price: string | null;
  suggested_quantity: string | null;
  confidence: string | null;
  reason: string | null;
  risk_rejected: boolean;
  risk_rejection_reason: string | null;
  params_snapshot: Record<string, unknown> | null;
  generated_at: string;
  expires_at: string | null;
  executed_at: string | null;
}

// ── Orders ────────────────────────────────────────────────────────────────────
export type OrderSide = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type OrderStatus =
  | "pending_intent"
  | "submitted"
  | "accepted"
  | "filled"
  | "cancelled"
  | "rejected"
  | "error";
export interface Order {
  id: string;
  signal_id: string | null;
  strategy_id: string | null;
  strategy_name: string | null;
  strategy_type_name: string | null;
  client_order_key: string;
  ticker: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: string;
  limit_price: string | null;
  stop_price: string | null;
  time_validity: string;
  status: OrderStatus;
  broker_order_id: string | null;
  filled_quantity: string | null;
  avg_fill_price: string | null;
  execution_environment: string | null;
  expected_fill_price: string | null;
  slippage_pct: string | null;
  slippage_value: string | null;
  submitted_at: string | null;
  first_ack_at: string | null;
  filled_at: string | null;
  cancelled_at: string | null;
  rejected_at: string | null;
  broker_latency_ms: number | null;
  fill_latency_ms: number | null;
  reconciliation_latency_ms: number | null;
  execution_quality_score: string | null;
  execution_quality_grade: string | null;
  execution_quality_notes: Record<string, unknown> | null;
  is_dry_run: boolean;
  cash_used: string | null;
  error_message: string | null;
  signal_reason: string | null;
  signal_confidence: string | null;
  signal_risk_rejected: boolean | null;
  signal_risk_rejection_reason: string | null;
  retry_count: number;
  last_reconciled_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface OrderEvent {
  id: string;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  payload: Record<string, unknown> | null;
  occurred_at: string;
}
export interface OrderDetail extends Order {
  broker_request: Record<string, unknown> | null;
  broker_response: Record<string, unknown> | null;
  signal_snapshot: Signal | null;
  events: OrderEvent[];
}
export interface PaperExecutionHistoryItem {
  id: string;
  order_id: string | null;
  created_at: string;
  updated_at: string | null;
  ticker: string;
  side: OrderSide | string | null;
  quantity: string | null;
  notional: string | null;
  venue: string | null;
  source: string | null;
  strategy: string | null;
  status: string;
  risk_result: "allowed" | "blocked" | "unknown";
  fill_price: string | null;
  filled_quantity: string | null;
  paper_only: true;
  live_order_sent: false;
  no_broker_order_sent: true;
  rejection_reason: string | null;
  audit_count: number;
  latest_audit_at: string | null;
}
export interface PaperExecutionHistory {
  items: PaperExecutionHistoryItem[];
  total: number;
  limit: number;
}
export interface PaperExecutionAuditEntry {
  id: string;
  occurred_at: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  actor: string;
  summary: string;
  metadata: Record<string, unknown>;
}
export interface PaperExecutionAudit {
  order_id: string;
  paper_only: true;
  live_order_sent: false;
  no_broker_order_sent: true;
  items: PaperExecutionAuditEntry[];
}
export interface CreateOrderPayload {
  ticker: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: string;
  limit_price?: string;
  stop_price?: string;
  time_validity?: "DAY" | "GTC";
  signal_id?: string;
}
export interface CreatePaperOrderPayload {
  ticker: string;
  side: OrderSide;
  quantity?: string;
  notional?: string;
  estimated_price?: string;
  order_type?: "market";
  strategy?: string;
  source?: string;
  venue?: "paper" | "mock";
  paper_only: true;
}

// ── Positions ─────────────────────────────────────────────────────────────────
export interface Position {
  ticker: string;
  quantity: number;
  avg_price: number;
  current_price: number | null;
  unrealized_pnl: number | null;
  quantity_available: number | null;
  value: number | null;
}

// ── Execution Quality ────────────────────────────────────────────────────────
export type ExecutionQualityStatus = "ok" | "watch" | "degraded" | "no_data";
export interface ExecutionQualitySummary {
  status: ExecutionQualityStatus;
  status_reason: string;
  total_orders: number;
  filled_orders: number;
  rejected_orders: number;
  cancelled_orders: number;
  error_orders: number;
  fill_rate: number;
  reject_rate: number;
  cancel_rate: number;
  error_rate: number;
  avg_score: number | null;
  score_delta: number | null;
  avg_slippage_pct: number | null;
  total_slippage_value: number;
  adverse_slippage_rate: number;
  abnormal_slippage_count: number;
  avg_broker_latency_ms: number | null;
  avg_fill_latency_ms: number | null;
  avg_reconciliation_latency_ms: number | null;
  environments: string[];
}
export interface ExecutionQualityBucket {
  environment: string;
  ticker: string;
  order_type: string;
  order_count: number;
  filled_count: number;
  rejected_count: number;
  cancelled_count: number;
  error_count: number;
  fill_rate: number;
  avg_score: number | null;
  avg_slippage_pct: number | null;
  total_slippage_value: number;
  avg_broker_latency_ms: number | null;
  avg_fill_latency_ms: number | null;
  worst_slippage_pct: number | null;
}
export interface ExecutionQualityPattern {
  status: string;
  ticker: string;
  order_type: string;
  reason: string;
  count: number;
  last_seen_at: string;
}
export interface ExecutionQualityWorstOrder {
  id: string;
  ticker: string;
  side: string;
  order_type: string;
  environment: string;
  status: string;
  expected_fill_price: number | null;
  avg_fill_price: number | null;
  slippage_pct: number | null;
  slippage_value: number | null;
  broker_latency_ms: number | null;
  fill_latency_ms: number | null;
  score: number | null;
  grade: string;
  created_at: string;
}
export interface ExecutionQualityReport {
  period_days: number;
  generated_at: string;
  include_dry_run: boolean;
  summary: ExecutionQualitySummary;
  by_symbol_order_type: ExecutionQualityBucket[];
  reject_cancel_patterns: ExecutionQualityPattern[];
  worst_orders: ExecutionQualityWorstOrder[];
}

// ── Alerts ────────────────────────────────────────────────────────────────────
export interface Alert {
  id: string;
  alert_type: string;
  channel: string;
  title: string;
  message: string;
  severity: "info" | "warning" | "error" | "critical";
  is_read: boolean;
  created_at: string;
}

// ── Settings ──────────────────────────────────────────────────────────────────
export interface AppSettings {
  id: number;
  theme: string;
  timezone: string;
  market_data_provider: string;
  auto_trading_enabled: boolean;
  kill_switch_active: boolean;
  live_trading_unlocked: boolean;
  daily_stats_reset_time: string;
  updated_at: string;
}

export interface LiveReadinessCheck {
  key: string;
  label: string;
  status: "pass" | "fail";
  detail: string;
  verified_at: string | null;
}

export interface LiveReadinessStatus {
  mode: string;
  live_execution_enabled: boolean;
  live_trading_unlocked: boolean;
  eligible_for_unlock: boolean;
  ready_for_live: boolean;
  blockers: string[];
  checks: LiveReadinessCheck[];
}

export type LiveReadinessAction =
  | "record_demo_validation"
  | "record_broker_test"
  | "record_telegram_test"
  | "record_kill_switch_test"
  | "unlock_live"
  | "lock_live";

// ── Operator Status ──────────────────────────────────────────────────────────
export type OperatorWorkerHealth = "healthy" | "stale" | "missing" | "unknown";
export type OperatorOverallStatus = "ok" | "degraded" | "blocked";

export interface OperatorVenueStatus {
  venue: string;
  present: boolean;
  kill_switch_active: boolean | null;
  auto_trading_enabled: boolean | null;
  degraded_mode_active: boolean | null;
  note: string | null;
  updated_at: string | null;
}

export interface OperatorTrading212Status {
  strategies_count: number;
  live_approved_strategies_count: number;
  active_orders_count: number;
  recent_orders_count: number;
  latest_order_status: string | null;
  live_readiness_status: LiveReadinessStatus | null;
  safety_notes: string[];
}

export interface OperatorKrakenStatus {
  strategies_count: number;
  paper_only_strategies_count: number;
  live_enabled: boolean;
  recent_orders_count: number;
  active_orders_count: number;
  venue_config: OperatorVenueStatus | null;
  safety_notes: string[];
}

export interface OperatorDcaStatus {
  config_count: number;
  enabled_config_count: number;
  decision_count_total: number;
  buy_due_count: number;
  blocked_count: number;
  skipped_count: number;
  total_paper_allocated_usd: number | string;
  scheduler_registered: boolean;
  scheduler_cadence: string | null;
  worker_health: OperatorWorkerHealth;
  runnable: boolean;
  live_enabled: boolean;
  paper_only: boolean;
  tickers: string[];
}

export interface OperatorPaperExecutionStatus {
  paper_only: true;
  enabled_in_mode: "mock";
  total_paper_orders: number;
  latest_paper_order_timestamp: string | null;
  last_paper_execution_status: string | null;
  open_paper_positions_count: number;
  safety_notes: string[];
}

export interface OperatorSchedulersStatus {
  dca_paper_evaluate_registered: boolean;
  dca_paper_evaluate_cadence: string | null;
  heartbeat_registered: boolean;
  heartbeat_cadence: string | null;
  worker_health: OperatorWorkerHealth;
  heartbeat_component: string;
  heartbeat_last_seen_at: string | null;
  heartbeat_stale_after_seconds: number;
}

export interface OperatorRecentActivity {
  id: string;
  occurred_at: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  actor: string;
  payload_summary: Record<string, unknown>;
}

export interface OperatorSafetyFlags {
  endpoint_read_only: boolean;
  creates_orders: boolean;
  calls_brokers: boolean;
  triggers_schedulers: boolean;
  runs_strategies: boolean;
  dca_runnable: boolean;
  dca_live_enabled: boolean;
  kraken_live_enabled: boolean;
  cash_only_mode: boolean;
  live_trading_enabled_setting: boolean;
  app_live_trading_unlocked: boolean;
  any_venue_kill_switch_active: boolean;
  any_venue_degraded: boolean;
  missing_expected_venue_configs: boolean;
  worker_health_known: boolean;
}

export interface OperatorStatus {
  subsystem: "operator";
  mode: "read_only_status";
  generated_at: string | null;
  overall_status: OperatorOverallStatus;
  live_trading_possible: boolean;
  live_trading_enabled_anywhere: boolean;
  venues: OperatorVenueStatus[];
  trading212: OperatorTrading212Status;
  kraken: OperatorKrakenStatus;
  dca: OperatorDcaStatus;
  paper_execution: OperatorPaperExecutionStatus;
  schedulers: OperatorSchedulersStatus;
  recent_activity: OperatorRecentActivity[];
  safety_flags: OperatorSafetyFlags;
}

export interface TelegramStatus {
  bot_configured: boolean;
  alert_chat_configured: boolean;
  webhook_secret_configured: boolean;
  control_enabled: boolean;
  allowed_chat_count: number;
  allowed_user_count: number;
  confirmation_window_seconds: number;
  supported_commands: string[];
}

export interface TelegramTestResult {
  sent: boolean;
  message: string;
}

// ── Backtest ─────────────────────────────────────────────────────────────────
export type BacktestStrategyType =
  | "orb"
  | "opening_fade"
  | "vwap_reclaim"
  | "closing_momentum"
  | "intraday_periodicity";
export type PortfolioBacktestStrategyType =
  | "buy_hold_core"
  | "equal_weight_rebalance"
  | "cross_sectional_momentum"
  | "low_volatility_tilt"
  | "trend_following_tactical";

export interface BacktestStrategyInfo {
  type: BacktestStrategyType;
  label: string;
  description: string;
}

export interface PortfolioBacktestStrategyInfo {
  type: PortfolioBacktestStrategyType;
  label: string;
  description: string;
  rationale: string;
  rebalance_frequency: string;
  min_history_bars: number;
}

export interface BacktestRunRequest {
  ticker: string;
  strategy_type: BacktestStrategyType;
  strategy_params?: Record<string, unknown>;
  from_date: string;
  to_date: string;
  initial_capital: number;
  run_walk_forward: boolean;
}

export interface PortfolioBacktestRunRequest {
  tickers: string[];
  strategy_type: PortfolioBacktestStrategyType;
  strategy_params?: Record<string, unknown>;
  from_date: string;
  to_date: string;
  initial_capital: number;
}

export interface BacktestJobResponse {
  job_id: string;
  status: string;
  message: string;
}

export interface BacktestTrade {
  id: string;
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  exit_reason: string;
  holding_bars: number;
  slippage: number;
  mfe: number;
  mae: number;
}

export interface BacktestEquityPoint {
  time: string;
  equity: number;
  cash: number;
  position_value: number;
  bar_idx: number;
}

export interface BacktestMonteCarlo {
  iterations: number;
  message?: string;
  median_max_drawdown_pct?: number;
  p95_max_drawdown_pct?: number;
  worst_max_drawdown_pct?: number;
  median_consecutive_losses?: number;
  p95_consecutive_losses?: number;
  probability_drawdown_gt_10pct?: number;
  probability_drawdown_gt_20pct?: number;
}

export interface WalkForwardWindow {
  window: number;
  is_start: string;
  is_end: string;
  oos_start: string;
  oos_end: string;
  best_params: Record<string, unknown>;
  oos_return_pct: number;
  oos_sharpe: number;
  oos_max_dd: number;
  oos_win_rate: number;
  oos_profit_factor: number;
  oos_trades: number;
}

export interface WalkForwardSummary {
  windows: number;
  verdict: string;
  message?: string;
  profitable_windows?: number;
  positive_sharpe_windows?: number;
  controlled_drawdown_windows?: number;
  avg_oos_return_pct?: number;
  median_oos_return_pct?: number;
  avg_oos_sharpe?: number;
  median_oos_sharpe?: number;
  worst_oos_max_dd?: number;
  robustness_score?: number;
}

export interface BacktestInterpretation {
  verdict: string;
  summary: string;
  warnings: string[];
}

export interface BacktestResult {
  strategy: string;
  strategy_name: string;
  strategy_type: BacktestStrategyType;
  ticker: string;
  from: string;
  to: string;
  initial_capital: number;
  final_capital: number;
  gross_pnl: number;
  net_pnl: number;
  gross_return_pct: number;
  total_return_pct: number;
  annualised_return_pct: number;
  benchmark_return_pct: number;
  alpha_vs_benchmark_pct: number;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown_pct: number;
  max_drawdown_duration_days: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  win_rate_pct: number;
  profit_factor: number;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  expectancy_pct: number;
  avg_rr_achieved: number;
  consecutive_losses_max: number;
  total_slippage_cost: number;
  total_commission_cost: number;
  avg_holding_bars: number;
  turnover_pct: number;
  exposure_pct: number;
  avg_mfe: number;
  avg_mae: number;
  monte_carlo: BacktestMonteCarlo;
  equity_curve: BacktestEquityPoint[];
  trades: BacktestTrade[];
}

export interface BacktestJob {
  status: "running" | "complete" | "error";
  created_at?: string;
  ticker?: string;
  strategy_type?: BacktestStrategyType;
  bars_used?: number;
  result?: BacktestResult;
  walk_forward?: WalkForwardWindow[] | null;
  walk_forward_summary?: WalkForwardSummary | null;
  interpretation?: BacktestInterpretation;
  error?: string;
  traceback?: string;
}

export interface PortfolioBacktestTrade {
  date: string;
  ticker: string;
  side: string;
  shares: number;
  price: number;
  notional: number;
  cost: number;
  reason: string;
  target_weight: number;
}

export interface PortfolioBacktestEquityPoint {
  date: string;
  equity: number;
  cash: number;
  exposure_pct: number;
  drawdown_pct: number;
  weights: Record<string, number>;
}

export interface PortfolioBacktestResult {
  strategy: string;
  strategy_name: string;
  strategy_type: PortfolioBacktestStrategyType;
  universe: string[];
  from: string;
  to: string;
  initial_capital: number;
  final_capital: number;
  total_return_pct: number;
  annualised_return_pct: number;
  benchmark_name: string;
  benchmark_return_pct: number;
  alpha_vs_benchmark_pct: number;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown_pct: number;
  total_trades: number;
  rebalance_count: number;
  turnover_pct: number;
  avg_exposure_pct: number;
  latest_weights: Record<string, number>;
  equity_curve: PortfolioBacktestEquityPoint[];
  trades: PortfolioBacktestTrade[];
  rationale: string;
}

export interface PortfolioBacktestJob {
  status: "running" | "complete" | "error";
  created_at?: string;
  tickers?: string[];
  strategy_type?: PortfolioBacktestStrategyType;
  bars_used?: number;
  result?: PortfolioBacktestResult;
  interpretation?: BacktestInterpretation;
  error?: string;
  traceback?: string;
}

// ── Audit ─────────────────────────────────────────────────────────────────────
export interface AuditLog {
  id: string;
  user_id: string | null;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  actor: string;
  ip_address: string | null;
  payload: Record<string, unknown> | null;
  occurred_at: string;
}
export interface AuditLogList {
  items: AuditLog[];
  total: number;
  page: number;
  page_size: number;
}

// ── Risk Events ───────────────────────────────────────────────────────────────
export interface RiskEvent {
  id: string;
  event_type: string;
  ticker: string | null;
  message: string | null;
  occurred_at: string;
}

// ── Reports ───────────────────────────────────────────────────────────────────
export interface PerformanceReport {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number;
  max_drawdown: number;
  sharpe_ratio: number | null;
  daily_pnl: Array<{ date: string; pnl: number }>;
}

// ── Health ────────────────────────────────────────────────────────────────────
export interface HealthStatus {
  status: string;
  timestamp: string;
  version: string;
  mode: string;
}
export interface DepsHealth {
  database: string;
  redis: string;
  broker: string;
  market_data: string;
  workers?: string | null;
  startup?: string | null;
}
export interface StartupCheck {
  key: string;
  label: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}
export interface StartupHealth {
  status: "pass" | "warn" | "fail";
  mode: string;
  failures: number;
  warnings: number;
  checks: StartupCheck[];
}
export interface MarketDataSymbolHealth {
  ticker: string;
  status: string;
  detail: string;
  used_source: string;
  validator_source: string | null;
  fallback_used: boolean;
  primary_timestamp: string | null;
  validator_timestamp: string | null;
  divergence_pct: number | null;
  checked_at: string | null;
}
export interface MarketDataHealth {
  status: string;
  provider: string;
  checked_at: string | null;
  detail: string;
  symbols: MarketDataSymbolHealth[];
}

// ── Emergency ─────────────────────────────────────────────────────────────────
export interface EmergencyActionResult {
  success: boolean;
  action: string;
  message: string;
  timestamp: string;
}

// ── Pagination ────────────────────────────────────────────────────────────────
export interface PaginatedList<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

// T-OPS-004: Read-only Kraken DCA activity report types.
export interface DcaActivityConfigSummary {
  id: string;
  ticker: string;
  venue: string;
  enabled: boolean;
  paper_only: boolean;
  cadence_days?: number;
  fixed_cash_amount?: string | number;
  max_position_percent?: string | number;
}

export interface DcaActivityTickerSummary {
  ticker: string;
  venue: string;
  enabled?: boolean;
  latest_decision_code?: string | null;
  latest_decision_at?: string | null;
  latest_reason?: string | null;
  total_allocated_usd?: string | number;
  executions_count?: number;
  last_buy_at?: string | null;
  decision_counts_by_code?: Record<string, number>;
}

export interface DcaRecentDecisionSummary {
  audit_id?: string;
  occurred_at?: string;
  created_at?: string;
  ticker?: string | null;
  venue?: string | null;
  decision_code?: string | null;
  reason?: string | null;
  summary?: string | null;
}

export interface DcaActivitySafetyFlags {
  dca_planner_runnable_is_false?: boolean;
  dca_planner_paper_only_is_true?: boolean;
  main_runner_registered?: boolean;
  order_creation_supported?: boolean;
  execution_called_by_report?: boolean;
  provider_called_by_report?: boolean;
  scheduler_triggered_by_report?: boolean;
  [key: string]: boolean | string | number | null | undefined;
}

export interface DcaConfig {
  id: string;
  ticker: string;
  venue: string;
  cadence_days: number;
  fixed_cash_amount: string | number;
  dip_buy_enabled: boolean;
  dip_buy_multiplier: string | number;
  min_cash_reserve: string | number;
  max_position_percent: string | number;
  paper_only: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface DcaLatestState {
  last_buy_at: string | null;
  last_decision_at: string | null;
  total_allocated_usd: string | number;
  executions_count: number;
  last_decision_code: string | null;
  last_reason: string | null;
}

export interface DcaConfigStatus {
  id: string;
  ticker: string;
  venue: string;
  enabled: boolean;
  paper_only: boolean;
  cadence_days: number;
  fixed_cash_amount: string | number;
  min_cash_reserve: string | number;
  max_position_percent: string | number;
  dip_buy_enabled: boolean;
  dip_buy_multiplier: string | number;
  latest_state: DcaLatestState | null;
}

export interface DcaAuditEntry {
  id: string;
  created_at: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  actor: string;
  metadata: Record<string, unknown> | null;
}

export interface DcaOperatorStatus {
  subsystem: "kraken_dca";
  mode: "paper_only";
  runnable: boolean;
  live_enabled: boolean;
  scheduler_registered: boolean;
  scheduler_cadence: string | null;
  config_count: number;
  enabled_config_count: number;
  configs: DcaConfigStatus[];
  recent_audit_entries: DcaAuditEntry[];
  safety_flags: DcaActivitySafetyFlags;
}

export interface DcaActivityResponse {
  subsystem: string;
  mode: string;
  runnable: boolean;
  live_enabled: boolean;
  generated_at?: string;
  config_count: number;
  enabled_config_count: number;
  decision_count_total: number;
  decision_counts_by_code: Record<string, number>;
  buy_due_count: number;
  blocked_count: number;
  skipped_count: number;
  total_paper_allocated_usd: string | number;
  order_count_sanity?: number;
  configs: DcaActivityConfigSummary[];
  per_ticker_activity: DcaActivityTickerSummary[];
  recent_decisions: DcaRecentDecisionSummary[];
  safety_flags: DcaActivitySafetyFlags;
}
