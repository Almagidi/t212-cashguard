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

- Move all remaining read-only broker workers through one broker factory.
- Add DB-level audit event categories/correlation IDs.
- Add request/correlation IDs consistently across all broker/audit paths.
- Add live-mode manual runbook drills before any live milestone.
