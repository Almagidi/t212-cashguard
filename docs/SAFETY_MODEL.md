# Safety Model

T212 CashGuard Trader is local-first and cash-only. The backend is the safety boundary; frontend controls only mirror backend state and are never the only guard.

## Runtime Modes

- **Mock mode** is the default. It uses local/mock data and must not call Trading 212 demo or live endpoints.
- **Paper mode** is local paper execution inside mock mode through `/v1/orders/paper`. It records local orders, simulates fills, and always marks `no_broker_order_sent=true`.
- **Demo mode** may use Trading 212 demo endpoints only when `APP_MODE=demo`, demo credentials or an encrypted demo broker connection are configured, and the user explicitly selects demo mode. Demo mode must never fall through to live credentials or the live base URL.
- **Live mode** is guarded for future use. It requires `APP_MODE=live`, `LIVE_TRADING_ENABLED=true`, a live broker connection, recent broker test, Telegram supervision, demo validation, kill-switch drill evidence, kill switch clear, and final admin unlock.

## Broker Boundary

Broker access is centralized through the backend safety policy. Mock mode blocks real broker environments. Demo mode blocks live environments. Live mode blocks broker calls unless the live flag is enabled and live readiness passes before order submission.

Direct `Trading212Adapter` construction is also policy-gated. It rejects mock/paper runtime modes, unknown app modes, unknown broker environments, blank environment credentials, demo-to-live attempts, and live adapter construction while `LIVE_TRADING_ENABLED=false`.

Trading 212 remains the current broker adapter. Future broker support must be added behind a common backend broker interface, with broker-specific mappers and safety gates kept explicit. Live trading and strategy-driven broker writes remain out of scope, and no second broker should be wired in until Trading 212 pending-order and reconciliation behaviours are fully understood.

Broker-neutral snapshot mappers are pure transformation utilities only; they do not perform broker reads, broker writes, API calls, database access, or scheduler/worker actions.

The demo reconciliation broker protocol remains read-only and excludes placement, cancellation, and other broker write methods.

Broker provider work must preserve explicit broker and environment safety gates; it must not introduce generic live broker construction or bypass Trading 212 credential-specific checks.

The provider request and credential validation helpers do not select credentials, decrypt secrets, read environment variables, query the database, or place/cancel orders. `get_broker()` now calls `create_trading212_provider_adapter(...)` only after its existing credential lookup, decryption, fallback, and safety-policy decisions have already selected explicit Trading 212 credentials.

`get_broker()` behaviour-equivalence tests lock current credential precedence, demo fallback, live flag blocking, credential decrypt failure behaviour, and provider request data during this migration.

`/v1/broker/trading212/connect` and `/v1/broker/trading212/test` now use the provider only for final adapter construction during credential tests. Submitted credential handling, encrypted credential storage, decryption, reconnect-required handling, route schemas, and audit behaviour remain in the route layer.

Scheduler/worker provider-equivalence tests document the migrated demo-only construction gates. Scheduler startup and the terminal one-shot worker now use `create_trading212_provider_adapter(...)` only for final Trading 212 adapter construction after their existing demo mode, demo environment, live-disabled, enabled-state, and credential-source checks pass. They avoid live credentials and remain read-only.

Remaining direct Trading 212 construction paths are inventoried and locked by a unit test. Read-only account sync, CFD funding tracking, pending-order reconciliation, timed-out-order cancellation, `PositionMonitor._get_broker`, `StrategyRunner._get_broker`, `PortfolioExecutionService._get_broker`, and `SystemControlService._get_broker` now use the provider only for final adapter construction, after caller-owned active connection lookup, credential decryption, reconnect-required handling, and environment gates. Account sync calls only `get_account_summary()` before persisting a local snapshot; CFD funding calls only `get_positions()` before persisting local funding records; pending-order reconciliation still hands the provider-created broker to `ExecutionEngine.reconcile_order(...)` for the same selected orders; timed-out-order cancellation still hands the provider-created broker to `ExecutionEngine.cancel_order(...)` for the same selected stale orders; PositionMonitor still hands the provider-created broker to existing position/account reads and `ExecutionEngine` exit/EOD flatten paths; StrategyRunner still hands the provider-created broker to existing account/position reads and `ExecutionEngine` entry/exit paths; PortfolioExecution still hands the provider-created broker to existing account/position reads and fake-tested `ExecutionEngine` rebalance order paths; SystemControl read/status calls use `operator_system_control_read`, while emergency cancel/flatten uses `operator_system_control_emergency`.

After PR #80, the remaining direct `Trading212Adapter` runtime references are limited to the adapter implementation, the canonical provider final-construction boundary, and terminal-only/manual DEMO smoke scripts. They do not include production operational workers or services. This is still not a live-readiness claim: live trading remains disabled unless the existing live gates are explicitly satisfied, Kraken/crypto work has not started, and Dependabot/frontend dependency changes remain unrelated to the broker provider boundary.

Focused order-worker provider-equivalence tests now lock `reconcile_pending_orders` and `cancel_timed_out_orders` as provider-backed for final adapter construction only. They prove these workers skip adapter construction in unsafe states, preserve both demo and live behaviour from the previous direct paths, use active encrypted connection credentials for the current runtime mode, and hand the constructed broker only to a fake `ExecutionEngine` in tests. `cancel_timed_out_orders` uses provider purpose `worker_cancel_timed_out_orders` and remains write-capable because cancellation still routes through `ExecutionEngine.cancel_order(...)`. Live-disabled mismatch remains a policy-layer rejection through `require_broker_environment(...)`, not a separate worker-owned gate.

Order submission, cancellation, strategy execution, portfolio execution, and emergency system-control paths remain separately controlled so provider work does not expand broker write reach. A source-level write-capable provider-boundary audit now locks this state without executing broker code: `cancel_timed_out_orders` is provider-backed with purpose `worker_cancel_timed_out_orders` but remains cancellation-capable through `ExecutionEngine.cancel_order(...)`; `PositionMonitor._get_broker` is provider-backed with purpose `worker_position_monitor` but remains write-capable for automated exits and EOD flatten; `StrategyRunner._get_broker` is provider-backed with purpose `worker_strategy_runner` but remains mixed/write-capable for strategy entries and exits; `PortfolioExecutionService._get_broker` is provider-backed with purpose `worker_portfolio_execution` but remains mixed/write-capable for rebalance orders; and `system_control` is provider-backed but remains mixed/write-capable because read/status calls and emergency cancel/flatten share one broker helper. Manual smoke scripts remain terminal-only/manual DEMO tools and are not production provider migration targets. This PR does not change live trading, order placement, cancellation behaviour, credential storage/decryption, route schemas, frontend controls, or add Kraken/Alpaca support.

SystemControlService now uses the Trading 212 provider for final adapter construction. `SystemControlService._get_broker` still preserves the existing mock-mode shortcut, unsafe app-mode rejection before DB/provider construction, active encrypted connection lookup, optional `broker_user_id` scoping, connection-environment gate, credential decryption, and reconnect-required `commit=True` handling on decrypt failure. The provider request is user-scoped and uses purpose `operator_system_control_read` for `get_snapshot()` and `get_positions_summary()`, and purpose `operator_system_control_emergency` for `cancel_all_pending()` and `flatten_all()`. Read/status methods remain read-only against fakes and do not use `ExecutionEngine`; emergency cancel-all and flatten-all remain write-capable through `ExecutionEngine.cancel_order(...)` and order intent/submission routing. Emergency operations are not read-only. This is not a live-readiness claim and does not add real broker calls in tests, cancellation changes, order-placement changes, frontend controls, or Kraken/crypto work.

PortfolioExecutionService now uses the Trading 212 provider for final adapter construction. `PortfolioExecutionService._get_broker` still preserves the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, reconnect-required marking with actor `portfolio_execution`, and `require_broker_environment(conn.environment, action="portfolio execution broker access")` before provider construction. The provider request uses purpose `worker_portfolio_execution`; it is user-scoped and write-capable because account/position reads and portfolio rebalance order production share the same broker helper. Tests prove no provider call in mock/no-connection/decryption-failure/policy-rejection cases, prove provider validation failure returns no broker without logging secrets or marking reconnect-required, preserve early `run_all_enabled(...)` kill-switch, auto-trading, and live-unlocked skips before broker lookup, preserve dry-run behavior and the live promotion gate, and prove rebalance order-producing paths route intent and submission through a fake `ExecutionEngine` rather than direct broker write methods. This is not a live-readiness claim and does not add frontend controls, route/schema changes, Kraken/Alpaca support, real broker calls in tests, cancellation changes, or order-placement changes.

StrategyRunner now uses the Trading 212 provider for final adapter construction. `StrategyRunner._get_broker` still preserves the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, reconnect-required marking with actor `strategy_runner`, and `require_broker_environment(conn.environment, action="strategy runner broker access")` before provider construction. The provider request uses purpose `worker_strategy_runner`; it is user-scoped and write-capable because account/position reads and strategy entry/exit order production share the same broker helper. `StrategyRunner.run_all_enabled(...)` now also skips in live mode with `skipped="live_not_unlocked"` before broker lookup when `AppSettings.live_trading_unlocked` is false, matching the safer portfolio-execution runtime pattern. Tests prove no provider call in mock/no-connection/decryption-failure/policy-rejection cases, prove provider validation failure returns no broker without logging secrets, preserve dry-run entry/exit behavior, and prove live entry/exit order-producing paths route intent and submission through a fake `ExecutionEngine` rather than direct broker write methods. This is not a live-readiness claim and does not add frontend controls, route/schema changes, Kraken/Alpaca support, real broker calls in tests, cancellation changes, or order-placement changes.

PositionMonitor now uses the Trading 212 provider for final adapter construction. `PositionMonitor._get_broker` still preserves the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, reconnect-required marking with actor `position_monitor`, and `require_broker_environment(conn.environment, action="position monitor broker access")` before provider construction. The provider request uses purpose `worker_position_monitor`; it is user-scoped and write-capable because position/account reads, automated exits, and EOD flatten order production share the same broker helper. Tests prove no provider call in mock/no-connection/decryption-failure/policy-rejection cases, prove provider validation failure returns no broker without logging secrets, preserve the current mock-mode dry-run flag for monitor-produced orders, and prove automated exit and EOD flatten paths route intent and submission through a fake `ExecutionEngine` rather than direct broker write methods. This is not a live-readiness claim and does not add frontend controls, route/schema changes, Kraken/Alpaca support, real broker calls in tests, cancellation changes, or order-placement changes.

Position-monitor daily-loss safety now uses non-dry-run closed `Trade.realized_pnl` for realised P&L instead of order cash-flow fields, applies `POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY` when unrealized P&L cannot be calculated, and commits the `eod_flatten_executed` audit record after fake/real execution-engine routing completes.

Position-monitor unrealized P&L snapshot failure now fails closed by default. `POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY` is startup-validated as one of `assume_zero`, `block_trading`, or `activate_kill_switch`; the default is `block_trading`. `assume_zero` preserves the legacy fail-open behaviour only for migration and test compatibility and is not appropriate for live automation. Runtime checks still defensively fail closed if tests or later mutation inject an invalid value. `_check_daily_loss_with_unrealized(...)` returns a typed internal outcome instead of hiding policy state on the monitor instance. `block_trading` halts monitoring without activating the global kill switch and returns `halted="unrealized_pnl_failure_block_trading"` with `failure_policy` and `fail_closed` metadata. `activate_kill_switch` calls the existing kill-switch helper, alerts operators through the existing kill-switch alert helper, and returns `halted="unrealized_pnl_failure_kill_switch"` without a second activation from `run()`. Policy halt paths log `position_monitor.realized_pnl_skipped` so incident review can distinguish a deliberate fail-closed halt from a completed realised-P&L calculation.

Demo and live credentials are separated:

- `T212_DEMO_API_KEY` / `T212_DEMO_API_SECRET`
- `T212_LIVE_API_KEY` / `T212_LIVE_API_SECRET`

No broker secrets belong in `NEXT_PUBLIC_*`.

## Kill Switch

The kill switch blocks manual orders, paper simulation, strategy orders, direct execution-engine submits, and worker-driven submit paths checked in this release pass. Activating it also disables auto-trading.

Disabling or recovering from the kill switch never re-enables auto-trading. Auto-trading requires a separate explicit enable action.

## Audit Events

High-value events are recorded in `audit_logs` and/or `order_events`, including paper order decisions, blocked orders, broker request attempts, broker failures, kill-switch actions, auto-trading changes, live-readiness actions, and daily reset manual-recovery requirements. Payloads must contain safe metadata only and never raw credentials, tokens, cookies, or auth headers.

Demo broker events use distinct action names (`demo_broker_order_attempt`, `demo_broker_order_success`, `demo_broker_order_failure`, `demo_order_blocked_by_kill_switch`) so operators can separate demo broker execution from mock/paper simulation.

## Impossible By Design

- Mock and paper flows cannot place real broker orders.
- Demo mode cannot use the live Trading 212 base URL.
- Live credentials do not make demo mode live-capable.
- Live broker calls are not enabled by default.
- Frontend broker secrets are not supported.
- Deposits and withdrawals are out of scope.
- Kill-switch recovery cannot automatically resume auto-trading.
- The generic `PATCH /v1/settings` route cannot write safety gates (`auto_trading_enabled`, `kill_switch_active`, `live_trading_unlocked`): its schema forbids unknown fields, the route checks an explicit field allowlist, and boundary tests pin both, so widening the schema without a deliberate review fails tests and the route fails closed.

## Future Work

- Preserve SystemControlService provider purposes: `operator_system_control_read` for `get_snapshot()` and `get_positions_summary()`, and `operator_system_control_emergency` for `cancel_all_pending()` and `flatten_all()`. Emergency cancel/flatten operations must stay classified as write-capable, not read-only. Future edits must also preserve `worker_cancel_timed_out_orders`, `worker_position_monitor`, `worker_strategy_runner`, and `worker_portfolio_execution` purposes, user-scoped active credential source, fake broker boundaries, risk gates, and `ExecutionEngine` order routing behavior.
- Move any remaining direct manual or experimental broker paths through the provider only after the relevant equivalence tests are in place.
- Add DB-level audit event categories/correlation IDs.
- Add request/correlation IDs consistently across all broker/audit paths.
- Add live-mode manual runbook drills before any live milestone.
