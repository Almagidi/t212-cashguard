# Unit 1 Ambiguous Broker Submissions Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Ensure every broker-writing path has a durably committed pre-submit intent and treats any potentially transmitted but unacknowledged request as one canonical, non-terminal `submission_unknown` state that blocks duplicates and is never automatically resubmitted.

**Architecture:** Introduce a typed broker-boundary outcome taxonomy based on transmission certainty. In one short transaction, persist the scoped intent, transition it to canonical active `submission_unknown`, and append dispatch-committed attempt evidence before any broker POST. Perform network I/O without a database row lock, then move the unknown state only when authoritative acceptance or rejection evidence can be persisted in a second transaction. A process death anywhere after the dispatch commit leaves one visible, duplicate-blocking unknown record selected for reconciliation and never resubmitted. This deliberately prefers a false-positive unknown if death occurs immediately before the POST over the unsafe possibility of an unrecorded external order. Keep Unit 1 limited to execution state/durability; bounded reconciliation, durable request idempotency, database constraints, and full reservations remain Units 2, 3, 4 and 6.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async ORM, Alembic, PostgreSQL 16, pytest/pytest-asyncio, HTTPX broker adapter, React/TypeScript API types.

---

### Task 1: Characterize the unsafe state transition (RED)

**Files:**
- Modify: `apps/api/tests/unit/test_execution_engine.py`
- Modify: `apps/api/tests/unit/test_order_state_machine.py`
- Inspect: `apps/api/app/execution/engine.py`
- Inspect: `apps/api/app/execution/state_machine.py`

- [ ] Add a broker double that records acceptance and raises `httpx.ReadTimeout` after transmission.
- [ ] Prove current code incorrectly produces terminal `error` and stops duplicate blocking.
- [ ] Add transition tests for one canonical `submission_unknown` state: active, duplicate-blocking, never backward to `pending_intent`.
- [ ] Run and record the expected failing tests before production edits.

### Task 2: Define transmission-aware broker outcomes (RED → GREEN)

**Files:**
- Modify: `apps/api/app/broker/protocols.py`
- Modify: `apps/api/app/broker/trading212.py`
- Modify: broker-double tests under `apps/api/tests/`

- [ ] Verify HTTPX exception contracts from official documentation.
- [ ] Add typed pre-transmission failure, definitive rejection, authoritative acceptance, and ambiguous-submission outcomes/exceptions.
- [ ] Classify only exceptions whose transmission certainty is supported; default uncertain post-dispatch failures to ambiguity.
- [ ] Keep paper/mock construction isolated from the real broker.
- [ ] Prove 400/422 rejection, pre-transmission failure, read timeout, reset, malformed post-transmission response, and selected gateway/server outcomes.

### Task 3: Persist durable intent and attempt evidence (RED → GREEN)

**Files:**
- Modify: `apps/api/app/db/models/__init__.py`
- Create: next Alembic revision after `0020_eod_flatten_operations`
- Modify: `apps/api/app/execution/engine.py`
- Modify: `apps/api/app/db/session.py` only if a narrowly scoped transaction helper is necessary
- Add: focused PostgreSQL integration tests

- [ ] Survey current rows for migration-invalid values; never silently rewrite financial data.
- [ ] Add the minimum append-only submission-attempt representation needed for durable attempt ID, request fingerprint, account/environment scope, expected cost, timestamp, and redacted outcome evidence.
- [ ] In a dedicated short transaction before broker I/O, commit intent, canonical active `submission_unknown`, and dispatch-committed attempt evidence.
- [ ] Persist result in a later transaction without holding a row lock over network I/O.
- [ ] Fault-inject process interruption after intent commit and prove the order remains recoverable.
- [ ] Kill execution after the broker double records the POST but before result persistence; prove a second session finds the original active order/attempt, reconciliation runs, and no second POST occurs.
- [ ] Kill execution after the dispatch commit but before the broker method begins; prove the conservative unknown remains blocked and requires evidence-based reconciliation/manual confirmation rather than automatic resubmission.
- [ ] Prove rollback before the durability boundary results in zero broker calls.

### Task 4: Integrate the canonical unknown state (RED → GREEN)

**Files:**
- Modify: `apps/api/app/execution/state_machine.py`
- Modify: `apps/api/app/execution/engine.py`
- Modify: relevant API schemas/routes
- Modify: relevant frontend order types/rendering
- Modify: risk/alert event integration

- [ ] Make `submission_unknown` legal, active and duplicate-blocking.
- [ ] Ensure it retains the existing expected-cash hold proxy; explicitly defer the full reservation ledger to Unit 6.
- [ ] Emit a critical durable risk/alert event without raw broker payloads or secrets.
- [ ] Make API and frontend serialization exhaustive for the new state.
- [ ] Audit manual, strategy, portfolio, position-monitor, emergency and EOD callers for truthful returned-state handling.
- [ ] Do not create cancellation-status synonyms in this unit; preserve prior active state plus durable evidence when cancellation certainty is unknown, pending the dedicated lifecycle design.

### Task 5: Add the narrow recovery route required by this unit

**Files:**
- Modify: reconciliation service and focused tests only as required to select and evidence `submission_unknown`

- [ ] Select `submission_unknown` even when acknowledgement or broker ID is absent; do not invent an auxiliary status for the same uncertainty.
- [ ] Resolve only from an unambiguous evidence-supported broker-history match.
- [ ] Leave zero-match or multiple-match cases unresolved and never submit.
- [ ] Prove a later synthetic history result resolves the original order and a second broker POST never occurs.
- [ ] Defer bounded pagination, distributed reconciliation ownership and persistent backoff to Unit 2.

### Task 6: Prove safety and migration behavior

- [ ] Run targeted state-machine, execution, broker-boundary, API, frontend, reconciliation and PostgreSQL process-death tests.
- [ ] Run the migration from exact prior head, downgrade where safe, and re-upgrade on PostgreSQL.
- [ ] Mutation proof: temporarily restore terminal `error` mapping and prove the timeout test fails; restore and prove it passes.
- [ ] Run complete backend, frontend and mock E2E baselines in a sterile broker-tripwire environment.
- [ ] Run Ruff/format/mypy on changed files; report unrelated whole-tree debt separately.
- [ ] Run `git diff --check`, secret boundary, Gitleaks, dependency audits and provider-boundary tests.

### Task 7: Independent review and protected PR

- [ ] Give a fresh read-only reviewer only the base/head SHAs, finding, acceptance criteria, invariants and non-goals.
- [ ] Require PASS with no P0/P1 before opening or updating the focused PR.
- [ ] Push, open the PR with the mandated body, wait for every required check, and do not bypass review.
- [ ] Record rollback as a code/schema revert that preserves unknown/evidence rows; never downgrade by deleting unresolved evidence.
- [ ] Leave residual risks explicit: no bounded reconciliation ownership (Unit 2), no request idempotency (Unit 3), no DB vocabulary constraints (Unit 4), and no durable reservation ledger (Unit 6).

## Explicit non-goals

- No automatic retry or resubmission.
- No cash-reservation model, broad reconciliation rewrite, manual idempotency design, paper-ledger repair, or operator exception centre.
- No strategy threshold, quantitative formula, Kraken, DCA, demo-order, credential, or live change.
