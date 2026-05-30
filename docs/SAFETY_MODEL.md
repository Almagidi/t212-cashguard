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

Remaining direct Trading 212 construction paths are inventoried and locked by a unit test. Read-only account sync, CFD funding tracking, and pending-order reconciliation now use the provider only for final adapter construction, after worker-owned active connection lookup, credential decryption, reconnect-required handling, and environment gates. Account sync calls only `get_account_summary()` before persisting a local snapshot; CFD funding calls only `get_positions()` before persisting local funding records; pending-order reconciliation still hands the provider-created broker to `ExecutionEngine.reconcile_order(...)` for the same selected orders.

Focused order-worker provider-equivalence tests now lock `reconcile_pending_orders` as provider-backed for final adapter construction only, and `cancel_timed_out_orders` as still direct and deferred. They prove these workers skip adapter construction in unsafe states, preserve both demo and live reconcile behaviour from the previous direct path, use active encrypted connection credentials for the current runtime mode, and hand the constructed broker only to a fake `ExecutionEngine` in tests. Live-disabled mismatch remains a policy-layer rejection through `require_broker_environment(...)`, not a separate worker-owned gate.

Order submission, cancellation, strategy execution, position monitoring, portfolio execution, and emergency system-control paths remain direct and deferred so provider work does not expand broker write reach. A source-level write-capable provider-boundary audit now locks this state without executing broker code: `cancel_timed_out_orders` remains direct/provider-unwired and cancellation-capable through `ExecutionEngine.cancel_order(...)`; `position_monitor` remains write-capable for automated exits and EOD flatten; `strategy_runner` remains mixed/write-capable for strategy entries and exits; `portfolio_execution_service` remains mixed/write-capable for rebalance orders; and `system_control` remains mixed/write-capable because read-only status calls and emergency cancel/flatten share one broker helper. Manual smoke scripts remain terminal-only/manual DEMO tools and are not production provider migration targets. This PR does not change live trading, order placement, cancellation behaviour, credential storage/decryption, route schemas, or frontend controls.

System-control provider-equivalence coverage is tests/docs-only. `SystemControlService._get_broker` still directly constructs `Trading212Adapter` after the existing mock-mode shortcut, app-mode policy gate, active encrypted connection lookup, connection-environment gate, and credential decryption. Read/status methods are proven read-only against fakes, while emergency cancel-all and flatten-all are proven write-capable only through fake broker and fake execution-engine records. No runtime provider migration, live-readiness claim, real broker call, network call, cancellation, or order placement is added.

Portfolio-execution provider-equivalence coverage is tests/docs-only. `PortfolioExecutionService._get_broker` still directly constructs `Trading212Adapter` after the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, and connection-environment gate. `require_broker_environment(...)` currently runs after credential decryption and before direct adapter construction; policy rejection returns no broker before construction. The service remains direct/provider-unwired and mixed/write-capable because account/position reads and rebalance order-producing behavior share the same broker helper. Tests prove early `run_all_enabled(...)` kill-switch, auto-trading, and live-unlocked skips happen before broker lookup, prove rebalance orders are routed through fake `ExecutionEngine` intent/submission calls only, preserve dry-run behavior and the live promotion gate, and make no real broker call, network call, cancellation, or order placement. This is not a live-readiness claim and does not migrate runtime construction.

Strategy-runner provider-equivalence coverage is tests/docs-only. `StrategyRunner._get_broker` still directly constructs `Trading212Adapter` after the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, and connection-environment gate. `require_broker_environment(...)` currently runs after credential decryption and before direct adapter construction; policy rejection returns no broker before construction. The runner remains direct/provider-unwired and mixed/write-capable because account/position reads and strategy entry/exit order production share the same broker helper. Tests prove early kill-switch and auto-trading skips happen before broker lookup, prove account/position reads occur through fake brokers only, preserve dry-run entry/exit behavior, and prove live entry/exit order-producing paths route intent and submission through a fake `ExecutionEngine` rather than direct broker write methods. No runtime provider migration, live-readiness claim, real broker call, network call, cancellation, or order placement is added. Future provider migration must preserve dry-run/live behavior, kill switch and auto-trading gates, strategy enabled, venue, promotion, allocation, and risk gates, active encrypted credential source, provider purpose, fake broker boundary, and `ExecutionEngine` order submission behavior.

Position-monitor provider-equivalence coverage is tests/docs-only. `PositionMonitor._get_broker` still directly constructs `Trading212Adapter` after the existing mock-mode shortcut, active encrypted connection lookup, credential decryption, and connection-environment gate. `require_broker_environment(...)` currently runs after credential decryption and before direct adapter construction; policy rejection returns no broker before construction. The monitor remains direct/provider-unwired and write-capable because position/account reads, automated exits, and EOD flatten order production share the same broker helper. Tests prove early kill-switch and auto-trading skips happen before broker lookup, prove account/position reads occur through fake brokers only, preserve the current mock-mode dry-run flag for monitor-produced orders, and prove automated exit and EOD flatten paths route intent and submission through a fake `ExecutionEngine` rather than direct broker write methods. No runtime provider migration, live-readiness claim, real broker call, network call, cancellation, or order placement is added. Future provider migration must preserve dry-run/live behavior, kill switch, auto-trading and monitor gates, active encrypted credential source, provider purpose, fake broker boundary, daily-loss/risk gates, and `ExecutionEngine` order submission behavior.

Position-monitor daily-loss safety now uses non-dry-run closed `Trade.realized_pnl` for realised P&L instead of order cash-flow fields, logs `position_monitor.unrealized_pnl_error` when unrealized P&L cannot be calculated, and commits the `eod_flatten_executed` audit record after fake/real execution-engine routing completes.

Position-monitor unrealized P&L snapshot failure is a remaining fail-open risk. The current daily-loss path logs `position_monitor.unrealized_pnl_error` and assumes `unrealized = 0.0` if `broker.get_positions()` or unrealized P&L calculation fails, so realised P&L alone can allow the check to pass. `docs/architecture/position-monitor-unrealized-pnl-failure-policy.md` documents this current behaviour and defines fail-closed, kill-switch, and configurable-policy options that should be decided before live automation.

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

## Future Work

- Add a tests-only/equivalence PR for exactly one remaining direct provider candidate before any runtime construction migration. Do not migrate write-capable paths until unchanged safety gates, credential source, provider request purpose, fake broker boundary, and order behavior are proven. For system control, split read-only status from emergency-write paths or give them separate provider purposes and tests before changing runtime construction. For portfolio execution, the current equivalence coverage locks construction paths, early `run_all_enabled(...)` safety gates, account/position reads, dry-run order routing, and the live promotion gate. Before provider wiring, add or retain focused coverage for allocation and risk blocker paths, then preserve credential source, provider purpose, fake broker boundary, and `ExecutionEngine` order submission behavior. For strategy runner, the current equivalence coverage locks direct construction, no-broker/decryption/environment skips, account/position reads, dry-run entry/exit behavior, and fake-engine entry/exit submission routing; before runtime migration, add or retain focused coverage for venue, promotion, allocation, and risk blocker combinations. For position monitor, the current equivalence coverage locks direct construction, no-broker/decryption/environment skips, account/position reads, mock-mode dry-run flags, and fake-engine automated exit/EOD flatten submission; before runtime migration, add or retain focused coverage for daily-loss, monitor-enabled, live-gate, and risk combinations.
- Move remaining broker workers or services through the provider only after the relevant equivalence tests are in place.
- Add DB-level audit event categories/correlation IDs.
- Add request/correlation IDs consistently across all broker/audit paths.
- Add live-mode manual runbook drills before any live milestone.
