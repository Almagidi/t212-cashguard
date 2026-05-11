# Safety Model

T212 CashGuard Trader is local-first and cash-only. The backend is the safety boundary; frontend controls only mirror backend state and are never the only guard.

## Runtime Modes

- **Mock mode** is the default. It uses local/mock data and must not call Trading 212 demo or live endpoints.
- **Paper mode** is local paper execution inside mock mode through `/v1/orders/paper`. It records local orders, simulates fills, and always marks `no_broker_order_sent=true`.
- **Demo mode** may use Trading 212 demo endpoints only when `APP_MODE=demo`, demo credentials are configured, and the user explicitly selects demo mode.
- **Live mode** is guarded for future use. It requires `APP_MODE=live`, `LIVE_TRADING_ENABLED=true`, a live broker connection, recent broker test, Telegram supervision, demo validation, kill-switch drill evidence, kill switch clear, and final admin unlock.

## Broker Boundary

Broker access is centralized through the backend safety policy. Mock mode blocks real broker environments. Demo mode blocks live environments. Live mode blocks broker calls unless the live flag is enabled and live readiness passes before order submission.

Demo and live credentials are separated:

- `T212_DEMO_API_KEY` / `T212_DEMO_API_SECRET`
- `T212_LIVE_API_KEY` / `T212_LIVE_API_SECRET`

No broker secrets belong in `NEXT_PUBLIC_*`.

## Kill Switch

The kill switch blocks manual orders, paper simulation, strategy orders, direct execution-engine submits, and worker-driven submit paths checked in this release pass. Activating it also disables auto-trading.

Disabling or recovering from the kill switch never re-enables auto-trading. Auto-trading requires a separate explicit enable action.

## Audit Events

High-value events are recorded in `audit_logs` and/or `order_events`, including paper order decisions, blocked orders, broker request attempts, broker failures, kill-switch actions, auto-trading changes, live-readiness actions, and daily reset manual-recovery requirements. Payloads must contain safe metadata only and never raw credentials, tokens, cookies, or auth headers.

## Impossible By Design

- Mock and paper flows cannot place real broker orders.
- Live broker calls are not enabled by default.
- Frontend broker secrets are not supported.
- Deposits and withdrawals are out of scope.
- Kill-switch recovery cannot automatically resume auto-trading.

## Future Work

- Move all remaining read-only broker workers through one broker factory.
- Add DB-level audit event categories/correlation IDs.
- Add full demo execution RC coverage before enabling demo broker order placement.
- Add live-mode manual runbook drills before any live milestone.
