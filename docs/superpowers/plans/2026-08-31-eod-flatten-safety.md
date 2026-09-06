# EOD Flatten Safety Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Make end-of-day flattening exchange-session-aware, strategy-scoped, durably idempotent, and fail-closed when broker holdings cannot be reconciled to the local strategy ledger.

**Architecture:** A dedicated EOD flatten service will translate the current UTC instant into the configured exchange session, compute a bounded due window, derive each strategy's filled net position from signal-linked orders, and reconcile those quantities against one broker position snapshot. Before any submission it will persist a unique operation and deterministic order intent, commit that evidence, and then route the sell through the existing execution safety boundary. Existing or ambiguous operations never auto-retry; they produce durable risk and operator evidence.

**Tech Stack:** Python 3.12, FastAPI service layer, SQLAlchemy async ORM, Alembic, PostgreSQL concurrency constraints, SQLite unit/integration fixtures, Celery/Redis worker scheduling, pytest.

---

### Task 1: Prove exchange-session scheduling behavior

**Files:**
- Create: `apps/api/tests/unit/test_eod_flatten_schedule.py`
- Create: `apps/api/app/services/eod_flatten.py`

- [ ] Add failing tests for XNYS summer and winter UTC offsets.
- [ ] Add failing tests for the DST transition week, holidays, weekends, and the bounded window edge.
- [ ] Add a failing early-close test proving the calendar close overrides a later configured close.
- [ ] Add a failing unsupported-venue test proving Kraken and unknown venues remain fail-closed.
- [ ] Run the focused schedule tests and record the RED result.
- [ ] Implement the smallest pure scheduling policy that passes the tests.
- [ ] Re-run the focused schedule tests and record GREEN.

### Task 2: Add durable operation identity and deterministic order keys

**Files:**
- Modify: `apps/api/app/db/models/__init__.py`
- Create: `apps/api/app/db/migrations/versions/0020_eod_flatten_operations.py`
- Modify: `apps/api/app/execution/engine.py`
- Create: `apps/api/tests/unit/test_eod_flatten_operation_model.py`
- Modify: `apps/api/tests/unit/test_execution_engine.py`

- [ ] Add failing schema/model tests for the unique operation identity `(operation_kind, strategy_id, venue, exchange_session_date, ticker)` and its order linkage.
- [ ] Add a failing execution-engine test for a caller-supplied stable operation identity producing the same client order key.
- [ ] Run the focused tests and record RED.
- [ ] Add the model and forward-only migration with operation status, attributable quantity, reconciliation metadata, and timestamps.
- [ ] Extend order-intent creation with an explicit stable operation identity while retaining existing behavior for signal and manual orders.
- [ ] Run the focused tests and record GREEN.

### Task 3: Prove scoped attribution and fail-closed reconciliation

**Files:**
- Create: `apps/api/tests/integration/test_eod_flatten_service.py`
- Modify: `apps/api/tests/unit/test_position_monitor.py`
- Modify: `apps/api/tests/unit/test_position_monitor_daily_loss_safety.py`
- Modify: `apps/api/tests/unit/test_position_monitor_provider_equivalence.py`
- Modify: `apps/api/tests/unit/test_write_capable_provider_boundary_audit.py`

- [ ] Add failing tests proving unrelated/manual holdings are not sold.
- [ ] Add failing tests for two strategies sharing a ticker and strategy-specific quantities.
- [ ] Add failing tests for missing, duplicate, insufficient, and active-sell broker/ledger ambiguity; prove zero broker submissions and durable alert/risk evidence.
- [ ] Add a failing test proving broker quantity and `maxSell` cap submissions.
- [ ] Inspect every test double for direct or network-capable broker access before running it.
- [ ] Run the focused tests and record RED.
- [ ] Implement filled-order attribution and one-snapshot reconciliation in the EOD service.
- [ ] Keep all submissions routed through `ExecutionEngine`; never call a broker write method directly.
- [ ] Update structural provider-boundary tests to cover the new service boundary.
- [ ] Re-run the focused tests and record GREEN.

### Task 4: Prove durable replay and recovery behavior

**Files:**
- Extend: `apps/api/tests/integration/test_eod_flatten_service.py`
- Create: `apps/api/tests/integration/test_eod_flatten_concurrency.py`

- [ ] Add failing tests for repeated scheduler ticks, terminal orders, delayed settlement snapshots, and same-session re-entry.
- [ ] Prove the operation and order intent are committed before the broker submission begins.
- [ ] Prove a submission exception or ambiguous terminal state never causes an automatic resubmission and creates manual-reconciliation evidence.
- [ ] Add a PostgreSQL-gated concurrent claimant test proving one operation and one order intent survive.
- [ ] Run SQLite-focused replay tests and the PostgreSQL test when `POSTGRES_TEST_DATABASE_URL` is available; record skips separately.
- [ ] Implement nested-transaction unique-claim recovery and post-submission operation status updates.
- [ ] Re-run focused replay/concurrency tests and record GREEN.

### Task 5: Replace the unsafe worker path

**Files:**
- Modify: `apps/api/app/workers/tasks.py`
- Modify: `apps/api/app/services/position_monitor.py`
- Create: `apps/api/tests/unit/test_eod_flatten_worker.py`

- [ ] Add failing tests proving the worker passes all eligible strategies and an aware UTC instant into the safe service path.
- [ ] Prove the worker does not use UTC string comparison or a process-only latch for correctness.
- [ ] Prove kill-switch and overlap-lock behavior remain intact.
- [ ] Replace the global `any(...)` trigger and flatten-all implementation with the scoped service.
- [ ] Re-run worker and position-monitor tests.

### Task 6: Validate migrations, safety invariants, and regression scope

**Files:**
- Modify: `docs/SAFETY_MODEL.md`

- [ ] Document the bounded EOD window, `t212 -> XNYS` mapping, durable operation key, fail-closed attribution, and manual-recovery semantics.
- [ ] Run Alembic upgrade from the prior revision and downgrade/upgrade on a disposable database.
- [ ] Run focused EOD, execution, position-monitor, provider-boundary, worker, and calendar suites with broker and alert credentials explicitly blank.
- [ ] Run Redis lock/recovery tests without any broker-capable credentials.
- [ ] Run PostgreSQL concurrency proof when infrastructure is available; report an infrastructure skip as a skip, never as a pass.
- [ ] Re-run DCA inertness tests and verify `RUNNABLE = False` remains unchanged.
- [ ] Run the complete backend suite and web typecheck, lint, tests, and build.
- [ ] Inspect the final diff for secrets, accidental live/demo enablement, out-of-scope Kraken/DCA work, and migration drift.

### Task 7: Preserve reviewability and hand off

**Files:**
- Review: all changed files

- [ ] Confirm only Unit 1 findings F-01, the EOD portion of F-03, and the EOD portion of F-19 are addressed.
- [ ] Commit on `codex/fix-eod-flatten-exchange-session-idempotency` with an evidence-based message.
- [ ] Push the branch and open an unmerged draft PR; do not merge it.
- [ ] Report baseline, RED/GREEN evidence, exact validation commands/results, residual risks, rollback procedure, and Unit 2 as the next unfinished unit.
