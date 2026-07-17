# Implementation Roadmap

This roadmap reflects the post-maintenance state of the active working repo:

- Active working repo: `/Users/Ameer/Desktop/t212-cashguard-codex`
- Git host repo for linked worktrees: `/Users/Ameer/Desktop/t212-cashguard`
- Do not move or delete `/Users/Ameer/Desktop/t212-cashguard`; it anchors linked worktrees.
- Current audited main SHA: `0c429cb9237d5d3c223aee0418aa92116f73526f`

Audit snapshot:

- No open PRs at audit time.
- Recent main CI and CodeQL were green at audit time.
- Maintenance/security queue is clear at audit time.
- Dependabot alert #58 for dev-only `js-yaml` is fixed.
- Recent maintenance included FastAPI 0.137.2 compatibility, Next.js 16.2.9,
  OpenTelemetry lockfile updates, Hypothesis backend test dependency updates,
  stronger live `PortfolioAttributionService` coverage, operator safety visibility,
  and the dev-only `js-yaml` alert cleanup.

## Safety Baseline

CashGuard remains a Trading 212 DEMO and paper-mode hardening project. Live trading is
disabled and not live-ready.

The roadmap must not be used as approval to:

- enable live trading
- weaken `LIVE_TRADING_ENABLED=false`
- weaken `CASH_ONLY_MODE=true`
- weaken `APP_MODE=mock` as the safe default
- weaken `POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY=block_trading`
- add frontend buy, sell, order, deposit, withdraw, banking, or cash-movement controls
- add Kraken/crypto trading work
- weaken auth, broker credentials, safety readiness, provider, or execution gates
- bypass CI or weaken tests
- delete quarantined folders permanently
- delete legacy attribution code without a separately approved Level C cleanup PR

The no-trading-controls invariant is enforced by regression tests:
`apps/web/tests/e2e/safety-invariants.spec.ts` sweeps every app page for
order-placement controls and verifies the paper order form only reaches
`/orders/paper`, and `apps/web/tests/unit/no-trading-controls-source.test.ts`
statically proves the live order-placement client method has no UI call sites.

## How To Choose Work

Prefer small PR-sized targets. Keep docs/tests/investigation separate from runtime-adjacent
changes. Runtime-adjacent work needs a stop-before-merge review even when the diff is small.

Autonomy levels:

- Level A: docs, tests, audits, and investigation only
- Level B: read-only UI/API visibility or CI/dependency hygiene
- Level C: runtime-adjacent behavior, deletion, broker safety, attribution logic, or
  execution-quality changes

## Recommended Next Targets

### Live-readiness attestation expiry enforcement (PR #168)

Status: `[In review]` — `feat/live-readiness-attestation-expiry`.

Autonomy level: Level C

Implements the fail-closed expiry policy approved in the #166 proposal and
specified by the #167 tests. `LiveReadinessService.evaluate()` now enforces a
24-hour TTL (`_ATTESTATION_MAX_AGE`) on the manual attestations:

- `demo_validated`
- `broker_test_attested`
- `telegram_test_attested`
- `kill_switch_tested`
- `live_unlock_acknowledged` (conservative 24-hour expiry; true same-session
  scoping deferred because no server-side session identity exists yet)

Fail-closed rules: missing, malformed, or future-skewed timestamps fail the
check. `ready_for_live` now additionally requires a fresh unlock
acknowledgement, not just the persisted `live_trading_unlocked` flag.
`live_broker_test_recent` keeps its existing 24-hour recency window and a
stale broker test is still not offset by `broker_test_attested`. No schema,
migration, frontend, or execution-path changes.

Deferred to later PRs:

- reconciliation-backed demo validation evidence
- same-session `live_unlock_acknowledged` semantics
- machine-readable freshness/reason-code fields in the readiness API and
  operator/settings UI visibility

### Test specification added in PR #167

Status: `[Done]` — merged via PR #167 (`test/live-readiness-expiry-spec`).

Autonomy level: Level A

PR #167 is tests/docs only. It locks current live-readiness recency behavior
and documents future attestation-expiry behavior without changing readiness
gates, safety behavior, broker/provider/execution behavior, live trading state,
or Kraken/crypto code.

Active current-behavior tests added in
`apps/api/tests/unit/test_live_readiness.py`:

- broker tests older than 24 hours fail `live_broker_test_recent`
- broker tests within 24 hours pass `live_broker_test_recent`
- old manual attestations remain accepted under current behavior
- stale broker recency is not offset by `broker_test_attested`
- the global kill switch blocks readiness

Skipped future-policy tests to activate during the later approved Level C
expiry-enforcement PR:

- broker-test attestation expires after 24 hours
- Telegram attestation expires after 24 hours
- kill-switch drill expires before a live smoke test
- demo validation requires fresh reconciliation evidence
- final live unlock acknowledgement is session-scoped
- expired attestations surface expired/stale reason codes

PR #168 activated the 24-hour expiry tests (including demo validation and a
conservative 24-hour unlock-acknowledgement expiry) and updated the
characterization tests to the new fail-closed behavior. The
reconciliation-backed demo evidence, same-session unlock, and reason-code
tests remain skipped with explicit deferral reasons.

### 1. Operator "Why Blocked" Readiness Detail

Status: `[Done]` — landed via PR #147 (`feat/operator-why-blocked-readiness`).

Autonomy level: Level B

What landed:

- `_compute_blocking_reasons()` in `apps/api/app/api/v1/routes/operator.py` returns
  structured `OperatorBlockingReasonOut` items with `code`, `severity`, and `message`.
- `WhyBlockedPanel` in `operator-dashboard.tsx` renders reasons near the top of
  the operator page.
- `why_blocked` is now part of `OperatorStatusOut` and surfaced in unit and
  Playwright E2E tests.

No broker calls, scheduler triggers, strategy runs, or mutation controls added.

### 1b. Operator CashGuard Card

Status: `[Done]` — landed via PR #160 (`feat/operator-cashguard-card`).

Autonomy level: Level B

What landed:

- `CashGuardCard` component added to `apps/web/components/operator/operator-dashboard.tsx`.
- Placed between `ExecutionBoundary` and the venue cards section.
- Calls the existing `useCashGuard()` hook (which hits `/api/v1/account/cash-guard`)
  for cash figures. No backend changes made.
- Displays: cash-only mode, kill switch state, available cash, reserved cash,
  total cash, currency, operator blockers, and a read-only safety note.
- Handles loading, error/unavailable, ok, degraded, and blocked states.
- `data-testid="operator-cashguard-card"` for test targeting.
- Unit tests added to `apps/web/tests/unit/operator-dashboard.test.tsx`.
- E2E mock route for `/v1/account/cash-guard` and assertions added to
  `apps/web/tests/e2e/operator.spec.ts`.

No backend changes, no new endpoints, no broker calls from the operator page,
no order controls, no safety-gate modifications, no live trading changes.

### 1c. Operator Protective Stops Visibility

Status: `[In review]` — `feat/operator-circuit-breaker-visibility`.

Autonomy level: Level B

Key repo evidence gathered before implementation:

- The in-process `CircuitBreaker` singletons in `apps/api/app/broker/circuit_breaker.py`
  are not wired into any live broker call path (referenced only by tests and a
  never-updated Prometheus gauge). Their in-memory state is not a reliable
  protection signal and is intentionally NOT surfaced as one.
- The reliable, persisted protective-stop state was invisible to operators:
  `AppSettings.kill_switch_active` / `auto_trading_enabled` (set by the
  emergency endpoint, the risk engine, and the circuit breaker's auto-kill
  path) and the `RiskEvent` trigger history (`kill_switch_on/off`,
  `kill_switch_block`, `cash_guard_block`, …). An active GLOBAL kill switch
  did not affect `overall_status`, which only checked per-venue kill switches.

What landed:

- `protective_stops` object added to `GET /v1/operator/status`
  (`OperatorProtectiveStopsOut`): status ok/triggered/unknown, global kill
  switch and auto-trading flags, last kill-switch event, and a sanitized
  allowlist-filtered list of recent protective `RiskEvent`s (no raw payloads).
- `overall_status` now also reports `blocked` when the persisted global kill
  switch is active (fail-closed direction only), mirrored by a new
  `global_kill_switch_active` entry in `why_blocked`.
- `ProtectiveStopsCard` added to `operator-dashboard.tsx`
  (`data-testid="operator-protective-stops"`), read-only, no
  reset/clear/enable/disable controls.
- Backend unit tests, frontend unit tests, and operator E2E assertions added.

No new enforcement logic, no kill-switch or safety-policy behavior change, no
broker calls from the operator endpoint, no mutation endpoints, no controls.

### 1d. Operator Reconciliation Visibility / E2E Hardening

Status: `[In review]` — `feat/operator-reconciliation-visibility`.

Autonomy level: Level B

Key repo evidence gathered before implementation:

- `GET /v1/broker/trading212/reconciliation/status` and
  `GET /v1/broker/trading212/reconciliation/scheduler/status` already expose
  worker/scheduler state, and `DemoReconciliationStatusCard` already renders it,
  but the operator Playwright E2E never mocked those endpoints — the card
  silently rendered its error state in E2E with zero assertions.
- The existing run summaries already carry `missing` (local order not found in
  broker history — a broker/local mismatch signal) and `failed` counts, but the
  card hid them; staleness and `last_error_message` were also not surfaced.

What landed (frontend + tests only, no backend changes):

- `DemoReconciliationStatusCard` now shows Missing/Failed counts, a derived
  read-only `Stale` badge (last finished run older than 3× the scheduler
  interval, 5-minute floor), the scheduler's `last_error_message`, and explicit
  read-only/no-reconciliation-controls wording.
- Unit tests for the new states; operator E2E now mocks both reconciliation
  endpoints and asserts card content, mismatch counts, staleness, and zero
  buttons inside the card.

No reconciliation algorithm, broker call, execution, or safety-enforcement
changes. No mutation endpoints or controls added.

### 2. Performance Attribution Slippage Caveats

Status: `[Done]` — read-only disclosure landed via PR1 from
`docs/architecture/backtest-execution-quality-parity-investigation.md`
(`feat/report-attribution-caveats`).

Autonomy level: Level B.

What landed:

- `PerformanceReport` and `PortfolioStrategyAttributionSummaryOut` (and the
  inherited `PortfolioStrategyAttributionOut`) now carry a `coverage_caveats`
  field stating plainly that slippage, fees, rejected/cancelled orders, and
  reconciliation delay are not joined into those figures.
- No numeric calculation, order path, or trading behavior changed.

Still open (separately approved Level C work, not started here):

- joining real slippage data into `symbol_attribution()`
  (`apps/api/app/services/performance_attribution.py`, currently hardcoded to
  `0.0`)
- backtest assumption metadata (PR3 in the parity investigation doc)

Files touched:

- `apps/api/app/api/schemas.py`
- `apps/api/app/api/v1/routes/reports.py`
- `apps/api/app/services/portfolio_attribution_service.py`
- `apps/api/tests/integration/test_reports_caveats.py`
- `apps/api/tests/unit/test_portfolio_attribution_service.py`

### 3. Legacy Portfolio Attribution Deletion Proof

Status: `[Blocked until separately approved]`

Autonomy level: Level C

Goal:

- prove the legacy attribution module is runtime-unused
- preserve or replace useful test coverage
- delete only after explicit approval

Why this matters:

- PR #131 strengthened live `PortfolioAttributionService` coverage
- duplicate attribution logic still creates maintenance risk
- deletion is runtime-adjacent and must not be bundled with other roadmap work

Likely files:

- `apps/api/app/services/portfolio_attribution.py`
- `apps/api/tests/unit/test_portfolio_attribution.py`
- `docs/architecture/portfolio-attribution-duplication-investigation.md`

Validation:

- `rg` proof of no runtime imports
- backend tests
- route tests covering current service behavior

### 4. Broker Error And Audit Payload Sanitization

Status: `[Planned]`

Autonomy level: Level C

Goal:

- ensure broker exceptions cannot leak sensitive text into audit/order-event payloads
- preserve operational categories without storing secrets
- avoid changing order state behavior

Likely files:

- `apps/api/app/execution/engine.py`
- execution and audit tests

Validation:

- tests proving raw secret-like broker errors are not persisted in audit/order-event payloads
- tests proving existing safety gates still block unsafe order paths

### 5. Backtest Versus Execution-Quality Parity Investigation

Status: `[Planned]`

Autonomy level: Level A

Goal:

- document where backtest cost/reject/slippage assumptions differ from demo execution data
- add characterization tests only if needed
- avoid making backtests promotion authority

Likely files:

- `apps/api/app/backtest/engine.py`
- `apps/api/app/backtest/portfolio_engine.py`
- `docs/testing.md`

Validation:

- docs-only evidence matrix or targeted characterization tests
- no runtime trading changes

### 6. High-Risk PR Verification Profile

Status: `[Planned]`

Autonomy level: Level B

Goal:

- define a clear verification profile for Level C PRs
- keep default CI practical
- avoid weakening required checks

Likely files:

- `.github/workflows/ci.yml`
- `Makefile`
- docs

Validation:

- workflow syntax check
- successful CI run before merge
- no bypasses or reduced required checks

### 7. Roadmap And Runbook Hygiene

Status: `[Done]` after this docs refresh lands

Autonomy level: Level A

Goal:

- keep docs aligned with the actual post-maintenance state
- distinguish the active working repo from the git host repo
- keep blocked work explicitly blocked

Likely files:

- `docs/implementation-roadmap.md`
- `docs/runbook.md`
- `docs/operator-manual-qa.md`

Validation:

- `git diff --check`
- docs stale-language grep
- docs-only diff review

## Blocked Until Separate Approval

The following are intentionally not part of routine roadmap work:

- live trading enablement
- frontend order-placement controls
- deposit, withdraw, banking, or cash movement
- Kraken/crypto trading
- slippage telemetry that changes runtime trading behavior
- attribution deletion
- broker/provider rewrites
- safety/readiness/auth weakening
- CI bypasses or test weakening

## Recently Completed Maintenance

- Ruff 0.15 compatibility cleanup — Batch 1: tests-only lint cleanup (I001 import
  order, F401 unused imports, TC003/TC006 typing-import hygiene, UP017
  `datetime.UTC`) across 10 test files plus pinned-formatter alignment. Ruff
  0.15.22 debt reduced 374 → 339 findings. No runtime behaviour changed.
  Dependabot #175 (ruff 0.15.22) stays open until the remaining Ruff 0.15 debt
  is cleared in follow-up batches.
- #118 FastAPI 0.137.2 compatibility update
- #119 Next.js 16.2.9 update
- #122 OpenTelemetry lockfile update
- #113 Hypothesis backend test dependency update
- #131 strengthened live `PortfolioAttributionService` test coverage
- #132 surfaced operator safety visibility
- #133 cleared the dev-only `js-yaml` Dependabot alert

## How To Keep This Roadmap Honest

When starting a workstream:

1. confirm the work fits one small PR-sized target
2. confirm the autonomy level before editing
3. keep docs/tests separate from runtime-adjacent changes
4. update this roadmap when a target lands or becomes blocked
5. never use roadmap text as approval to weaken safety gates
