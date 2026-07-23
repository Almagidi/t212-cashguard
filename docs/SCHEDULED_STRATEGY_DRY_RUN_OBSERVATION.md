# Scheduled Strategy-Signals Task — Dry-Run Observation

Status date: 2026-07-21 (updated same day with an executed observation).
Source of truth: `origin/main` at `ebb5cfef79ebc309ab9d9a354817a70eca89f23c`
(the #204 merge; unchanged by this update).

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

**Partial, but the scheduled-task-path gap identified below is now closed.**
Same overall answer as `PAPER_TRADE_DRY_RUN_VALIDATION.md` §5, now with the
task wrapper *and* the real Celery beat/worker process observed end to end.

- The manual/API paper-only path (`POST /orders/paper` → `PaperExecutionEngine`)
  is proven safe and functional end-to-end (`PAPER_TRADE_DRY_RUN_VALIDATION.md` §1).
- The scheduled task's **wrapper** (lock, session, delegation, safety-contract
  propagation) is proven safe by direct invocation (§1 above).
- The scheduled task's **strategy-execution logic** (kill switch, mock-mode
  broker selection, dry-run routing) is proven safe at the service layer
  (§1 above, citing pre-existing tests).
- **Now observed** (§4): a real Celery beat process fired `run_strategy_signals`
  on its actual, unmodified 300-second schedule, consumed by a real Celery
  worker process reading from a real (disposable) Redis broker and writing to
  a real (disposable) Postgres database — with a second, independent
  direct-dispatch invocation through the same real worker beforehand. No
  broker adapter or live endpoint was ever referenced in either process's
  logs. What remains unobserved is unattended, *unsupervised* execution over
  a longer horizon (hours/days) and with strategies actually enabled — this
  session used the repo's seeded defaults (`auto_trading_enabled=False`, no
  enabled strategies), which is the correct, minimal-risk way to observe the
  path without also needing to observe strategy-signal-generation logic
  (already covered by `test_strategy_runner_helpers.py` and friends).

## 4. Supervised mock-mode observation — executed 2026-07-21

This procedure was run in this session, in a disposable environment fully
isolated from any developer database, from the other agent's worktree, and
from any pre-existing local containers. It is recorded here both as evidence
and as a repeatable runbook.

### 4.1 Environment

- **Isolation**: two throwaway Docker containers, uniquely named
  (`agent-a-mock-postgres`, `agent-a-mock-redis`) and mapped to non-default
  host ports (`15432`, `16379`) specifically to avoid colliding with any
  container the sibling `t212-cashguard-codex` worktree owns (stale, stopped
  containers named `t212_postgres`/`t212_redis` already existed there from
  unrelated local-dev sessions; they were left completely untouched —
  confirmed unchanged before and after this run).
- **Safety env vars**: `APP_MODE=mock`, `LIVE_TRADING_ENABLED=false` (both
  also the code defaults — `apps/api/app/core/config.py:37,135`), plus a
  disposable `SECRET_KEY`/`MASTER_KEY`/`ADMIN_EMAIL`/`ADMIN_PASSWORD` used
  only for this container pair. No `T212_*` or `KRAKEN_*` credential
  variables were set at any point (all default to `""` per
  `apps/api/app/core/config.py`).
- **Database**: fresh Postgres 16, migrated with `alembic upgrade head`
  (repo's own migration chain, unmodified) and seeded with the repo's own
  `python -m app.db.seed` — which seeds `AppSettings(id=1, kill_switch_active=False,
  auto_trading_enabled=False, live_trading_unlocked=False)` and strategies
  with `is_enabled=False` by default (`apps/api/app/db/seed.py`). No manual
  data seeding beyond the repo's own script was performed.
- **Broker**: a real Celery worker process (`celery -A app.workers.celery_app
  worker --loglevel=INFO --concurrency=1 --pool=solo`) and a real Celery beat
  process (`celery -A app.workers.celery_app beat --loglevel=INFO`), both
  pointed at the disposable Redis/Postgres above via env vars only — no repo
  file was modified to run this. `--schedule`/`--pidfile` were redirected to
  `/tmp` to avoid writing beat state into the repo tree.

### 4.2 Route A — direct dispatch through a real worker

```
celery_app.send_task("app.workers.tasks.run_strategy_signals")
→ TASK_ID cbe6cfbe-0c00-4446-88c5-8270e010421a
→ TASK_RESULT {'strategies_run': 0, 'signals_generated': 0, 'orders_submitted': 0,
               'risk_blocks': 0, 'errors': [], 'skipped': 'auto_trading_off'}
```

Worker log confirmed: task received, ran `tasks.signals_complete`, and
succeeded in 0.156s. `grep -iE "Trading212Adapter|KrakenAdapter|trading212\.com|kraken\.com"`
across the full worker log matched nothing.

### 4.3 Route B — real Celery beat firing on its actual 300s schedule

Beat started at `17:47:52`. Its own log recorded:

```
17:48:22  Scheduler: Sending due task reconcile-orders (...)
17:52:52  Scheduler: Sending due task strategy-signals (app.workers.tasks.run_strategy_signals)
```

`17:52:52 − 17:47:52 = 300.0s` — the real, unmodified 5-minute cadence, not a
shortened test-only interval. The worker log for the same timestamp:

```
17:52:52,313  Task ...run_strategy_signals[e78de727-...] received
17:52:52,352  tasks.signals_complete errors=[] orders_submitted=0 risk_blocks=0
              signals_generated=0 skipped=auto_trading_off strategies_run=0
17:52:52,353  Task ...run_strategy_signals[e78de727-...] succeeded in 0.040s
```

The bounded wait (~360s, via a background poll — not a synchronous blocking
sleep) was itself the only concession to the real cadence; no schedule value
was shortened or altered to make this observation faster. A combined
end-to-end tripwire grep across both the worker and beat logs for
`Trading212Adapter|KrakenAdapter|trading212\.com|kraken\.com|api\.trading212`
matched nothing.

### 4.4 Operator/status readback

Rather than standing up a full authenticated HTTP server against the
disposable database (a materially larger blast radius for no extra signal),
this session called the exact same service function the route uses
internally — `app.services.worker_health.build_worker_health(db)`, which
`GET /v1/operator/status` calls directly at
`apps/api/app/api/v1/routes/operator.py:436` — against the disposable
session. This is not an approximation of the endpoint; it is the endpoint's
own computation, exercised without the FastAPI/auth layer around it.

| When | `strategy_signals_observation_status` | `strategy_signals_last_seen_at` |
|---|---|---|
| Before any dispatch (fresh env) | `unknown` | `null` |
| After Route A (direct dispatch) | `ok` | `2026-07-21T16:47:16.536591+00:00` |
| After Route B (real beat tick) | `ok` | `2026-07-21T16:52:52.347201+00:00` (age 27s at read time) |

This is exactly the transition `SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md`
(previous revision) and `OPERATOR_SCHEDULER_VISIBILITY.md` specified as the
UI's contract: absent/`unknown` in a fresh environment, `ok` with a fresh
timestamp only once a real invocation has recorded a heartbeat. No safety
flag, `overall_status`, or `why_blocked` value was touched by reading it.

### 4.5 Teardown

Both Celery processes were stopped (`pkill -f "celery -A app.workers.celery_app"`);
both disposable containers were stopped and removed
(`docker rm agent-a-mock-postgres agent-a-mock-redis`); the `/tmp` beat
schedule/pidfile were deleted. The pre-existing, stopped `t212_postgres` /
`t212_redis` / `t212_worker` / `t212_beat` / `t212_api` / `t212_web`
containers belonging to the sibling worktree's project were confirmed
unchanged (same `Exited` status, same age) before and after this session —
they were never started, stopped, or removed by this observation.

### 4.6 Repeatable command reference

```bash
# 1. Disposable, uniquely-named containers — avoids colliding with any
#    existing project's fixed docker-compose container names.
docker run -d --name <unique>-postgres -e POSTGRES_USER=cashguard \
  -e POSTGRES_PASSWORD=cashguard_secret -e POSTGRES_DB=cashguard \
  -p <free-port>:5432 postgres:16-alpine
docker run -d --name <unique>-redis -p <free-port>:6379 \
  redis:7-alpine redis-server --requirepass cashguard_redis

# 2. Migrate + seed (repo's own scripts, unmodified)
cd apps/api
DATABASE_URL=postgresql+asyncpg://cashguard:cashguard_secret@localhost:<pg-port>/cashguard \
  PYTHONPATH=. .venv/bin/python -m alembic upgrade head
DATABASE_URL=postgresql+asyncpg://cashguard:cashguard_secret@localhost:<pg-port>/cashguard \
  REDIS_URL=redis://:cashguard_redis@localhost:<redis-port>/0 \
  APP_MODE=mock LIVE_TRADING_ENABLED=false \
  SECRET_KEY=<disposable> MASTER_KEY=<disposable> \
  PYTHONPATH=. .venv/bin/python -m app.db.seed

# 3. Real worker + real beat, both pointed at the disposable services only
DATABASE_URL=... REDIS_URL=... APP_MODE=mock LIVE_TRADING_ENABLED=false \
  .venv/bin/celery -A app.workers.celery_app worker --loglevel=INFO \
  --concurrency=1 --pool=solo > /tmp/worker.log 2>&1 &
DATABASE_URL=... REDIS_URL=... APP_MODE=mock LIVE_TRADING_ENABLED=false \
  .venv/bin/celery -A app.workers.celery_app beat --loglevel=INFO \
  --schedule=/tmp/beat-schedule --pidfile=/tmp/beat.pid \
  > /tmp/beat.log 2>&1 &

# 4. Optional immediate dispatch (do not wait 5 minutes if not needed):
#    celery_app.send_task("app.workers.tasks.run_strategy_signals")

# 5. Tear down: kill both processes, `docker rm -f` both containers.
```

**No real broker credentials, no real network call, and no live-money order
of any kind were involved in this procedure.**

## 5. Remaining gaps

**Update (2026-07-23):** the dry-run gap described below is now **fixed**.
`strategy_runner.py`'s two order-creation call sites now pass
`is_dry_run=(settings.APP_MODE == "mock")`, proven by a converted regression
test and a real service-level observation (Route A). A follow-up real-worker
observation (Route B) found a *second, separate, pre-existing* gap —
`MockMarketDataProvider` has no async-context-manager path, so
`MarketRegimeService` always reports `regime="unknown"` in `APP_MODE=mock`,
which blocks every live-routed strategy's entry signal at
`RiskEngine.check_market_conditions()`, before the (now-fixed) order-creation
step is ever reached. Full detail, evidence, and the precise follow-up: see
[`SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md`](SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md) §4.

**Update (2026-07-23), second time:** the market-regime gap above is now
**also fixed** — `MarketRegimeService._load_snapshots()` falls back to
`MockMarketDataProvider`'s existing `get_ohlcv()` method (mirroring
`strategy_runner.py::_fetch_market_context`'s own dual-path pattern) instead
of silently skipping snapshot collection for providers without an async
context manager. A real-worker Route B re-run (11 dispatches) confirmed the
old `"unknown market regime"` block never fires anymore, and that
`RiskEngine.check_market_conditions()` now correctly evaluates real (if
random, mock-mode) regime classifications — including legitimately blocking
`orb` during a `trending_down` regime, a different, working safety behaviour
rather than the old bug. No paper fill was reached in this re-run (unrelated
strategy-engine/mock-data randomness, not a code defect). Full detail:
[`SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md`](SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md) §4.4.

**Before an unattended automated paper trade:**

- This observation used the repo's seeded defaults: no enabled strategies,
  `auto_trading_enabled=False`. The safe no-op path (`skipped: "auto_trading_off"`)
  is now observed end-to-end through real beat/worker/Redis/Postgres.
  The *signal-generating* path was attempted in a follow-up session — see
  [`SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md`](SCHEDULED_SIGNAL_PAPER_FILL_OBSERVATION.md).
  ~~It found that an enabled, live-routed strategy does **not** currently
  reach a paper fill through this path — it deterministically errors inside
  a safety-policy gate, because `strategy_runner.py`'s order-creation calls
  are missing a dry-run flag that every other order-creation call site in
  the codebase already sets.~~ **Resolved 2026-07-23** — the missing
  dry-run flag was added; see that document §1-§3 for the fix and its
  regression-test/Route-A proof. ~~A real-worker (Route B) attempt
  afterward found the market-regime gap described in the update note above,
  which now blocks the *real, fully-unstubbed* process route one step later
  than before.~~ **Also resolved 2026-07-23** (second update) — see that
  document §4.4: `MarketRegimeService` now gets real mock-mode snapshot
  data, and a real-worker Route B re-run confirmed the `"unknown market
  regime"` block no longer fires. An actual paper fill was not reached in
  that re-run either, for a third, unrelated reason (mock strategy-engine
  randomness did not produce a qualifying breakout) — see §4.4 for detail.
  No live broker call results from any of the three findings.
- This was one supervised, short-lived run (~6 minutes), not a soak test.
  Long-running unattended behaviour (hours/days, restart/reconnect handling,
  Redis lock contention across multiple ticks) remains unobserved.

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
- The §4 observation session changed no runtime code, no beat schedule, no
  safety gate, and no default. It ran the repo's existing code, unmodified,
  against a throwaway environment, and recorded what happened.
