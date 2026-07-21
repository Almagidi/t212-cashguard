# Paper-Trade Dry-Run Readiness — Evidence Matrix

Status date: 2026-07-21. Source of truth: `origin/main` at `ad5480151b01ff8c8b98345d1219ac9fadabf853`.

This document consolidates what is **currently proven** about the automated
paper-trade dry-run path, what is **not** proven, and what would need to be
true before (a) an unattended automated paper trade, and (b) the tiny
supervised live-money smoke test in
[`LIVE_SMOKE_TEST_RUNBOOK.md`](LIVE_SMOKE_TEST_RUNBOOK.md). It does not
introduce new runtime behaviour — it is a reading of the existing test suite
and service code, written down in one place. It complements, and does not
duplicate, [`paper-execution.md`](paper-execution.md) (the feature's design
and safety-convention doc).

**Live trading remains disabled.** `LIVE_TRADING_ENABLED` defaults to
`false` and `APP_MODE` defaults to `"mock"`
(`apps/api/app/core/config.py:37,135`). No change in this document alters
either default. **No real broker invocation has been validated or allowed**
by any test referenced below — every scenario in
`test_paper_dry_run_validation.py` runs against the in-memory SQLite `db`
fixture in `APP_MODE=mock`, with a monkeypatched tripwire that fails the test
if a Trading 212 or Kraken adapter constructor is ever called.

## 1. Validated evidence: the manual/API paper-only path

Merged as [#195](https://github.com/Almagidi/t212-cashguard/pull/195)
(2026-07-20), file `apps/api/tests/integration/test_paper_dry_run_validation.py`
(409 lines, 8 tests). Independently re-run in this worktree on 2026-07-21
against `origin/main` HEAD: **8/8 passed**, and the full backend suite
(**1710 passed, 4 skipped, 84.53% coverage**) is green, matching the PR's own
validation notes.

This suite proves the chain for the path that actually runs paper-only, in
isolation, with no broker construction:

`app/execution/paper_engine.py::PaperExecutionEngine.execute()` (reached via
the `/orders/paper` API surface, `paper_only` pinned `True` by schema).

| # | Test | Proves |
|---|------|--------|
| 1 | `test_paper_happy_path_produces_local_dry_run_order_without_broker` | An automation-sourced signal produces a local `filled` order with `is_dry_run=true`, `execution_environment=paper_mock`, no Trading212/Kraken adapter constructed, and a full `paper_signal_accepted → paper_risk_check_result → paper_order_created → paper_fill_simulated → paper_position_updated` audit trail, every row tagged `paper_only=true` / `no_broker_order_sent=true`. |
| 2 | `test_kill_switch_blocks_paper_execution_and_creates_no_order` | With the global kill switch active, `PaperExecutionEngine.execute()` raises before any `Order` row is created; audited with decision code `kill_switch_block`. |
| 3 | `test_kill_switch_skips_automated_strategy_runner_before_broker_lookup` | `StrategyRunner.run_all_enabled()` (the Celery-beat entry point — see §3) short-circuits with `skipped="kill_switch"` and never calls `_get_broker()` when the kill switch is active. |
| 4 | `test_paper_execution_refused_outside_mock_mode` | Paper execution is refused (403, `PAPER_MODE_BLOCK`) when `APP_MODE != "mock"`, even for a well-formed paper-only order. |
| 5 | `test_paper_sell_exceeding_position_is_blocked_before_fill` | An oversell is rejected (`paper_oversell_block`) before any fill; the prior buy position is left intact. |
| 6 | `test_paper_order_schema_forbids_non_paper_payloads` | The `PaperOrderCreate` schema rejects `paper_only=false` and unknown live-ish fields (e.g. `live=True`) at validation time, before any service code runs. |
| 7 | `test_demo_reconciler_refuses_paper_dry_run_order` | `DemoOrderReconciler.reconcile_order()` refuses a paper order at `demo_reconciliation_order_environment_block`, before any broker read — the real-broker demo reconciliation path can never ingest a paper fill. |
| 8 | `test_operator_paper_execution_summary_reflects_paper_fill` | `paper_execution_summary()` (the same aggregation the operator status endpoint serves) reports the fill and open position as `paper_only=true` / `no_broker_order_sent=true` with correct counts. |

These 8 tests satisfy this task's Phase 3 (paper-only path) and Phase 4
(kill-switch / risk-gate blocking) objectives in full. **No new PR was opened
for those phases in this session** — opening one would have duplicated #195.

## 2. Reconciliation and operator backend visibility

- **Isolation boundary** (`apps/api/app/services/demo_order_reconciliation.py`):
  `DemoOrderReconciler` gates on `execution_environment`. A paper order
  (`paper_mock`) is refused at `demo_reconciliation_order_environment_block`
  before any call to the broker's order-history endpoint — proven by test 7
  above.
- **Operator visibility** (`apps/api/app/api/v1/routes/operator.py`): the
  operator status endpoint surfaces `paper_execution_summary()` (test 8),
  the global and per-venue `kill_switch_active` flags, computed blocking
  reasons (`_compute_blocking_reasons`), and Celery beat/heartbeat schedule
  health (`_beat_entry`, `_scheduler_entry`, `_heartbeat_entry` — currently
  wired for the DCA planner and worker heartbeat beat entries, not for the
  `strategy-signals` entry specifically). This is read-only surfacing; the
  route adds no order-placement controls.

## 3. The automated Celery-beat path is a *separate* execution path — read carefully

`docs/LIVE_SMOKE_TEST_RUNBOOK.md` (added by #195) lists "a real
scheduler/Celery-beat dry run wired to the paper path" as an unproven gap.
That framing is slightly imprecise and worth correcting here: the wiring
**exists in code** and is **already unit-tested**, but it does not run
through `PaperExecutionEngine` at all.

- `celery_app.conf.beat_schedule["strategy-signals"]`
  (`apps/api/app/workers/celery_app.py:34-37`) fires
  `app.workers.tasks.run_strategy_signals` every 5 minutes.
- That task (`apps/api/app/workers/tasks.py:128-147`) acquires a Redis lock,
  opens a real `AsyncSessionLocal()` session, and calls
  `StrategyRunner(db).run_all_enabled()`.
- `run_all_enabled()` (`apps/api/app/services/strategy_runner.py:492`) checks
  kill switch / auto-trading / live-unlock flags first (test 3 above proves
  the kill-switch branch), then calls `self._get_broker()`
  (`strategy_runner.py:102`), which:
  - returns `MockBrokerAdapter()` when `APP_MODE == "mock"` (no network call —
    this is the mode the test suite and, per `config.py:37`, the default
    deployment run in);
  - otherwise looks up an active `BrokerConnection`, decrypts credentials,
    and constructs a **real** `Trading212Adapter` via
    `create_trading212_provider_adapter(...)` for whatever environment
    (`demo` or `live`) that connection is scoped to.
- Order submission for this path goes through
  `app/execution/engine.py::ExecutionEngine.submit_order()`
  (`strategy_runner.py:942,1086`) — a **different class** from
  `PaperExecutionEngine`. Individual strategies carry their own `dry_run`
  flag that gates whether `ExecutionEngine.submit_order()` is actually
  reached for that strategy.
- This broker-selection and dry-run-routing logic (mock vs. demo vs. live,
  `_get_broker()` behaviour, `ExecutionEngine` routing for dry-run vs. live
  entries) is already covered by the pre-existing
  `apps/api/tests/unit/test_strategy_runner_provider_equivalence.py`
  (15 tests, e.g. `test_get_broker_mock_mode_returns_mock_adapter_without_trading212_construction`,
  `test_run_all_enabled_demo_is_not_blocked_by_live_unlock_flag`,
  `test_process_ticker_live_entry_routes_order_through_execution_engine_only`) —
  it was not written for this task and is not new evidence from #195, but it
  is real, currently-passing coverage of the automated path's broker
  selection and dry-run gating.

**Net effect:** the "paper-only, no-broker" story proven by §1 is specific to
the manual/API `PaperExecutionEngine` path. The Celery-beat automated path is
a structurally separate path whose non-kill-switch behaviour depends on
`APP_MODE` / the active `BrokerConnection` environment, and whose
order-routing logic is unit-tested but has never been exercised end-to-end
through an actual running Celery beat + worker process, nor cross-referenced
against the paper-only narrative until this document.

## 4. What remains unproven

- **End-to-end scheduler firing.** No test starts a real `celery beat` +
  worker process and observes `run_strategy_signals` actually fire on its
  5-minute schedule and complete. Coverage stops at direct calls to
  `StrategyRunner.run_all_enabled()`; the Celery task function itself is not
  exercised via `.apply()`/task dispatch, only via the service method it
  wraps.
  This is an infra/process-level gap, not a logic gap: the underlying gates
  (kill switch, broker selection, dry-run routing) are unit- and
  integration-tested; the task wrapper (`task_lock`, `run_monitored_task`,
  retry/time-limit config) is not exercised by either suite.
- **Operator dashboard read of a scheduler-produced fill.** No test drives
  the automated path to completion and then reads the resulting state back
  through the operator status endpoint the way test 8 does for the manual
  paper path.
- **`strategy-signals` beat entry is not surfaced in operator status.** Only
  the DCA planner and worker-heartbeat beat entries are (`operator.py`
  `_scheduler_entry` / `_heartbeat_entry`); there is no equivalent read for
  the 5-minute strategy-signal schedule.
- **Live-broker interaction of any kind.** No test in this repository (new or
  pre-existing) makes a real network call to Trading 212 or Kraken. This
  document does not change that.

## 5. Can an automated paper trade be attempted now?

**Partial.** If "automated paper trade" means the manual/API paper-only path
(`POST /orders/paper` → `PaperExecutionEngine`) triggered by an external
automation client, that path is proven safe and functional today (§1). If it
means the actual Celery-beat scheduled strategy runner producing paper-mode
fills unattended, that requires `APP_MODE=mock` (so `_get_broker()` returns
`MockBrokerAdapter`) and has never been observed running end-to-end through a
live beat/worker process — the individual gates are unit-tested, but nobody
has watched the whole schedule fire and complete.

## 6. Blockers before the tiny supervised live-money smoke test

Unchanged from `LIVE_SMOKE_TEST_RUNBOOK.md` §2 — none of the work in this
session (or in #195) touches live-enablement prerequisites: broker
credentials/environment configuration, `live_trading_unlocked` attestation
flow, and recorded owner sign-off. Live trading remains disabled
(`LIVE_TRADING_ENABLED=False` default) and not live-ready.

## 7. Kill-switch / risk-gate status

- Global kill switch blocks the manual paper-only path before order creation
  (test 2) and short-circuits the automated runner before broker lookup
  (test 3).
- Mode gate (`PAPER_MODE_BLOCK`) blocks paper execution outside
  `APP_MODE=mock` (test 4).
- Position/oversell gate blocks an unsafe paper sell before fill (test 5).
- Schema gate rejects non-paper payloads before any service code runs
  (test 6).
- None of these gates were modified, loosened, or bypassed by this session's
  work; this document only records what already exists.
