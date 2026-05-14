# Broker Interface Readiness Audit

Date: 2026-05-14

## Scope

This audit maps the current Trading 212 integration and the smallest safe path toward a future broker-neutral interface. It does not add a second broker, change broker behaviour, enable live trading, place orders, add frontend controls, or weaken existing safety gates.

Trading 212 remains the current broker adapter. Future API-native broker support should be added behind an explicit common interface only after the current pending-order and reconciliation behaviours are fully understood.

## Current Broker Integration Architecture

The main broker adapter is `Trading212Adapter` in `apps/api/app/broker/trading212.py`. It is an async HTTP adapter around Trading 212 endpoints, with an async context manager that owns an `httpx.AsyncClient`, rate-limit tracking, Trading 212 auth/API exceptions, response-shape checks, and the current `environment`/`base_url` selection.

Broker construction is still mostly Trading 212-specific:

- `apps/api/app/api/deps.py` returns `MockBrokerAdapter` in mock mode and otherwise constructs `Trading212Adapter` from either active encrypted `BrokerConnection` credentials or `T212_DEMO_API_KEY`/`T212_DEMO_API_SECRET`.
- `apps/api/app/api/v1/routes/broker.py` exposes routes under `/broker/trading212`, stores `broker="trading212"`, tests credentials by constructing `Trading212Adapter`, and serializes Trading 212 connection status.
- controlled smoke scripts under `apps/api/scripts/t212_demo_*` either call the app routes or construct `Trading212Adapter` directly for read-only reconciliation.

The runtime execution paths are more broker-like than the construction paths. `ExecutionEngine`, reconciliation services, and route dependencies generally accept an object with expected async methods and an `environment` attribute. That makes them partially ready for a protocol, but they still assume Trading 212 status values, payload fields, order quantity conventions, and environment names.

## Direct Trading212Adapter Usage

Direct construction or import appears in these important areas:

- `apps/api/app/api/deps.py`: broker dependency factory for demo/live app modes.
- `apps/api/app/api/v1/routes/broker.py`: credential connect/test and reconciliation route dependencies.
- `apps/api/app/api/v1/routes/orders.py`: imports Trading 212 exception types for broker HTTP error handling.
- `apps/api/app/execution/engine.py`: imports `make_sell_quantity`, because Trading 212 sell quantities must be negative.
- `apps/api/app/services/demo_order_reconciliation.py`: imports Trading 212 exception classes and parses Trading 212 history payloads.
- `apps/api/app/services/demo_reconciliation_scheduler.py`: background startup path constructs `Trading212Adapter` with demo credentials.
- `apps/api/app/services/position_monitor.py`, `apps/api/app/services/system_control.py`, and `apps/api/app/workers/tasks.py`: construct Trading 212 adapters for operational broker reads/writes.
- `apps/api/scripts/t212_demo_reconcile_order.py`, `apps/api/scripts/t212_demo_multi_order_reconciliation_smoke.py`, and read-only smoke scripts: construct `Trading212Adapter` directly.
- integration and unit tests monkeypatch `Trading212Adapter` methods or replace `get_broker` with fake broker-like objects.

## Read-Only Broker Methods Relied On

Current read-only expectations include:

- `environment: str`
- `test_connection() -> dict[str, Any]`
- `get_account_summary() -> dict[str, Any]`
- `get_account_metadata() -> dict[str, Any]`
- `get_instruments() -> list[dict[str, Any]]`
- `get_positions() -> list[dict[str, Any]]`
- `get_pending_orders() -> list[dict[str, Any]]`
- `get_order_by_id(order_id: str) -> dict[str, Any]`
- `get_historical_orders(cursor=None, ticker=None, limit=50) -> dict[str, Any]`

`get_historical_transactions()` exists on `Trading212Adapter`, but it is not part of the immediate reconciliation interface.

## Write-Like Broker Methods Relied On

Current write-like methods are:

- `place_market_order(ticker, quantity, time_validity="DAY")`
- `place_limit_order(ticker, quantity, limit_price, *, time_validity="DAY")`
- `place_stop_order(ticker, quantity, stop_price, *, time_validity="DAY")`
- `place_stop_limit_order(ticker, quantity, stop_price, limit_price, *, time_validity="DAY")`
- `cancel_order(order_id)`

`apps/api/app/broker/safety.py` also inventories compatibility names `modify_order`, `place_order`, and `submit_order` so smoke guards fail closed if future adapter write methods appear.

## Status And Order-History Shapes Assumed

Order submission and reconciliation currently assume Trading 212-like response keys:

- order id: `id`
- status: `status`, with known values `FILLED`, `CANCELLED`, `REJECTED`, `WORKING`, and `PENDING`
- filled quantity: `filledQuantity`
- fill price: `filledPrice`

Historical order reconciliation expects Trading 212 paginated history as `{"items": [...]}` with defensive support for `{"data": [...]}` and bare lists. Matching looks for `id`, `orderId`, `order_id`, `broker_order_id`, or nested `order.id`. Ticker extraction looks at `ticker`, `instrumentCode`, `shortName`, `order.ticker`, and `order.instrument.ticker`. Fill fields include nested Trading 212 `fill.quantity`, `fill.price`, `fill.filledAt`, and nested `order.filledQuantity`.

These shapes are adapter-specific and should eventually be normalized before application services consume them.

## Trading 212-Tied Safety Gates

Current safety gates are intentionally Trading 212-specific:

- `APP_MODE` must match broker use (`mock`, `demo`, or live-gated paths).
- `T212_ENVIRONMENT` must be `demo` for demo reconciliation and controlled smoke tests.
- `LIVE_TRADING_ENABLED` must remain false for demo reconciliation and controlled DEMO smoke flows.
- `T212_DEMO_ORDER_ENABLED` defaults demo order placement off.
- `T212_DEMO_ORDER_CONFIRM` and `T212_DEMO_MULTI_ORDER_CONFIRM` are terminal-only operator confirmations.
- `DEMO_RECONCILIATION_WORKER_ENABLED` and `DEMO_RECONCILIATION_SCHEDULER_ENABLED` gate read-only worker/scheduler behaviour.
- controlled multi-order placement requires bounded counts, small positive quantities, demo credentials, demo app mode, demo broker environment, and scheduler disabled.
- reconciliation smoke wraps the broker in `ReadOnlyBrokerGuard`, using the canonical write inventory to block write-like method names.

These gates should remain intact while broker-neutral abstractions are introduced.

## Reconciliation Assumptions Specific To Trading 212

Demo reconciliation is not a generic broker lifecycle engine yet. It assumes:

- reconciliation is Trading 212 DEMO only;
- the broker has an `environment` attribute equal to `demo`;
- local orders have `execution_environment="demo"` and `venue="t212"`;
- `get_historical_orders(limit=...)` is the source of truth for broker history;
- a local `broker_order_id` can be matched against Trading 212 history ids;
- Trading 212 historical status values map directly to local statuses;
- unknown broker statuses are non-destructive and preserve local status;
- rate limits, auth failures, and API failures are Trading 212 exception classes;
- reconciliation is read-only and must emit `no_broker_order_sent=True`.

The `venue="t212"` candidate filter is a particularly important coupling point. A second broker should not share this worker without a broker-neutral order source, venue filter, and history normalizer.

## Order Placement Assumptions Specific To Trading 212

Order placement currently assumes:

- sell quantities are converted to negative values via `make_sell_quantity`;
- Trading 212 market orders do not receive `timeValidity`, although the method signature keeps it for engine compatibility;
- limit/stop/stop-limit payload keys are `limitPrice`, `stopPrice`, and `timeValidity`;
- accepted pending statuses are `WORKING` and `PENDING`;
- response fields use Trading 212 names such as `filledQuantity` and `filledPrice`;
- manual order routes preflight risk using Trading 212 account summary shapes normalized by the account route helper;
- controlled DEMO scripts use `/v1/broker/trading212/connect` before order route calls.

These assumptions should stay in `Trading212Adapter` or a Trading 212-specific mapper when a common broker interface is introduced.

## What Should Move Behind A Broker-Neutral Interface

Good candidates for a future interface:

- broker construction through a `BrokerProvider` or registry keyed by broker id and environment;
- read-only account, positions, instruments, pending order, order lookup, and historical order methods;
- order placement and cancellation method signatures used by `ExecutionEngine`;
- normalized broker order response objects for `id`, `status`, fills, timestamps, rejection reason, and raw payload;
- normalized account summary object for risk checks;
- broker capability metadata, including supported order types, time validity semantics, fractional quantity support, and cancel support;
- broker-neutral error categories for auth, rate limit, retryable API error, validation error, and unknown failure.

The newly added `apps/api/app/broker/protocols.py` is intentionally limited to method-level protocols and a protocol write-method inventory. It documents the current surface without requiring application services to change dependencies in this PR.

## Broker-Neutral Snapshots Added

`apps/api/app/broker/snapshots.py` now defines lightweight broker-neutral `BrokerAccountSnapshot` and `BrokerOrderSnapshot` dataclasses. `apps/api/app/broker/trading212_mappers.py` maps observed Trading 212 DEMO account, pending-order, historical-order, and order-response payloads into those snapshots.

These mappers are pure transformation utilities. They do not construct brokers, call Trading 212, place orders, read or write the database, call API routes, change settings, or alter scheduler/worker behaviour. Existing Trading 212 runtime paths continue to consume their current raw payloads; wiring snapshots into reconciliation, execution, or account routes remains future work.

## What Should Stay Trading 212-Specific

These should remain adapter- or Trading 212 module-specific:

- endpoint paths, base URLs, authentication, rate-limit details, and raw HTTP errors;
- Trading 212 credential names and migration from generic `T212_API_KEY` fallback;
- `make_sell_quantity` unless a future canonical order model decides how side and signed quantity should interact;
- Trading 212 raw status names and raw response field parsing;
- controlled Trading 212 DEMO smoke scripts and QA evidence;
- `/broker/trading212` route naming until a separate broker-neutral connection API is intentionally designed;
- read-only reconciliation scripts that prove Trading 212 history behaviour.

## Risks If A Second Broker Is Added Too Early

- Different status models could silently map pending, partial fill, expired, rejected, or cancelled states incorrectly.
- A broker with native client order ids or idempotency keys may need a different duplicate-prevention model than Trading 212.
- Quantity sign, fractional quantity, notional order, extended-hours, time-in-force, and venue semantics may differ.
- Reusing the current demo reconciliation worker could skip or mutate the wrong orders because it filters `venue="t212"` and parses Trading 212 history shapes.
- Safety gates named `T212_*` may give false confidence for another broker unless broker-neutral and broker-specific gates are separated.
- Exception handling may leak or misclassify failures if another adapter does not raise Trading 212 exception classes.
- Operator QA evidence for pending order behaviour may not transfer to another broker.

## Recommended Phased Migration Plan

1. Keep this PR as audit plus protocol scaffolding only.
2. Add a broker-neutral `BrokerOrderSnapshot` and `BrokerAccountSnapshot` model in a follow-up PR, with Trading 212 mapping tests based on existing DEMO QA examples.
3. Update `ExecutionEngine` type hints to depend on `OrderPlacementBrokerProtocol` without changing runtime construction.
4. Update reconciliation service type hints to depend on `ReadOnlyBrokerProtocol`, then isolate Trading 212 history parsing into a dedicated mapper.
5. Introduce a broker registry/factory that can return the existing `Trading212Adapter` by broker id while preserving `/broker/trading212` behaviour.
6. Only after those seams are tested, design a second adapter spike using recorded/non-live fixtures. Do not add live trading or strategy-driven broker writes as part of that spike.

## Next Recommended PR

Add broker-neutral snapshot dataclasses and Trading 212 mapper tests for account summary, order placement responses, pending order responses, and historical order records. The PR should keep runtime behaviour unchanged and should use existing Trading 212 DEMO evidence as fixtures where possible.
