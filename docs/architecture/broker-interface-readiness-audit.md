# Broker Interface Readiness Audit

Date: 2026-05-14

## Scope

This audit maps the current Trading 212 integration and the smallest safe path toward a future broker-neutral interface. It does not add a second broker, change broker behaviour, enable live trading, place orders, add frontend controls, or weaken existing safety gates.

Trading 212 remains the current broker adapter. Future API-native broker support should be added behind an explicit common interface only after the current pending-order and reconciliation behaviours are fully understood.

## Current Broker Integration Architecture

The main broker adapter is `Trading212Adapter` in `apps/api/app/broker/trading212.py`. It is an async HTTP adapter around Trading 212 endpoints, with an async context manager that owns an `httpx.AsyncClient`, rate-limit tracking, Trading 212 auth/API exceptions, response-shape checks, and the current `environment`/`base_url` selection.

Broker construction is still partly Trading 212-specific:

- `apps/api/app/api/deps.py` returns `MockBrokerAdapter` in mock mode and otherwise delegates final adapter construction to the Trading 212 provider after selecting active encrypted `BrokerConnection` credentials or demo fallback credentials.
- `apps/api/app/api/v1/routes/broker.py` exposes routes under `/broker/trading212`, stores `broker="trading212"`, tests credentials through the Trading 212 provider, and serializes Trading 212 connection status.
- scheduler startup, the terminal one-shot demo reconciliation worker, `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders` delegate final adapter construction to the provider after their existing gates.
- service helpers, selected worker tasks, and controlled smoke scripts still construct `Trading212Adapter` directly as inventoried below.

The runtime execution paths are more broker-like than the construction paths. `ExecutionEngine`, reconciliation services, and route dependencies generally accept an object with expected async methods and an `environment` attribute. That makes them partially ready for a protocol, but they still assume Trading 212 status values, payload fields, order quantity conventions, and environment names.

## Direct Trading212Adapter Usage

Direct construction or import appears in these important areas:

- `apps/api/app/broker/provider.py`: canonical Trading 212 provider construction after caller-owned credential and safety decisions.
- `apps/api/app/api/v1/routes/orders.py`: imports Trading 212 exception types for broker HTTP error handling.
- `apps/api/app/execution/engine.py`: imports `make_sell_quantity`, because Trading 212 sell quantities must be negative.
- `apps/api/app/services/demo_order_reconciliation.py`: imports Trading 212 exception classes and parses Trading 212 history payloads.
- `apps/api/app/services/position_monitor.py`, `apps/api/app/services/system_control.py`, `apps/api/app/services/strategy_runner.py`, `apps/api/app/services/portfolio_execution_service.py`, and selected functions in `apps/api/app/workers/tasks.py`: construct Trading 212 adapters for operational broker reads/writes.
- `apps/api/scripts/t212_demo_reconcile_order.py`, `apps/api/scripts/t212_demo_multi_order_reconciliation_smoke.py`, and `apps/api/scripts/t212_demo_readonly_smoke.py`: construct `Trading212Adapter` directly under terminal-only DEMO gates.
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

Demo reconciliation now performs that Trading 212 historical-order parsing through the broker-neutral `BrokerOrderSnapshot` mapper surface, keeping the service read-only and preserving the existing status mapping and audit behaviour.

Demo reconciliation now targets a narrow `ReconciliationHistoryBrokerProtocol` containing only broker environment metadata and broker-history read access through `get_historical_orders(...)`.

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

The newly added `apps/api/app/broker/protocols.py` is intentionally limited to method-level protocols and a protocol write-method inventory. It documents the current surface without requiring runtime broker construction to change.

Demo reconciliation now type-targets `ReconciliationHistoryBrokerProtocol` at the service, worker, and scheduler boundaries. Scheduler startup and the terminal one-shot worker delegate final adapter construction to the provider, while remaining reconciliation smoke scripts stay Trading 212-specific. The broader `ReadOnlyBrokerProtocol` remains available for account, order, and wider broker-read paths.

`docs/architecture/broker-provider-design.md` now documents the next broker-agnostic architecture step: a future provider boundary that can return the existing `Trading212Adapter` by broker id and environment without changing current Trading 212 routes, credential handling, demo reconciliation, or write safety gates.

`apps/api/app/broker/provider.py` now contains provider request scaffolding and fail-closed validation for that future boundary.

It also contains a Trading 212 provider function that requires explicit credentials and constructs `Trading212Adapter` only after provider and credential validation pass.

`get_broker()` now uses that provider function only for final Trading 212 adapter construction. Active encrypted credential lookup, demo fallback selection, credential decryption, reconnect-required handling, route behaviour, and live safety gates remain in the existing dependency path.

`apps/api/tests/integration/test_get_broker_provider_equivalence.py` now locks the current `get_broker()` behaviour during provider wiring: mock-mode selection, active encrypted credential precedence, demo fallback credentials, live flag blocking, invalid runtime-mode safety errors, credential decryption failure handling, and proof that fallback construction reaches the provider with the selected request and credentials.

`/v1/broker/trading212/connect` and `/v1/broker/trading212/test` now also use the provider function only for final adapter construction during credential tests. The route layer still owns submitted credential handling, active connection lookup, encryption/decryption, reconnect-required handling, schemas, and audit behaviour.

`apps/api/tests/integration/test_scheduler_worker_provider_equivalence.py` now locks the migrated scheduler and worker construction paths. It proves the background scheduler calls the provider only after demo-only gates pass, preserves the current demo credential fallback order, refuses unsafe mock/paper/live and live-flag states before provider construction, and does not use live credentials. It also documents that the service worker receives a broker object while the terminal demo worker script now delegates final demo adapter construction to the provider under its own environment gates.

## Remaining Trading 212 Direct Construction Audit

Date: 2026-05-19

This audit covers the remaining direct `Trading212Adapter` construction/import paths after the provider migrations. The latest update is tests/docs-only: it classifies and locks the remaining write-capable or mixed direct paths without migrating runtime code. It does not change routes or schemas, change credential storage/decryption, place broker orders, cancel broker orders, expand live trading, add Alpaca, add frontend order controls, or weaken safety gates.

`apps/api/tests/unit/test_trading212_construction_inventory.py` now locks the approved runtime inventory. The test scans `apps/api/app` and `apps/api/scripts`, excludes tests/docs and the adapter definition itself, and fails if a new runtime `Trading212Adapter` import or constructor call appears without updating this audit.

`apps/api/tests/unit/test_write_capable_provider_boundary_audit.py` adds the semantic boundary lock for the remaining direct paths. It proves `cancel_timed_out_orders` is still direct/provider-unwired and cancellation-capable, proves provider usage in `apps/api/app/workers/tasks.py` remains limited to `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders`, and classifies `position_monitor`, `strategy_runner`, `portfolio_execution_service`, and `system_control` by their write-capable surfaces. The test is source/AST-level only and does not execute broker code.

`apps/api/scripts/t212_demo_reconciliation_worker.py` was migrated in the previous provider PR and is therefore intentionally absent from the remaining direct construction inventory.

### Classification Summary

| Classification | Direct runtime references |
| --- | --- |
| Runtime production app paths | `app/broker/provider.py`; `app/services/portfolio_execution_service.py`; `app/services/position_monitor.py`; `app/services/strategy_runner.py`; `app/services/system_control.py`; `app/workers/tasks.py` |
| Runtime scripts/smoke tools | `scripts/t212_demo_readonly_smoke.py`; `scripts/t212_demo_reconcile_order.py`; `scripts/t212_demo_multi_order_reconciliation_smoke.py` |
| Tests/fakes/monkeypatching | Unit/integration tests import, instantiate, or monkeypatch `Trading212Adapter` for safety-policy, provider-equivalence, route-boundary, paper-execution, demo-boundary, and inventory tests. These are not runtime construction paths. |
| Docs | This audit, the provider design, and the safety model mention `Trading212Adapter` to document current boundaries and future migration sequencing. |

### Runtime Production App Inventory

| Path | Function/class | Direct use | Purpose | Current safety gates | Credential source | Read/write capability | Migration timing | Incorrect-migration risk | Recommended acceptance tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `apps/api/app/broker/provider.py` | `create_trading212_provider_adapter(...)` | Imports and constructs the adapter after provider request and credential validation. It also has a type-checking import. | Canonical final Trading 212 construction point for migrated callers. | `validate_broker_provider_request(...)` rejects unsupported broker ids/environments/purposes, mock/paper modes, demo-to-live, live-to-demo, and live when `LIVE_TRADING_ENABLED` is false; credential validation rejects blank secrets before construction. | Explicit `BrokerProviderCredentials` supplied by caller; no DB, env, or decryption lookup inside provider. | Returns the broad adapter, but the provider itself performs no broker read/write. | Never migrate away until a real provider registry replaces it. This is the intended construction boundary. | Moving credential lookup or live gating into a generic shortcut could change credential precedence or permit live construction too early. | Existing provider scaffolding tests plus provider-equivalence tests for each migrated caller. |
| `apps/api/app/workers/tasks.py` | `sync_account_snapshot` | Delegates final adapter construction to `create_trading212_provider_adapter(...)`. | Scheduled account snapshot persistence from broker account summary. | Mock mode uses `MockBrokerAdapter`; real path requires active connection for `settings.APP_MODE`, `require_broker_environment(conn.environment, action="worker account sync")`, provider credential/environment gates, and credential-decryption failure marks reconnect required. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`; worker code still owns lookup and decryption. | Read-only: calls `get_account_summary()` and writes a local snapshot row. | Done. | Future edits could still accidentally change connection selection, mock persistence behavior, reconnect-required marking, or construct before the environment gate. | Provider called only after mock/no-connection/decryption/environment gates; request purpose is `worker_account_sync` with active connection user id; no provider call in mock/paper/invalid/live-disabled mismatch; snapshot output unchanged with fake adapter; no write-like broker methods called. |
| `apps/api/app/workers/tasks.py` | `reconcile_pending_orders` | Delegates final adapter construction to `create_trading212_provider_adapter(...)`. | Reconciles accepted/submitted local orders by polling broker order status through `ExecutionEngine.reconcile_order(...)`. | Task lock; skips mock; requires non-dry-run orders with broker ids; active connection for current app mode; `require_broker_environment(conn.environment, action="worker reconcile")`; decryption failure marks reconnect required; provider credential/environment gates. Provider validation preserves the previous direct path's demo and live reconcile behaviour. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`; worker code still owns lookup and decryption. | Read-only in current engine flow because reconciliation calls `get_order_by_id(...)`, but it passes a broad adapter into `ExecutionEngine`. | Done. | Future edits could accidentally broaden order selection, construct before worker-owned gates, make reconcile live-only, or move reconciliation/persistence into the provider. | Provider request purpose is `worker_reconcile`; provider receives active connection user id and decrypted credentials; demo and live golden paths are covered; no provider call in mock/no-orders/no-connection/decryption failure/unsafe environment; provider-created fake broker is passed to `ExecutionEngine`; no broker writes invoked. |
| `apps/api/app/workers/tasks.py` | `cancel_timed_out_orders` | Local import and direct `async with Trading212Adapter(...)`. | Cancels stale accepted/submitted limit/stop/stop-limit orders through `ExecutionEngine.cancel_order(...)`. | Selects only non-dry-run timed-out orders with broker ids; skips mock/no connection; `require_broker_environment(conn.environment, action="worker timeout cancel")`; decryption failure marks reconnect required; adapter gates. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`. | Write-capable: can call broker `cancel_order(...)`. | Deferred. Do not migrate until a dedicated cancellation-equivalence PR proves unchanged order selection, provider purpose, credential source, environment gates, and cancellation behavior. | A wrong provider purpose or premature construction could expand cancellation reach or bypass timeout/order filters. | No provider call in any current tests; direct construction happens only after timed-out candidate selection, active connection lookup, environment gate, and credential decryption; cancellation count/status behavior unchanged; no real broker/network calls. |
| `apps/api/app/workers/tasks.py` | `track_cfd_funding` | Delegates final adapter construction to `create_trading212_provider_adapter(...)`. | Reads open broker positions and records CFD funding costs locally. | Mock uses `MockBrokerAdapter`; real path requires active connection, credential decryption, `require_broker_environment(conn.environment, action="worker cfd funding")`, provider credential/environment gates, and provider validation failure summarization. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`; worker code still owns lookup and decryption. | Read-only: calls `get_positions()` and writes local funding records. | Done. | Future edits could accidentally construct before mock/no-connection/decryption/environment gates or change funding-record input shape. | Provider request purpose is `worker_cfd_funding`; no provider call in mock/no-connection/decryption failure/unsafe environment; provider receives active connection user id and decrypted credentials; positions-to-records behavior unchanged; no write-like broker methods called. |
| `apps/api/app/services/system_control.py` | `SystemControlService._get_broker` | Local import and direct construction. | Shared helper for operator/system status, positions, emergency cancel-all, and emergency flatten-all. | Mock returns mock adapter; `require_broker_environment(settings.APP_MODE, action="system control broker access")`; active connection optionally scoped by `broker_user_id`; `require_broker_environment(conn.environment, ...)`; decryption failure marks reconnect required and commits; adapter gates. | Active encrypted `BrokerConnection`, optionally user scoped. | Mixed: read-only `get_snapshot()`/`get_positions_summary()` and write-capable `cancel_all_pending()`/`flatten_all()`. | Later, probably after splitting or separately testing read-only and emergency-write use cases. Not a safe first target because one helper serves both status reads and emergency writes. | Migrating the shared helper as one broad path could accidentally grant write-capable adapters to read-only status flows or alter emergency controls. | Separate tests for read-only snapshot provider purpose and emergency cancel/flatten provider purpose; unchanged reconnect-required commit behavior; no provider call before system-control environment gates. |
| `apps/api/app/services/portfolio_execution_service.py` | `PortfolioExecutionService._get_broker` | Local import and direct construction. | Reads account/positions for portfolio rebalance and can submit portfolio rebalance orders through `ExecutionEngine`. | Mock returns mock adapter; active connection for `settings.APP_MODE`; decryption failure marks reconnect required; `require_broker_environment(conn.environment, action="portfolio execution broker access")`; higher-level kill switch, auto-trading, live unlock, strategy promotion, risk, and allocation gates before submission. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`. | Mixed/write-capable: account/position reads plus possible order submission. | Later. Not a safe first target because rebalance planning, dry-run behavior, live unlock, promotion gates, allocation gates, and order submission share the same broker helper. | A wrong migration could construct before live unlock/promotion checks are considered, or blur dry-run versus real submission behavior. | Provider not called when no broker/credential/environment blocks; live and dry-run order behavior unchanged; order submission still passes through execution safety gates. |
| `apps/api/app/services/position_monitor.py` | `PositionMonitor._get_broker` | Local import and direct construction. | Monitors positions and submits automated exits or EOD flatten orders. | Mock returns mock adapter; active connection for `settings.APP_MODE`; decryption failure marks reconnect required; `require_broker_environment(conn.environment, action="position monitor broker access")`; higher-level app settings and strategy/risk checks in monitor flow. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`. | Write-capable: can create and submit exit/flatten orders. | Later. Not a safe first target because it is an automated exit engine and EOD flatten path. | Premature migration could expand automated exit write access or weaken position/environment checks. | Provider called only after monitor gates; no provider call in mock/no connection/decryption failure; exit and EOD flatten tests prove no change to order intent/submission gates. |
| `apps/api/app/services/strategy_runner.py` | `StrategyRunner._get_broker` | Local import and direct construction. | Reads account/positions and can submit strategy orders through `ExecutionEngine`. | Mock returns mock adapter; active connection for `settings.APP_MODE`; decryption failure marks reconnect required; `require_broker_environment(conn.environment, action="strategy runner broker access")`; higher-level auto-trading, kill switch, strategy state, and risk checks before submission. | Active encrypted `BrokerConnection` for current `settings.APP_MODE`. | Mixed/write-capable. | Later. Not a safe first target because entry and exit submission, dry-run behavior, strategy gates, venue gates, and risk gates must be proven equivalent together. | A generic provider path could make strategy writes easier to reach before the strategy/live gates are proven equivalent. | Provider-equivalence tests for account/position reads, no-broker/decryption/environment skips, and unchanged dry-run/live submit behavior. |

### Runtime Scripts And Smoke Tools

| Path | Terminal/manual | Demo/live constraints | Can place/cancel/modify orders? | Credential source | Migration order |
| --- | --- | --- | --- | --- | --- |
| `apps/api/scripts/t212_demo_readonly_smoke.py` | Manual terminal smoke. | Requires `APP_MODE=demo`, `T212_ENVIRONMENT=demo`, and `LIVE_TRADING_ENABLED=false`. | No. It calls `test_connection()`, `get_account_summary()`, `get_positions()`, and a direct GET history request. | `T212_API_KEY` / `T212_API_SECRET`. | After read-only runtime workers. It is already manual, demo-only, and read-only. |
| `apps/api/scripts/t212_demo_reconcile_order.py` | Manual terminal reconciliation. | Requires `APP_MODE=demo`, `T212_ENVIRONMENT=demo`, `LIVE_TRADING_ENABLED=false`, and one explicit local order id or broker order id. | No broker writes. It reconciles from `GET /api/v0/equity/history/orders` and writes local DB reconciliation/audit state. | `T212_API_KEY` / `T212_API_SECRET`. | After read-only runtime workers. Preserve explicit terminal inputs and local DB behavior. |
| `apps/api/scripts/t212_demo_multi_order_reconciliation_smoke.py` | Manual terminal smoke. | Requires `APP_MODE=demo`, `T212_ENVIRONMENT=demo`, `LIVE_TRADING_ENABLED=false`, `DEMO_RECONCILIATION_WORKER_ENABLED=true`, and `DEMO_RECONCILIATION_SCHEDULER_ENABLED=false`. | No expected broker writes. `ReadOnlyBrokerGuard` blocks inventoried write methods if called. | Prefers `T212_DEMO_API_KEY` / `T212_DEMO_API_SECRET`; falls back to `T212_API_KEY` / `T212_API_SECRET`. | After read-only runtime workers. Keep the write guard and scheduler-disabled constraint. |

These scripts are terminal-only/manual QA tools and should remain separate from production runtime migration decisions. They are manually gated, constrained to DEMO, and valuable as broker-specific evidence. They may remain Trading 212-specific longer than application workers, and they should not drive provider migration sequencing for production paths.

### Test References

Tests import or monkeypatch `Trading212Adapter` for bounded reasons:

- provider-equivalence tests replace `app.broker.trading212.Trading212Adapter` with recording fakes to prove provider request data and credential precedence without network calls;
- safety-policy and demo-boundary tests instantiate the adapter with fake credentials to prove constructor gates and method-level protections;
- paper-execution, route, operator-status, and heartbeat tests monkeypatch adapter methods so broker writes cannot escape during tests;
- `test_broker_safety_inventory.py` introspects the adapter to keep the write-method inventory aligned with real adapter methods;
- `test_trading212_construction_inventory.py` scans runtime source and does not treat test fake usage as runtime construction.

These references are intentionally separate from runtime production and script inventories.

### Latest Provider Migration

`sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders` in `apps/api/app/workers/tasks.py` now use `create_trading212_provider_adapter(...)` only for final Trading 212 adapter construction. Account sync calls only `get_account_summary()`, CFD funding calls only `get_positions()`, and pending-order reconciliation still delegates each selected order to `ExecutionEngine.reconcile_order(...)`.

Active connection lookup, credential decryption, reconnect-required handling, `require_broker_environment(...)`, provider-validation failure summarization, order selection, `ExecutionEngine` reconciliation, task summaries, `BrokerAccountSnapshot` persistence, and local CFD funding record persistence remain in worker code. The provider still does not query the database, decrypt credentials, read settings, choose credentials, select orders, reconcile orders, persist snapshots or funding records, audit worker events, or place/cancel/modify orders.

Write-capable execution paths should remain deferred. `position_monitor`, `strategy_runner`, `portfolio_execution_service`, `system_control` emergency actions, and `cancel_timed_out_orders` can submit or cancel orders after their own domain gates. Moving those broad helpers too early would make it harder to prove the provider migration did not change order placement, cancellation, dry-run behavior, live unlock semantics, risk gates, or kill-switch coverage.

Read-only worker migrations now lock these acceptance tests:

- provider is not called in mock mode, no-connection, credential-decryption failure, unsafe environment, or live-disabled mismatch cases;
- provider receives `broker_id="trading212"`, the active connection environment, caller-specific purpose (`worker_account_sync`, `worker_cfd_funding`, or `worker_reconcile`), and the active connection user id;
- provider receives only decrypted active connection credentials;
- `BrokerAccountSnapshot`, local CFD funding persistence, selected-order reconciliation, and task summaries remain unchanged;
- no placement, cancellation, modification, deposit, or withdrawal method is called.

`apps/api/tests/unit/test_order_worker_provider_equivalence.py` now locks `reconcile_pending_orders` as provider-backed for final Trading 212 adapter construction only in both demo and live modes, while `cancel_timed_out_orders` remains direct. The runtime direct construction count in `apps/api/app/workers/tasks.py` is now `{"construct": 1, "import": 1}` for `cancel_timed_out_orders`.

The order-worker tests prove unsafe states return skipped summaries before provider/direct adapter construction: mock mode, no eligible orders, no active connection, credential-decryption failure with reconnect-required marking, policy rejection, and live-disabled mismatch. They also prove active encrypted `BrokerConnection` credentials for the current `settings.APP_MODE` are used, the constructed broker is handed to `ExecutionEngine`, and the engine receives exactly the selected orders.

`cancel_timed_out_orders` remains write-capable through `ExecutionEngine.cancel_order(...)` and must stay deferred until a provider migration can prove unchanged cancellation behaviour. This PR does not change live trading, order placement, cancellation behaviour, credential storage/decryption, route schemas, or frontend controls.

### Current Write-Capable Boundary Lock

As of 2026-05-29, the provider-backed production construction paths are `get_broker()`, `/v1/broker/trading212` credential-test construction, scheduler startup, the terminal one-shot demo reconciliation worker, `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders`.

The remaining direct runtime paths are deliberately classified before further migration:

- `cancel_timed_out_orders`: write-capable cancellation path; direct/provider-unwired; uses `ExecutionEngine.cancel_order(...)`.
- `position_monitor`: write-capable automated exit and EOD flatten path; direct/provider-unwired.
- `strategy_runner`: mixed/write-capable strategy entry and exit submission path; direct/provider-unwired.
- `portfolio_execution_service`: mixed/write-capable portfolio rebalance submission path; direct/provider-unwired.
- `system_control`: mixed read/status plus emergency cancel/flatten path; direct/provider-unwired.
- manual smoke scripts: terminal-only/manual DEMO tools; direct/provider-unwired and not production provider migration targets.

The next recommended provider step, if the migration continues, is another tests-only/equivalence PR for exactly one candidate. That PR should prove the selected path's existing safety gates, credential source, direct/provider-unwired baseline, provider request purpose, fake broker boundary, and unchanged read/write behavior before any runtime construction changes are made.

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
4. Add type-only broker provider scaffolding and tests for fail-closed provider request validation.
5. Done: introduce a Trading 212 provider and wire `get_broker()` final construction only after its safety gates and credential handling are specified and tested.
6. Done: move `/v1/broker/trading212` credential-test construction to the provider while preserving route names, schemas, credential handling, and audit behaviour.
7. Done: add focused scheduler/worker provider-equivalence tests before migrating their construction points.
8. Done: migrate scheduler and terminal worker construction to the Trading 212 provider while preserving the newly documented demo-only gates, credential source rules, and read-only reconciliation boundary.
9. Done: audit the remaining direct construction paths and choose `sync_account_snapshot` as the next narrow read-only migration target.
10. Done: migrate `sync_account_snapshot` final adapter construction to the Trading 212 provider while preserving worker-owned connection lookup, credential decryption, environment gates, snapshot persistence, and read-only broker use.
11. Done: migrate `track_cfd_funding` final adapter construction to the Trading 212 provider while preserving worker-owned connection lookup, credential decryption, environment gates, local funding persistence, and read-only broker use.
12. Done: migrate `reconcile_pending_orders` final adapter construction to the Trading 212 provider while preserving worker-owned order selection, active connection lookup, credential decryption, environment gates, reconnect-required handling, `ExecutionEngine.reconcile_order(...)`, and summary behaviour.
13. Before migrating any remaining direct write-capable or mixed path, add a tests-only/equivalence PR for exactly one chosen candidate. The PR should lock existing safety gates, credential source, direct/provider-unwired baseline, provider request purpose, fake broker boundary, and unchanged read/write behavior.
14. Only after the remaining direct construction paths are migrated where appropriate, design a second adapter spike using recorded/non-live fixtures. Do not add live trading or strategy-driven broker writes as part of that spike.

## Next Recommended PR

Keep write-capable paths deferred. The next PR should be tests-only/equivalence for exactly one candidate, not a runtime migration. It should not recommend migrating a write-capable path until the tests prove existing safety gates, credential source, broker write boundary, and unchanged order behavior. `cancel_timed_out_orders` should remain deferred because it can cancel broker orders through `ExecutionEngine.cancel_order(...)`.
