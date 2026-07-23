# Scheduled Signal-to-Paper-Fill Observation

Status date: 2026-07-23 (updated same day, second time: the mock
market-regime gap found in §4 below is now fixed and re-observed via a new
Route B run — see §4.4).
Source of truth: `origin/main` at `e69710bad5b746b9991b5f48967662023af245c3`
(the #210 merge; unchanged by this update — #210 only touched
`apps/web/**` and `docs/OPERATOR_SCHEDULER_VISIBILITY.md` /
`docs/OPERATOR_UI_SAFETY_INVARIANTS.md`, none of which this session edits).

This document is scoped to the gap first identified in the prior revision of
this file (2026-07-21): an enabled, `is_live=True` strategy that generates a
real signal through the real scheduled path
(`app.workers.tasks.run_strategy_signals` -> `StrategyRunner.run_all_enabled()`)
did not produce a paper/mock fill in `APP_MODE=mock` — it errored inside
`require_order_submission_allowed()`'s mock-mode broker block, and the
resulting `Order` was orphaned at `status="pending_intent"` forever.

**That gap is now closed.** `apps/api/app/services/strategy_runner.py`'s two
`create_order_intent()` call sites (`_process_ticker`, `_check_exit`) now
pass `is_dry_run=(settings.APP_MODE == "mock")`, matching every other
order-creation call site in the codebase. This document records the fix,
the regression test that proves it, and two real-process observations
(Route A and Route B) run against it.

**Live trading remains disabled and not live-ready.** `LIVE_TRADING_ENABLED`
defaults to `false`, `APP_MODE` defaults to `"mock"`
(`apps/api/app/core/config.py:37,135`). Nothing in this document, the fix it
describes, or the tests it references changes either default or any broker/
order/safety/kill-switch implementation.

## 1. The fix

`apps/api/app/services/strategy_runner.py`, two call sites:

- `_process_ticker` (entry orders, ~line 943): added
  `is_dry_run=(settings.APP_MODE == "mock")`.
- `_check_exit` (exit orders, ~line 1087): added
  `is_dry_run=(settings.APP_MODE == "mock")`.

This is the exact pattern already used at every other order-creation call
site in the codebase (`apps/api/app/services/position_monitor.py:506,607`,
`apps/api/app/api/v1/routes/orders.py:359`,
`apps/api/app/services/system_control.py:246`). The diff is two lines, one
keyword argument each — no other line in `strategy_runner.py` changed. In
non-mock modes (`paper`, `demo`, `live`) the expression evaluates to
`False`, identical to the previous unconditional default — **no behaviour
change outside `APP_MODE=mock`.**

No safety-policy, broker-adapter, execution-engine, or strategy-signal code
was touched. `require_order_submission_allowed()`
(`apps/api/app/services/safety_policy.py:173-237`) is unmodified: it still
checks the kill switch *before* the `if order.is_dry_run: return`
short-circuit, so is_dry_run continues to have zero effect on kill-switch
enforcement (see §2, kill-switch test).

## 2. Regression tests: blocker test converted to success-path proof

[`apps/api/tests/integration/test_scheduled_signal_paper_fill_gap.py`](../apps/api/tests/integration/test_scheduled_signal_paper_fill_gap.py)
(2 tests, both passing against this fix, real DB, real `ExecutionEngine`,
real `safety_policy` gates, real `MockBrokerAdapter` — nothing in the
order-creation/execution/safety-policy layer is mocked or stubbed):

| Test | Proves |
|---|---|
| `test_scheduled_live_strategy_signal_reaches_mock_paper_fill` (renamed from `test_scheduled_live_strategy_signal_errors_instead_of_paper_filling`) | An enabled, `is_live=True` strategy reaching a real signal through `StrategyRunner.run_all_enabled()` now ends with `orders_submitted=1`, `Signal.status="executed"`, `Order.status="filled"`, `Order.is_dry_run=True`, `Order.broker_response={"dry_run": true, "simulated": true}` and no `broker_order_id` (proof the real-submission branch and its `broker.place_*_order()` calls were never reached), an `order_submitted`/`decision=simulated` safety audit entry, and a `strategy_order_placed` audit entry. `Trading212Adapter`, `KrakenAdapter`, and the T212 provider factory are sentineled to raise if constructed — none do. The previously-firing `order_blocked_by_runtime_policy`/`mock_broker_block` audit entry no longer appears. |
| `test_kill_switch_blocks_the_real_submission_path_independent_of_the_top_level_gate` (assertions extended) | With the kill switch active, the real `ExecutionEngine.submit_order() -> require_order_submission_allowed()` path still blocks first (`order_blocked_by_kill_switch`), even though the order was created with `is_dry_run=True` (`APP_MODE=mock`) — proving is_dry_run does not bypass the kill switch, only the broker-environment/live-readiness checks that come after it in that function. |

"Non-mock mode still does not become implicitly dry-run" is proven in
[`apps/api/tests/unit/test_strategy_runner_provider_equivalence.py`](../apps/api/tests/unit/test_strategy_runner_provider_equivalence.py):
its `APP_MODE="demo"` fixture now asserts `is_dry_run: False` in the
captured `create_order_intent()` kwargs at both call sites
(`test_process_ticker_live_entry_routes_order_through_execution_engine_only`,
`test_check_exit_live_routes_sell_order_through_execution_engine_only`) —
both updated as part of this fix, since they previously asserted the exact
kwargs dict *without* an `is_dry_run` key, which the fix now always adds.

Full local validation (this worktree, `fix/strategyrunner-mock-dry-run-orders`
branched from `origin/main` at `bef604b399e6906353d4ca3997be98b3baa0f622`,
pinned `ruff 0.15.22`):

```
pytest tests/integration/test_scheduled_signal_paper_fill_gap.py \
       tests/integration/test_paper_dry_run_validation.py \
       tests/unit/test_run_strategy_signals_worker.py \
       tests/unit/test_strategy_runner_provider_equivalence.py \
       tests/unit/test_strategy_runner_helpers.py \
       tests/unit/test_strategy_runner_daily_loss_gate.py -q --no-cov
  → 90 passed, 1 skipped

ruff check app/services/strategy_runner.py \
           tests/integration/test_scheduled_signal_paper_fill_gap.py \
           tests/unit/test_strategy_runner_provider_equivalence.py
  → 1 pre-existing finding (TC006, line 139, inside _get_broker's non-mock
    branch — confirmed present on origin/main before this change, unrelated
    to it, not touched)

ruff format --check ...same 3 files...  → 3 files already formatted

mypy app/services/strategy_runner.py \
     tests/integration/test_scheduled_signal_paper_fill_gap.py \
     tests/unit/test_strategy_runner_provider_equivalence.py \
     --ignore-missing-imports --follow-imports=silent
  → 38 errors (baseline on the pre-fix versions of these same 3 files: 34;
    the +4 are `dict[str, Any] | None` "not indexable" findings on the new
    success-path test's payload[...] assertions, the exact same pre-existing
    convention already used elsewhere in this file, e.g. the kill-switch
    test's `kill_switch_audit.payload["decision"]` — not a new class of
    issue)

pytest tests -q  → 1722 passed, 4 skipped, 84.67% coverage
```

## 3. Route A — service-level observation (executed, deterministic)

The regression test in §2 *is* Route A: `StrategyRunner.run_all_enabled()` —
the exact method the Celery task calls — invoked against a real SQLite test
DB, with a real, enabled, `is_live=True` ORB strategy, relaxed params, and a
deterministic breakout bar sequence (only `_fetch_market_context`, the
market-data fetch, is stubbed — a test-only monkeypatch, not a production
change). Evidence is the full assertion set in §2's table; it is not
duplicated here. Result: **signal generated, mock/paper fill reached, full
audit trail, no live broker adapter constructed (sentineled), kill switch
independently re-verified to still block.**

## 4. Route B — real Celery worker dispatch (executed; new finding)

Since Route A succeeded, Route B was attempted: a real Celery worker process,
pointed at disposable, uniquely-named Postgres/Redis containers
(`agent-a-fill-postgres` on `localhost:15432`, `agent-a-fill-redis` on
`localhost:16379` — distinct names and ports from the pre-existing, already-
stopped `t212_postgres`/`t212_redis` containers belonging to the sibling
`t212-cashguard-codex` worktree, which were confirmed unchanged — same
`Exited` status, same age — before and after this session).

**Environment:** fresh Postgres 16 (`alembic upgrade head`, unmodified
migration chain), seeded with the repo's own `python -m app.db.seed` plus
one additional, deterministic, enabled/`is_live=True` ORB strategy
(`allowed_tickers=["NVDA"]`, relaxed entry-filter params) and
`AppSettings.auto_trading_enabled=True` /
`VenueConfig(t212).auto_trading_enabled=True` (both otherwise seed
`False`). `kill_switch_active=False`, `live_trading_unlocked=False`
throughout. `APP_MODE=mock`, `LIVE_TRADING_ENABLED=false`,
`ENVIRONMENT=test`; no `T212_*` or `KRAKEN_*` variable was set at any point.
`MARKET_DATA_PROVIDER` was left at its code default (`"mock"` —
`apps/api/app/core/config.py:103`), so the real, unstubbed
`MockMarketDataProvider` supplied all market data — no live market-data
network call either.

**Dispatch:** `celery_app.send_task("app.workers.tasks.run_strategy_signals")`
against a real worker (`celery -A app.workers.celery_app worker
--loglevel=INFO --concurrency=1 --pool=solo`):

```
TASK_ID    12580e2f-799f-4e57-99e9-31fbb57c4056
TASK_RESULT {'strategies_run': 1, 'signals_generated': 0, 'orders_submitted': 0,
             'risk_blocks': 1, 'errors': []}
```

Worker log:
```
runner.intelligence_block  reason='Strategy entries blocked: unknown market regime.'
                           strategy='Agent A Route B Observation ORB' ticker=NVDA
tasks.signals_complete     errors=[] orders_submitted=0 risk_blocks=1
                           signals_generated=0 strategies_run=1
```
`RiskEvent` table: `regime_block`, `NVDA`, `"Strategy entries blocked: unknown
market regime."`, `payload={'regime': 'unknown', 'strategy_type': 'orb'}`.
A tripwire grep for `Trading212Adapter|KrakenAdapter|trading212\.com|
kraken\.com|api\.trading212` across the full worker log matched nothing.
Operator readback (`build_worker_health(db)`, the same function
`GET /v1/operator/status` calls internally) showed
`run_strategy_signals` transition to `status="ok"`, `age_seconds=37` at read
time — the same fresh-heartbeat contract documented in
`SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §4.4.

### 4.1 What this found: a second, pre-existing, unrelated gap

The dispatch did not reach a fill — not because of the `is_dry_run` fix (it
never got that far), but because of a **separate, structural, pre-existing
gap in mock-mode market-regime detection**, unrelated to this session's
change:

- `StrategyRunner._process_ticker` calls
  `risk.check_market_conditions(market_regime=intelligence.get("regime"), ...)`
  (`strategy_runner.py:754`) *before* calling `engine.generate_signal(...)`
  (`strategy_runner.py:770`).
- `RiskEngine.check_market_conditions()`
  (`apps/api/app/risk/engine.py:385-453`) unconditionally raises
  `RiskViolation("Strategy entries blocked: unknown market regime.",
  "regime_block")` whenever the computed regime is `"unknown"`.
- `MarketRegimeService.evaluate()` (`apps/api/app/services/market_regime.py:71-85`)
  returns `regime="unknown"` whenever `_load_snapshots()` returns an empty
  list.
- `_load_snapshots()` (`market_regime.py:156-183`) only populates benchmark
  (SPY/QQQ/IWM) snapshots inside an `if hasattr(provider, "__aenter__"):`
  branch — i.e. only for async-context-manager providers (Alpaca/Polygon).
  `MockMarketDataProvider` (the provider `APP_MODE=mock` uses) does not
  implement `__aenter__`, so this branch is always skipped in mock mode and
  `snapshots` is always `[]`.

**Net effect: in `APP_MODE=mock`, `MarketRegimeService.evaluate()`
deterministically returns `regime="unknown"` on every call, and
`check_market_conditions()` therefore deterministically blocks every
live-routed strategy's entry signal, for every ticker, every time** — before
`generate_signal()` (and therefore before `create_order_intent()`,
`is_dry_run`, or anything this session's fix touches) is ever reached. This
is independent of which strategy, ticker, or market data is fed in; a second
dispatch (with an accidental duplicate strategy row, cleaned up
immediately after) reproduced the identical `regime_block` a second time.

This is **not a live-broker call**, **not a weakened safety check**, and
**not caused or affected by this session's `is_dry_run` fix** — the
`RiskEngine` gate is doing exactly what it is designed to do (refuse to
trade without a classified market regime); it simply has no data source to
classify a regime from in mock mode. It is, however, a real blocker to
observing an actual scheduled signal-to-fill through a real, fully-unstubbed
worker process, and is out of this session's allowed-edit scope
(`app/risk/engine.py` and `app/services/market_regime.py` are not in Agent
A's allowed-files list, and fixing either would be a materially different,
broader change than the one this session is scoped to).

### 4.2 Why Route C (real beat) was not attempted

`regime_block` is deterministic and ticker/strategy-independent — a real
`celery beat` tick would dispatch the identical task, through the identical
`RiskEngine.check_market_conditions()` call, and hit the identical block.
Running Route C would only re-demonstrate the same finding with more
infrastructure and a longer wait (the real 5-minute cadence), for no
additional signal — the same reasoning the prior Route-A-only session in
this document's previous revision used to justify not attempting Routes B/C
before this fix landed. Route C is therefore documented as **not attempted,
by design**, pending the separate market-regime-detection gap being closed.

### 4.3 Teardown

The Celery worker process was stopped (`pkill -f "celery -A
app.workers.celery_app worker"`); both disposable containers were stopped
and removed (`docker rm agent-a-fill-postgres agent-a-fill-redis`). The
pre-existing, already-stopped `t212_postgres`/`t212_redis`/`t212_worker`/
`t212_beat`/`t212_api`/`t212_web` containers belonging to the sibling
worktree's project were confirmed unchanged (same `Exited` status, same
age) before and after this session.

**No real broker credentials, no real market-data credentials, no real
network call, and no live-money order of any kind were involved in this
procedure.**

### 4.4 Update (2026-07-23): the market-regime gap is now fixed, Route B re-run

**The fix.** `apps/api/app/services/market_regime.py`, `_load_snapshots()`
only: when `get_live_provider()` returns a provider with no
`__aenter__`/`__aexit__` (i.e. `MockMarketDataProvider` in `APP_MODE=mock`),
it now falls back to that provider's existing, already-used-elsewhere
`get_ohlcv()` method to build the same `_SeriesSnapshot` objects, instead of
silently skipping snapshot collection entirely. This mirrors the identical
`hasattr(provider, "__aenter__")` dual-path pattern
`strategy_runner.py::_fetch_market_context` already uses for the same
mock-vs-real distinction. **`MockMarketDataProvider` itself is untouched** —
it deliberately still has no `__aenter__`/`__aexit__`, because giving it one
would silently flip `_fetch_market_context`'s own branch selection too (used
by `position_monitor.py`, `portfolio_execution_service.py`,
`portfolio_attribution*.py`, and `strategy_runner.py` itself), a much wider
blast radius than this fix needs. `app/risk/engine.py` is untouched;
unknown/invalid regimes are still blocked exactly as before.

**Regression tests** (`apps/api/tests/unit/test_market_regime.py`, 3 new
tests, all passing against the full 1725-test suite):

- `test_market_regime_service_mock_provider_produces_real_snapshots_not_unknown`
  — the real `MockMarketDataProvider` (not a test fake), via the real
  `get_live_provider()`, no longer yields `regime="unknown"`.
- `test_market_regime_service_mock_provider_has_no_async_context_manager` —
  locks in the chosen fix shape (see above) as a regression guard.
- `test_real_mock_regime_feeds_risk_engine_without_regime_block` — chains
  the real `MarketRegimeService.evaluate()` output into the real
  `RiskEngine.check_market_conditions()` (the exact call
  `strategy_runner.py:754` makes before signal generation) and confirms it
  is never blocked for being unknown/invalid. Handles `high_volatility`
  (the one regime `RiskEngine` blocks unconditionally) as the sole tolerated
  exception, since `MockMarketDataProvider`'s underlying price walk is
  shared, mutable module state that drifts across the whole test session —
  this test cannot assume which *trusted* regime a given run lands on, only
  that it lands on one.

Existing coverage unaffected and reconfirmed: `unknown` regime still returned
for genuinely insufficient snapshot data
(`test_market_regime_service_missing_data_is_unknown`, unchanged, still
passing — its `FakeProvider("empty")` has `__aenter__`, so it still takes
the async branch, untouched by this fix); `RiskEngine` still blocks
unknown/invalid/high-volatility regimes
(`test_risk_engine_blocks_untrusted_market_regime_states`,
`test_risk_engine_blocks_unsafe_market_conditions`, unchanged); kill switch
still blocks (`test_kill_switch_blocks_the_real_submission_path_independent_of_the_top_level_gate`,
unchanged); Route A (§2, §3 above) unchanged and still green.

**Route B, re-run.** Same disposable-container procedure as §4 above, new
containers (`agent-a-regime-postgres` on `localhost:15432`,
`agent-a-regime-redis` on `localhost:16379`; the sibling worktree's
`t212_postgres`/`t212_redis`/etc. confirmed unchanged, same `Exited` status
and age, before and after). Same seed shape as §4 (one enabled, `is_live`
ORB strategy on NVDA with relaxed entry-filter params,
`auto_trading_enabled=True`, `VenueConfig(t212).auto_trading_enabled=True`,
`kill_switch_active=False`, `live_trading_unlocked=False`). Same real worker
(`celery -A app.workers.celery_app worker --loglevel=INFO --concurrency=1
--pool=solo`), same dispatch method
(`celery_app.send_task("app.workers.tasks.run_strategy_signals")`).

Dispatched 11 times over ~6 minutes (spaced to cross the regime service's
60-second cache TTL at least twice, so more than one independently-computed
regime value was observed):

```
TASK_RESULT (1st dispatch)  {'strategies_run': 1, 'signals_generated': 0, 'orders_submitted': 0, 'risk_blocks': 0, 'errors': []}
TASK_RESULT (dispatches 2-5, same 60s regime-cache window) {'risk_blocks': 1, ...} x4
TASK_RESULT (dispatches 6-11, after cache expiry + more of today's session elapsed) {'risk_blocks': 0, ...} x6
```

**The old bug never fired, in any of the 11 dispatches:** `grep -c "unknown
market regime" worker.log` → `0`. Worker-observed regime values (read back
from `AppSettings.extra["market_intelligence_monitor"]["last_regime"]`,
which `MarketIntelligenceMonitor.evaluate_and_alert()` persists on every
run) were real, trusted classifications — `trending_up` and `trending_down`
were both observed across the 11 dispatches, driven by
`MockMarketDataProvider`'s underlying random walk. When the regime was
`trending_down`, `RiskEngine` correctly, legitimately blocked the ORB
strategy — `trending_down` suppresses `orb` by design
(`MarketRegimeService._strategy_policy`) — logged as
`runner.intelligence_block reason='Strategy orb blocked in trending_down
regime.'` and recorded as 4 `RiskEvent` rows,
`event_type="regime_block"`, `payload={'regime': 'trending_down',
'strategy_type': 'orb', 'suppressed_strategies': ['closing_momentum',
'intraday_periodicity', 'orb']}`. This is the risk engine working exactly
as designed, not a bug, and a categorically different message/payload than
the old `"Strategy entries blocked: unknown market regime."` /
`{'regime': 'unknown', ...}`.

**No paper fill was reached this session.** In the 6 dispatches where the
regime permitted `orb` (`risk_blocks=0`), `check_market_conditions()` passed
and `engine.generate_signal(...)` was reached and called — but returned
`None` every time: `MockMarketDataProvider.get_ohlcv()`'s random-walk bars
did not happen to produce a qualifying opening-range breakout in any of
those 6 draws. This is expected, unrelated stochastic strategy-engine
behaviour, not a regression or a new blocker: `_extract_session_context`
correctly found only a handful of real, wall-clock-timestamped bars
available this early into today's trading session (dispatches ran
~14:53-14:57 UTC, session opens 14:30 UTC), leaving little room for a clear
breakout to form. `signals_generated=0`/`orders_submitted=0` in every
dispatch; no `Signal` or `Order` row was created (no row is created until
`generate_signal()` returns non-`None`); the deterministic, guaranteed-fill
path from a controlled bar sequence is already proven separately at Route A
(§2, §3) and does not depend on this randomness.

Operator readback (`build_worker_health(db)`) showed `run_strategy_signals`
at `status="ok"`, `age_seconds=24` at read time, same fresh-heartbeat
contract as §4 and `SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §4.4. A
tripwire grep for `Trading212Adapter|KrakenAdapter|trading212\.com|
kraken\.com|api\.trading212` across the full worker log matched nothing.

**Route C (real beat) was not attempted.** `run_strategy_signals` is on a
300-second beat cadence; a real beat tick would dispatch the identical task
through the identical code path already exercised 11 times via direct
`send_task()` above. It would re-confirm beat-scheduling infrastructure
(already proven in `SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §4) but add
no new evidence about the regime fix specifically, for a 5-minute-per-tick
wait. Documented as not attempted, by design, same reasoning §4.2 used for
the pre-fix Route C decision.

**Teardown:** worker process stopped
(`pkill -f "celery -A app.workers.celery_app worker"`); both disposable
containers stopped and removed
(`docker rm agent-a-regime-postgres agent-a-regime-redis`); sibling
worktree's `t212_*` containers confirmed unchanged before/after.

## 5. Remaining gaps

**Before an unattended automated paper trade:**

- **Resolved 2026-07-23 (§4.4):** the market-regime gap described in the
  original revision of this section — `MockMarketDataProvider` had no
  `__aenter__`, so `MarketRegimeService._load_snapshots()` always returned no
  benchmark data and `evaluate()` always returned `regime="unknown"` in
  `APP_MODE=mock`, unconditionally blocking every live-routed strategy's
  entry signal at `RiskEngine.check_market_conditions()` before order
  creation — is now fixed. See §4.4 for the fix, regression tests, and the
  re-run Route B observation proving the old `"unknown market regime"` block
  no longer fires (0 occurrences across 11 real-worker dispatches).
- **New, smaller remaining gap:** Route B did not reach an actual paper fill
  this session. Not because of the regime fix (proven closed above) or the
  `is_dry_run` fix (proven separately at Route A) — the mock strategy engine
  simply did not draw a qualifying opening-range breakout in any of the 6
  attempts where the regime permitted it, likely compounded by how few
  today's-session bars existed this early after market open at dispatch
  time. Re-running Route B later in a session (more elapsed session bars)
  or across more attempts would be the natural next step; this is ordinary
  strategy-engine/mock-data randomness, not a code defect, and is not
  believed to require any further code change.
- The `is_dry_run` fix itself **is** proven, deterministically, at the
  service level (§2, §3) — an enabled strategy that reaches
  `generate_signal()` and creates an order intent in `APP_MODE=mock` now
  reaches a real paper fill. What Route B additionally establishes is that
  the real Celery worker/task-lock/session wrapper around that logic is
  unaffected by the fix and still forwards the (different, unrelated)
  `regime_block` outcome safely, with a real audit trail and no live broker
  call, exactly as it forwarded `mock_broker_block` before the fix and
  `auto_trading_off` in the no-strategies-enabled case
  (`SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §4.2).
- This was one supervised, short-lived Route B run, not a soak test.
  Long-running unattended behaviour remains unobserved (unchanged from the
  prior revision of this document).

**Before the tiny supervised live-money smoke test**
(`LIVE_SMOKE_TEST_RUNBOOK.md`): unchanged. Nothing in this session's fix or
observation touches live-enablement prerequisites — broker credentials/
environment configuration, the `live_trading_unlocked` attestation flow, or
recorded owner sign-off. Live trading remains disabled
(`LIVE_TRADING_ENABLED=False` default) and not live-ready.

## 6. Kill-switch / safety status

- No safety, risk, kill-switch, or readiness gate was modified, loosened, or
  bypassed by this session or by the §4.4 update. The §4.4 runtime change is
  confined to `MarketRegimeService._load_snapshots()`
  (`app/services/market_regime.py`) — it supplies real mock market data
  through the same code path production providers already use, so the risk
  engine has something legitimate to evaluate; it does not change what the
  risk engine does with that evaluation. `app/risk/engine.py` is untouched
  by either update in this document.
- The kill switch's defense-in-depth check inside
  `require_order_submission_allowed()` was re-verified, against the real
  `ExecutionEngine.submit_order()` path and a real `is_dry_run=True` order,
  to still block first (§2, second test).
- The market-regime block itself is an *existing*, unmodified safety gate
  behaving as designed (refusing to trade on an unclassified regime or a
  regime that suppresses the given strategy); §4.4 gives it real data to
  classify from in mock mode, it does not touch, loosen, or work around the
  gate's logic. §4.4's Route B re-run directly demonstrates the gate still
  firing correctly and legitimately for a *different*, real reason
  (`trending_down` suppressing `orb`) once it has real data to work with.
