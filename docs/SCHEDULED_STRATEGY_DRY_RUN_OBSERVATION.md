# Scheduled Strategy-Signals Task — Dry-Run Observation

Status date: 2026-07-21. Source of truth: `origin/main` at
`e6b5960b1c73c0a2357139f35eeadf68db6a0417`.

This document is scoped to one thing: the Celery-beat-scheduled
`run_strategy_signals` task path (`celery_app.conf.beat_schedule["strategy-signals"]`,
every 5 minutes). It records what is now proven about invoking that task path
directly in `APP_MODE=mock`, what operator-facing visibility exists for it, and
what remains unobserved. It complements — and does not duplicate —
[`PAPER_TRADE_DRY_RUN_VALIDATION.md`](PAPER_TRADE_DRY_RUN_VALIDATION.md) (the
broader paper-trade evidence matrix, which covers the manual/API paper-only
path in depth and this task path at the service-method level). It introduces
no new runtime behaviour.

**Live trading remains disabled and not live-ready.** `LIVE_TRADING_ENABLED`
defaults to `false`, `APP_MODE` defaults to `"mock"`
(`apps/api/app/core/config.py:37,135`). Nothing in this document, or in the
PRs it describes, changes either default or any broker/order/safety/
kill-switch implementation.

## 1. What is proven: the task wrapper itself

Merged as [#201](https://github.com/Almagidi/t212-cashguard/pull/201)
(2026-07-21), file
`apps/api/tests/unit/test_run_strategy_signals_worker.py` (7 tests).
Independently re-run in this worktree against the current `origin/main` HEAD:
**7/7 passed**, and the full backend suite (**1720 passed, 4 skipped, 84.5%
coverage**) is green.

Prior to this PR, `app.workers.tasks.run_strategy_signals` — the actual
Celery task function Celery beat calls every 5 minutes — was **not**
exercised by any test. Coverage stopped at direct calls to
`StrategyRunner.run_all_enabled()` (see
`PAPER_TRADE_DRY_RUN_VALIDATION.md` §1 test 3 and §4). This PR closes that
specific gap using the established pattern already in this codebase for the
other Celery tasks (`test_order_worker_provider_equivalence.py`): direct
invocation of the bound task via `tasks.run_strategy_signals.run()` — Celery's
supported way to call a task's body synchronously, with no broker, Redis, or
worker process required.

| # | Test | Proves |
|---|------|--------|
| 1 | `test_strategy_signals_task_registered_on_five_minute_cadence` | `celery_app.conf.beat_schedule["strategy-signals"]` maps to `app.workers.tasks.run_strategy_signals` on a 300-second (5 min) cadence. |
| 2 | `test_run_strategy_signals_defines_bounded_retries_and_time_limits` | `max_retries=0`, `time_limit=240`, `soft_time_limit=180` — a stuck or failing tick cannot retry indefinitely or run unbounded. |
| 3 | `test_run_strategy_signals_never_references_broker_adapters_directly` | Static (AST) guard: the task function body never references `Trading212Adapter`, `KrakenAdapter`, or `create_trading212_provider_adapter` directly — all broker access is delegated to `StrategyRunner`, whose own gates are proven separately (see §2). |
| 4 | `test_run_strategy_signals_uses_task_lock_and_invokes_runner` | The task acquires `app.core.redis.task_lock("run_strategy_signals", ttl_seconds=270)`, opens a DB session via `AsyncSessionLocal`, constructs exactly one `StrategyRunner`, awaits `run_all_enabled()` exactly once, and records the returned summary via heartbeat. |
| 5 | `test_run_strategy_signals_skips_when_lock_not_acquired_without_touching_session_or_runner` | When the distributed lock is already held (another instance running), the task returns `{"skipped": true, "reason": "already_running"}` **without ever opening a DB session or constructing `StrategyRunner`** — proven with sentinels that raise if either is touched. |
| 6 | `test_run_strategy_signals_propagates_kill_switch_contract_without_broker_access` | When `StrategyRunner.run_all_enabled()` returns `skipped="kill_switch"`, the task wrapper passes that result straight through and adds no broker access of its own around it. |
| 7 | `test_run_strategy_signals_propagates_safe_noop_when_no_enabled_strategies` | When there are no enabled strategies, the task wrapper propagates the bare zero-count summary (no `skipped` key) unchanged. |

`StrategyRunner.run_all_enabled()` itself is stubbed at its constructor
boundary in tests 4–7 above — a deliberate, documented choice (see the test
file's module docstring). Its *real* kill-switch short-circuit, `APP_MODE=mock`
→ `MockBrokerAdapter`-only broker selection, and safety-gate-before-broker-lookup
behaviour is proven separately, against a real database and the real class,
by pre-existing tests:

- `test_kill_switch_skips_automated_strategy_runner_before_broker_lookup`
  (`tests/integration/test_paper_dry_run_validation.py`, merged in #195) —
  real `StrategyRunner`, real SQLite `db` fixture, kill switch active,
  Trading212Adapter/KrakenAdapter sentinel proves neither is ever touched.
- `test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction`
  and `test_run_all_enabled_safety_gates_skip_before_broker_lookup`
  (`tests/unit/test_strategy_runner_provider_equivalence.py`, pre-existing).

Together, §1's new task-level tests and the pre-existing service-level tests
form one continuous, verified chain: **Celery task invocation → task_lock →
session → `StrategyRunner.run_all_enabled()` → kill-switch / mock-mode /
no-broker guarantees**, with no untested link between them.

## 2. What is proven: operator read-only visibility

Merged as [#203](https://github.com/Almagidi/t212-cashguard/pull/203)
(2026-07-21), files `apps/api/app/api/schemas.py`,
`apps/api/app/api/v1/routes/operator.py`,
`apps/api/tests/unit/test_operator_status_api.py` (3 new tests, 29/29 passed
in that file after the change).

Before this PR, `GET /v1/operator/status` surfaced beat/heartbeat visibility
for the DCA planner and the generic worker-heartbeat task
(`_scheduler_entry`, `_heartbeat_entry`) but had no equivalent field for the
`strategy-signals` entry — a gap the frontend had already documented in
[`OPERATOR_SCHEDULER_VISIBILITY.md`](OPERATOR_SCHEDULER_VISIBILITY.md) so the
UI would not invent the data itself. `OperatorSchedulersStatusOut` now also
carries:

- `strategy_signals_registered: bool` — whether the beat entry exists and
  points at the right task (static config fact, read from
  `celery_app.conf.beat_schedule`).
- `strategy_signals_cadence: str | None` — the raw schedule value (`"300.0"`).
- `strategy_signals_task_name: str` — the fully-qualified task name.
- `strategy_signals_observation_status: "ok" | "stale" | "unknown"` — whether
  a *real* invocation has ever recorded a heartbeat, via the existing
  `app.services.worker_health.build_worker_health()` helper (already used by
  `/v1/health/workers`; this is the first place it is also read from the
  operator status endpoint).
- `strategy_signals_last_seen_at: datetime | None`
- `strategy_signals_observation_detail: str` — human-readable explanation.

This is read-only, additive, and derived entirely from data that already
existed (the static beat schedule dict and the heartbeat record every task
already writes on completion via `_complete_task`). It adds no persistence, no
new endpoint, no mutation path, and does not change `overall_status`,
`why_blocked`, or any `safety_flags` value — proven by
`test_operator_status_strategy_signals_metadata_triggers_no_task_or_broker_call`,
which also asserts reading the field never calls `.delay()`/`.apply_async()`
on the real task and never constructs a real broker adapter.

**This field does not, by itself, mean the task has ever actually run.** In a
fresh environment (no heartbeat ever recorded), `strategy_signals_observation_status`
reads `"unknown"` and `strategy_signals_last_seen_at` is `null` — the UI's
documented "must remain absent or unknown" requirement is satisfiable exactly
as specified.

## 3. Can an automated paper-trade test be run now?

**Partial — same overall answer as `PAPER_TRADE_DRY_RUN_VALIDATION.md` §5,
now with the task wrapper itself in scope.**

- The manual/API paper-only path (`POST /orders/paper` → `PaperExecutionEngine`)
  is proven safe and functional end-to-end (`PAPER_TRADE_DRY_RUN_VALIDATION.md` §1).
- The scheduled task's **wrapper** (lock, session, delegation, safety-contract
  propagation) is now proven safe by direct invocation (§1 above).
- The scheduled task's **strategy-execution logic** (kill switch, mock-mode
  broker selection, dry-run routing) is proven safe at the service layer
  (§1 above, citing pre-existing tests).
- What is **still not observed**: an actual Celery beat process firing
  `run_strategy_signals` on its real 5-minute schedule, consumed by an actual
  Celery worker process reading from a real broker (Redis), end to end. Every
  test above invokes the task function directly — none starts `celery beat`
  or `celery worker`.

## 4. Exact command path for a future supervised mock-mode observation

This is a **documented, not-yet-executed** procedure for a human operator to
run in a supervised, disposable environment — not something this session ran.
It requires infrastructure (Redis, a worker process) beyond what a test-suite
run provides, and the real beat interval is 300 seconds, which this session
correctly declined to wait out synchronously to avoid a slow/flaky automated
test (per this task's own instructions).

```bash
# 1. Supervised, disposable environment only. Confirm before starting:
export APP_MODE=mock
export LIVE_TRADING_ENABLED=false
#    ... plus the other test-safe env vars already required by
#    apps/api/tests/conftest.py (SECRET_KEY, MASTER_KEY, REDIS_URL, etc.)
#    No real T212_API_KEY / T212_API_SECRET / KRAKEN_* values should be set.

# 2. Start Redis (task_lock + Celery broker) if not already running, e.g.:
redis-server --daemonize yes

# 3. In one terminal, start a Celery worker (short-lived, supervised):
cd apps/api
celery -A app.workers.celery_app worker --loglevel=info --concurrency=1

# 4. In a second terminal, start Celery beat (short-lived, supervised):
cd apps/api
celery -A app.workers.celery_app beat --loglevel=info

# 5. Observe the worker log for a "strategy-signals" tick firing
#    run_strategy_signals and completing (up to ~5 minutes for the first
#    scheduled tick). Confirm the returned summary is a safe no-op or a
#    kill-switch skip, depending on seeded AppSettings/Strategy state.

# 6. Confirm no broker adapter was ever constructed, e.g. by grepping worker
#    stdout for "Trading212Adapter" / "KrakenAdapter" (should be absent), and
#    by checking that GET /v1/operator/status now reports
#    strategy_signals_observation_status == "ok" with a fresh
#    strategy_signals_last_seen_at.

# 7. Stop both processes (Ctrl-C or `celery control shutdown`) and tear down
#    the disposable environment. Do not leave a beat/worker pair running
#    against a database that also serves real traffic.
```

**No real broker credentials, no real network call, and no live-money order
of any kind are involved in this procedure** — it exercises the exact same
mock-mode, no-broker-adapter contract that §1 and §2 already prove at the
function level, just through the real Celery process machinery instead of a
direct call. This session did not execute this procedure; it is recorded here
as the exact next step for whoever picks this observation up.

## 5. Remaining gaps

**Before an unattended automated paper trade:**

- The real beat+worker end-to-end firing in §4 has not been observed.
- Nothing beyond the individual task/service tests has ever watched a full
  5-minute schedule tick complete and read the result back through the
  operator status endpoint in the same session (the operator-visibility
  fields in §2 have been proven correct against a *manually recorded*
  heartbeat, not one produced by a live tick).

**Before the tiny supervised live-money smoke test**
(`LIVE_SMOKE_TEST_RUNBOOK.md`): unchanged. Nothing in #201 or #203 touches
live-enablement prerequisites — broker credentials/environment configuration,
the `live_trading_unlocked` attestation flow, or recorded owner sign-off.
Live trading remains disabled (`LIVE_TRADING_ENABLED=False` default) and not
live-ready.

## 6. Kill-switch / safety status

- The scheduled task path fails closed on the kill switch, proven both at the
  task-wrapper level (§1, test 6) and, for the real `StrategyRunner`, against
  a real database (`PAPER_TRADE_DRY_RUN_VALIDATION.md` §1 test 3).
- `APP_MODE=mock` is the only mode in which `_get_broker()` is reached without
  constructing a real broker adapter; this is unchanged and re-confirmed, not
  altered, by #201.
- No safety, risk, kill-switch, or readiness gate was modified, loosened, or
  bypassed by #201 or #203; both PRs are additive (new tests, and a purely
  read-only operator field derived from pre-existing data).
