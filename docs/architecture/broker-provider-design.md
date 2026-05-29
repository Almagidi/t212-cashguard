# Broker Provider Design

Date: 2026-05-15

## Scope

This document designs a broker provider boundary for returning the existing `Trading212Adapter` by broker id and environment. It does not add a second broker, change `/v1/broker/trading212` routes, change credential handling, enable live trading, place orders, add frontend controls, or weaken safety gates.

The current supported real broker remains Trading 212. The purpose of this design is to make the next runtime migration boring: first document the target shape, then add a small provider behind tests, then move call sites one at a time.

## Current Direct Construction Points

The provider migration now covers `get_broker()`, `/v1/broker/trading212` credential-test construction, scheduler startup construction, the terminal one-shot demo reconciliation worker, `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders`. The remaining direct `Trading212Adapter` construction/import inventory is locked by `apps/api/tests/unit/test_trading212_construction_inventory.py` and detailed in `docs/architecture/broker-interface-readiness-audit.md`.

`apps/api/tests/unit/test_write_capable_provider_boundary_audit.py` now adds the semantic lock for the remaining direct paths. It proves `cancel_timed_out_orders` is still direct/provider-unwired and cancellation-capable, proves provider calls in `apps/api/app/workers/tasks.py` remain limited to `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders`, and classifies the remaining service helpers as write-capable or mixed before any further migration.

`sync_account_snapshot` now delegates only final Trading 212 adapter construction to `create_trading212_provider_adapter(...)`. It is still an isolated read-only worker task that reads account summary with `get_account_summary()`, uses active encrypted `BrokerConnection` credentials, and does not submit, cancel, or modify broker orders. Active connection lookup, credential decryption, reconnect-required handling, environment gates, provider-validation failure summarization, and snapshot persistence remain in worker code. Write-capable service helpers and worker cancel/strategy execution paths remain deferred until each has explicit provider-equivalence tests for its safety gates and broker write boundaries.

`track_cfd_funding` now delegates only final Trading 212 adapter construction to `create_trading212_provider_adapter(...)` with purpose `worker_cfd_funding`. It remains read-only at the broker boundary and calls only `get_positions()` before persisting local CFD funding records. Active connection lookup, credential decryption, reconnect-required handling, `require_broker_environment(conn.environment, action="worker cfd funding")`, provider-validation failure summarization, and local funding persistence remain in worker code. Write-capable worker and service paths remain deferred.

`reconcile_pending_orders` now delegates only final Trading 212 adapter construction to `create_trading212_provider_adapter(...)` with purpose `worker_reconcile`. Order selection, mock-mode skip behaviour, active connection lookup, `require_broker_environment(conn.environment, action="worker reconcile")`, credential decryption, reconnect-required handling, provider-validation failure summarization, `ExecutionEngine` construction, `ExecutionEngine.reconcile_order(...)`, and task summary counts remain in worker code. Provider validation preserves the previous direct worker path's demo and live reconcile behaviour; `worker_reconcile` is not a live-only purpose.

`cancel_timed_out_orders` remains intentionally unwired from the provider and direct because it is cancellation-capable through `ExecutionEngine.cancel_order(...)`. Focused unit tests document that it does not call the provider helper, does not construct before timed-out candidate selection, active connection lookup, `require_broker_environment(...)`, or credential decryption, and makes no real broker calls in tests.

The remaining direct runtime construction count in `apps/api/app/workers/tasks.py` is now `{"construct": 1, "import": 1}`, owned by `cancel_timed_out_orders`.

The other remaining direct paths are intentionally deferred:

- `position_monitor` is write-capable because it can submit automated exits and EOD flatten orders.
- `strategy_runner` is mixed/write-capable because it reads account/positions and can submit strategy entry and exit orders.
- `portfolio_execution_service` is mixed/write-capable because it reads account/positions and can submit rebalance orders.
- `system_control` is mixed/write-capable because the same broker helper backs read-only status calls and emergency cancel/flatten operations.
- manual smoke scripts are terminal-only/manual DEMO tools and are not production provider migration targets.

None of these paths should be migrated from this document alone. Each needs a focused tests-only/equivalence PR before runtime construction changes.

Tests may still import or monkeypatch `Trading212Adapter` to prove safety boundaries without network calls. Those references are not runtime construction paths.

## Current Credential Sources

Trading 212 credential selection is broker-specific today and should remain broker-specific when a provider is introduced:

- `/v1/broker/trading212/connect` receives submitted credentials, validates the requested environment through `require_broker_environment(...)`, tests them with `Trading212Adapter`, and stores encrypted credentials on `BrokerConnection`.
- `/v1/broker/trading212/test` decrypts active `BrokerConnection` credentials for `settings.APP_MODE` and tests them with `Trading212Adapter`.
- `get_broker()` prefers active encrypted credentials for the current user and `settings.APP_MODE`.
- `get_broker()` has a demo-only fallback to `T212_DEMO_API_KEY` and `T212_DEMO_API_SECRET` when no active demo connection exists.
- The demo reconciliation scheduler uses `T212_DEMO_API_KEY`/`T212_DEMO_API_SECRET`, falling back to generic `T212_API_KEY`/`T212_API_SECRET` in its existing terminal-oriented path.
- Controlled demo scripts use their own explicit environment-variable gates and credential lookup rules.

A future provider must not generalize these rules into a credential-agnostic shortcut. Credential lookup should be owned by broker-specific provider code or broker-specific request builders.

## Current Safety Gates

The existing safety model is explicit and broker-aware:

- `Trading212Adapter.__init__` calls `require_adapter_credentials(...)`.
- `require_adapter_credentials(...)` calls `require_broker_environment(...)` and rejects blank credentials.
- `require_broker_environment(...)` blocks real broker access in `APP_MODE=mock` and `APP_MODE=paper`.
- Demo mode may only use the demo broker environment.
- Live mode requires the live broker environment and `LIVE_TRADING_ENABLED=true`.
- Demo order placement is separately gated by `T212_DEMO_ORDER_ENABLED`.
- Demo multi-order smoke flows require explicit terminal confirmation and bounded order counts.
- Demo reconciliation requires `APP_MODE=demo`, `T212_ENVIRONMENT=demo`, `LIVE_TRADING_ENABLED=false`, broker `environment="demo"`, a local demo order, and a broker order id.
- Reconciliation uses `ReconciliationHistoryBrokerProtocol`, which exposes only `environment` and `get_historical_orders(...)`.
- The canonical write-method inventory marks placement, cancellation, and compatibility write names as write-like.

A provider must call the same safety gates before adapter construction and must not make generic live broker construction possible.

## Route Behavior That Must Not Change Yet

The current public broker API remains Trading 212-specific:

- The route prefix stays `/v1/broker/trading212`.
- `BrokerConnection.broker` remains `"trading212"` for Trading 212 connections.
- Mock-mode connect/status behavior remains synthetic and ignores submitted credentials.
- Demo missing-credential status remains Trading 212-specific.
- Connect/test routes continue to test Trading 212 credentials before storing or reporting status.
- Order routes continue to call `get_broker()` and do not receive a broker id parameter.
- Reconciliation run-once routes continue to depend on `get_broker()` and the current demo-only safety gates.

Provider work should first preserve these route contracts. A broker-neutral route surface is a separate design and should not be smuggled into the provider migration.

## Proposed Provider Model

The future provider should separate three concerns:

1. Resolve the requested broker id and runtime environment.
2. Select and decrypt credentials using broker-specific rules.
3. Construct the broker adapter only after explicit app-mode, environment, credential, and live gates pass.

Proposed type shape:

```python
BrokerId = Literal["trading212"]
BrokerRuntimeEnvironment = Literal["demo", "live"]

@dataclass(frozen=True)
class BrokerProviderRequest:
    broker_id: BrokerId
    environment: BrokerRuntimeEnvironment
    user_id: uuid.UUID | None
    purpose: Literal[
        "dependency",
        "credential_test",
        "demo_reconciliation",
        "worker_account_sync",
        "worker_cfd_funding",
        "worker_reconcile",
        "worker_cancel",
    ]
```

The first implementation should support only `broker_id="trading212"` and `environment in {"demo", "live"}`. `mock` and `paper` are runtime modes, not real broker environments. Mock-mode adapter selection can remain outside the real broker provider or be represented by a separate local-only provider path that cannot return a live-capable adapter.

The provider result should be an async context-manager-capable adapter implementing the protocol required by the caller:

- read-only account/order paths can depend on `ReadOnlyBrokerProtocol`;
- demo reconciliation can depend on `ReconciliationHistoryBrokerProtocol`;
- order placement paths can depend on `OrderPlacementBrokerProtocol`;
- future narrower protocols should be preferred when a caller needs less surface.

The provider itself should not expose placement/cancel methods. It should only return an adapter after validating the request. Write capability belongs to the returned adapter protocol and the downstream execution safety gates.

## Type-Only Provider Scaffolding Added

`apps/api/app/broker/provider.py` now defines type-only provider request scaffolding: `BrokerId`, `BrokerRuntimeEnvironment`, `BrokerProviderPurpose`, frozen `BrokerProviderRequest`, and `validate_broker_provider_request(...)`.

The helper is pure fail-closed validation only. It accepts only `trading212`, accepts only real broker environments `demo` and `live`, rejects mock/paper app modes for real broker construction, blocks demo-to-live and live-to-demo requests, and requires `live_trading_enabled=True` for live validation. It does not construct `Trading212Adapter`, touch credentials, access the database, import API routes, call Trading 212, or wire any runtime call sites.

## Trading 212 Provider Function Added

`apps/api/app/broker/provider.py` now also contains `BrokerProviderCredentials`, `validate_broker_provider_credentials(...)`, and `create_trading212_provider_adapter(...)` behind unit tests. The function requires explicit credentials, validates the provider request first, rejects blank credentials before adapter construction, and only then constructs the existing `Trading212Adapter`.

`create_trading212_provider_adapter(...)` validates the provider request before credentials. Request gates reject unsupported broker ids, unsupported broker environments, unsupported app modes, mock/paper modes, demo-to-live requests, live-to-demo requests, and live requests when `LIVE_TRADING_ENABLED` is not true. Credential validation runs only after those request gates pass, and the local `Trading212Adapter` import/construction happens only after both validators pass.

`get_broker()` now delegates final Trading 212 adapter construction to this helper after it has selected already-active encrypted credentials or the existing demo fallback credentials. Credential lookup, credential decryption, reconnect-required marking, route behaviour, and order-placement gates remain outside the provider and unchanged.

The demo reconciliation scheduler startup path, terminal one-shot worker script, `sync_account_snapshot`, `track_cfd_funding`, and `reconcile_pending_orders` now also delegate only final Trading 212 adapter construction to `create_trading212_provider_adapter(...)`. Their gates, enabled checks, credential source selection, scheduler timing, audit behaviour, order selection, account snapshot and CFD funding persistence, `ExecutionEngine` reconciliation, and broker boundaries remain in caller code. `/v1/broker/trading212` route names and schemas, credential lookup/decryption rules, order placement, cancellation behaviour, and frontend controls remain unchanged.

## Broker Route Credential-Test Migration

`/v1/broker/trading212/connect` and `/v1/broker/trading212/test` now delegate final Trading 212 adapter construction to `create_trading212_provider_adapter(...)` for credential testing. The route layer still owns submitted credential handling, active `BrokerConnection` lookup, credential encryption/decryption, reconnect-required marking, response schemas, status codes, route names, and audit events.

The provider receives only already-selected explicit credentials plus a `BrokerProviderRequest` with `broker_id="trading212"`, the selected demo/live environment, `purpose="credential_test"`, and the current user id. It still does not query the database, decrypt secrets, read environment variables, change credential precedence, alter route schemas, place orders, or own audit behaviour.

## Get Broker Equivalence Tests Added

`apps/api/tests/integration/test_get_broker_provider_equivalence.py` now documents the current `get_broker()` behaviour that a future provider migration must preserve. The tests cover mock-mode selection, active encrypted credential precedence, demo fallback credentials, demo refusal to use live credentials, live flag blocking, invalid runtime modes, unreadable stored credentials, stable safety error details, and proof that demo fallback adapter construction goes through the provider helper with the already-selected credentials and request data.

## Scheduler And Worker Equivalence Tests Added

`apps/api/tests/integration/test_scheduler_worker_provider_equivalence.py` now documents the migrated scheduler and worker Trading 212 construction behaviour. The tests cover the background scheduler's demo-only startup gates, provider request data, existing `T212_DEMO_*` to generic `T212_API_*` credential fallback, refusal to use live credentials, and proof that unsafe states fail before provider construction. They also document that `DemoReconciliationWorker` receives an already-constructed broker while the terminal worker script calls the provider with its current generic demo credential environment variables under demo-only gates.

## Trading 212 Provider Behavior

For Trading 212, a future provider would preserve existing construction behavior:

- `broker_id="trading212"` maps to `Trading212Adapter`.
- `environment="demo"` maps to `https://demo.trading212.com` through the existing `broker_base_url_for(...)` path.
- `environment="live"` maps to `https://live.trading212.com` only when `require_broker_environment("live", ...)` allows it.
- Submitted credential tests keep using the submitted key/secret and requested environment.
- Authenticated route dependencies keep preferring active encrypted `BrokerConnection` credentials for the current user and app mode.
- Demo fallback credentials remain `T212_DEMO_API_KEY` and `T212_DEMO_API_SECRET` for the existing demo dependency path.
- Scheduler startup, the terminal one-shot demo reconciliation worker, account snapshot sync, and CFD funding tracking now use provider construction after their existing gates; remaining smoke scripts stay direct, manual, and Trading 212-specific for now.

The provider should not infer that a live credential is safe because it exists. Live construction remains blocked unless app mode, environment, live flag, and live readiness checks allow the relevant action.

## Broker ID And Environment Model

Initial broker ids:

- `trading212`: the only real broker id supported by the first provider.

Reserved future ids:

- `alpaca`: reserved for a later recorded-fixture/non-live adapter spike, not implemented here.
- `mock`: local synthetic adapter identity if a later design chooses to model mock selection through the provider. It must not share the real broker credential path.

Initial environments:

- `demo`: real Trading 212 demo endpoint.
- `live`: real Trading 212 live endpoint, still blocked unless live gates allow it.

Local runtime modes:

- `mock`: application runtime mode that should return `MockBrokerAdapter` through existing logic or a separate local-only path.
- `paper`: local paper execution mode; it should not construct real broker adapters.

The provider should not treat `paper` as a real broker environment. Paper execution remains a local engine concern.

## Why Not Wire It Yet

Introducing a runtime provider too early would change the highest-risk layer of the broker integration: credential lookup, live gating, order placement dependencies, and worker construction. The current code has several direct construction paths with slightly different credential and safety assumptions. Collapsing those paths before documenting their behavior could accidentally:

- allow live adapter construction from a generic path;
- weaken demo-only reconciliation and smoke-script gates;
- hide Trading 212-specific credential names behind misleading generic names;
- change `/v1/broker/trading212` response behavior;
- make tests pass with a fake provider while real credential paths drift;
- expand broker write capability to read-only callers.

The provider should be introduced only after its test matrix proves it preserves the current Trading 212 behavior exactly.

## Phased Migration Plan

1. Keep this PR documentation-only.
2. Add type-only provider scaffolding with `BrokerId`, `BrokerRuntimeEnvironment`, `BrokerProviderRequest`, and no runtime call-site wiring.
3. Add provider tests that prove unsupported broker ids fail closed, mock/paper modes cannot construct real broker adapters, demo can only request demo, and live requires the existing live gates.
4. Done: add Trading 212 provider-function scaffolding behind tests, without wiring routes.
5. Done: add `get_broker()` behaviour-equivalence tests for credential precedence, demo fallback, safety errors, and provider-wiring proof.
6. Done: move `get_broker()` to call the provider while preserving every existing error message, credential fallback, and route behavior.
7. Done: move broker route credential-test construction to the provider while keeping `/v1/broker/trading212` names and response schemas unchanged.
8. Done: add scheduler/worker provider-equivalence tests for current demo-only gates and credential fallback behavior before migrating construction.
9. Done: move scheduler and terminal worker construction to the provider while preserving the newly locked demo-only gates, credential sources, route handoff behaviour, and read-only reconciliation boundary.
10. Done: move `sync_account_snapshot` final adapter construction to the provider while preserving active connection lookup, credential decryption, environment gates, reconnect-required handling, snapshot persistence, and read-only account-summary behavior.
11. Done: move `track_cfd_funding` final adapter construction to the provider while preserving active connection lookup, credential decryption, environment gates, reconnect-required handling, local CFD funding persistence, and read-only positions behavior.
12. Done: add order-worker provider-equivalence tests for `reconcile_pending_orders` and `cancel_timed_out_orders` before any order-worker provider migration, locking direct construction gates, credential source, initial provider-unwired status, and fake engine/broker boundaries.
13. Done: move `reconcile_pending_orders` final adapter construction to the provider while preserving worker-owned order selection, active connection lookup, credential decryption, environment gates, reconnect-required handling, `ExecutionEngine.reconcile_order(...)`, and summary behaviour.
14. Done: add a tests/docs-only write-capable provider-boundary audit that locks the remaining direct paths and classifies `cancel_timed_out_orders`, `position_monitor`, `strategy_runner`, `portfolio_execution_service`, `system_control`, and manual smoke scripts without changing runtime code.
15. Before migrating another direct path, add a tests-only/equivalence PR for exactly one selected candidate. It must prove the current direct/provider-unwired baseline, safety gates, credential source, provider request purpose, fake broker boundary, and unchanged read/write behavior.
16. Consider broker-neutral route design only after the Trading 212 provider migration is complete and behavior-equivalent.
17. Design any second broker with recorded/non-live fixtures. Do not add live trading or strategy-driven broker writes as part of that spike.

## Risks Of Introducing A Provider Too Early

- A generic provider could accidentally normalize away `T212_*` safety gates while the rest of the app still assumes Trading 212 semantics.
- A provider that returns a broad adapter to every caller could undo the narrow reconciliation-history boundary.
- Centralized credential lookup could change the precedence between encrypted connections and environment fallback credentials.
- Worker tasks could start constructing adapters in runtime modes where they previously returned early.
- Live-mode code paths could become easier to reach before live-readiness evidence is complete.
- Tests could become less precise if they mock the provider instead of proving caller-specific safety behavior.

## Acceptance Criteria For A Runtime Provider PR

A later runtime provider PR should be accepted only when:

- it supports only `trading212` unless a separate second-broker design has been approved;
- it preserves `/v1/broker/trading212` route paths, request schemas, response schemas, status semantics, and audit events;
- it preserves active encrypted credential lookup and demo fallback credential behavior;
- it blocks mock and paper runtime modes from real broker construction;
- it blocks demo-to-live construction;
- it blocks live construction unless `LIVE_TRADING_ENABLED=true` and the existing live safety policy allows the action;
- it does not add placement/cancel methods to read-only or reconciliation protocols;
- it keeps scheduler and reconciliation behavior demo-only;
- it has focused tests for credential precedence, safety-policy failures, unsupported broker ids, and unchanged route behavior;
- it uses recorded/fake adapters only in tests and makes no network calls.

## Next Recommended PR

Keep write-capable paths deferred. The next PR should be tests-only/equivalence for exactly one candidate and should not migrate runtime construction. `cancel_timed_out_orders` should remain especially late because it can cancel broker orders through the execution engine; migrate it only when unchanged cancellation selection, environment gating, credentials, and broker-write behaviour are proven against fakes. Continue to avoid runtime migrations for `position_monitor`, `strategy_runner`, `portfolio_execution_service`, and `system_control` until each path has similarly focused tests for its safety gates, credential source, broker write boundary, and unchanged order behavior.
