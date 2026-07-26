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

### 4.5 Update (2026-07-26): long-run Route B re-run — a second, distinct, deterministic blocker found

Status date: 2026-07-26. Source of truth: `origin/main` at
`f3e337509d66e6e807b6b1221e35bca1edb9e376` (the #214 merge; this session
makes no runtime code changes).

The §4.4 re-run's stated next step ("re-running Route B later in a session,
across more attempts, would be the natural next step") was followed up on:
a longer, more controlled Route B observation, run against a fresh pair of
disposable, uniquely-named Postgres/Redis containers
(`agent-a-longrun-postgres` on `localhost:25432`, `agent-a-longrun-redis` on
`localhost:26379` — distinct names/ports from every prior session's
containers and from the sibling `t212-cashguard-codex` worktree's `t212_*`
containers, confirmed unchanged, same `Exited` status, before and after).
Same environment shape as §4/§4.4: `APP_MODE=mock`, `LIVE_TRADING_ENABLED=false`,
`ENVIRONMENT=test`, `MARKET_DATA_PROVIDER=mock` (code default), no `T212_*`
or `KRAKEN_*` variable set at any point (verified by `env | grep` immediately
after sourcing the session's env file, before any app code ran).
`kill_switch_active=False`, `live_trading_unlocked=False`,
`auto_trading_enabled=True` throughout.

**Methodology change, declared before running, not tuned after seeing
results:** the prior Route B run's own write-up (§4.4) diagnosed its cause
of zero fills as "the mock strategy engine simply did not draw a qualifying
opening-range breakout in any of the 6 attempts where the regime permitted
it, likely compounded by how few today's-session bars existed this early
after market open." This session's one enabled/`is_live` ORB strategy
(`Agent A Long-Run Route B Observation ORB`, `NVDA`, seeded via a
scratchpad-only helper script, not committed) therefore changes exactly one
`OpeningRangeBreakoutStrategy` param from its `DEFAULT_PARAMS`:
`session_open_utc` is set ~3 hours before the seeding timestamp instead of
the real-session default `"14:30"`, so `StrategyRunner._extract_session_context`
(`strategy_runner.py:175-206`) — which filters `MockMarketDataProvider`'s
wall-clock-relative bars down to whatever falls after `session_open_utc` on
the most recent matching date — has a genuine multi-hour window of bars to
work with (36 bars observed throughout this run) instead of the ~5-25
minutes' worth the earlier run had. This comfortably clears
`OpeningRangeBreakoutStrategy`'s own `trend_ema_slow=21`-bar minimum for its
`require_trend` check, which the previous run's ~5-bar window never reached
(that check is skipped, not failed, below 21 bars — the previous run never
got a real answer from it either way). `min_rvol` is also relaxed `1.5 ->
1.0`, documented as: RVOL here is driven by `MockMarketDataProvider`'s
independent `random.randint(50000, 500000)` per-bar volume draw against the
20-bar trailing average, not a real intraday volume curve, so requiring
"at or above average" rather than "50% above average" is a reasonable
threshold for characterizing this data generator rather than real market
behavior. `session_open_utc` is a normal, already-existing, user-configurable
`Strategy.params` key (the exact key `strategy_runner.py:710` already reads
with a `"14:30"` fallback) — nothing was hard-coded into market data, no
risk gate, regime check, or strategy filter logic was edited, bypassed, or
loosened; both changes are strategy *input configuration*, applied the same
way for every one of this run's dispatches regardless of outcome.

**Dispatch.** Same task (`app.workers.tasks.run_strategy_signals`), same
real worker command as §4/§4.4
(`celery -A app.workers.celery_app worker --loglevel=INFO --concurrency=1
--pool=solo`), dispatched via `celery_app.send_task(...)` against the real
worker 40 times, ~75 seconds apart (comfortably past
`MarketRegimeService`'s 60-second cache TTL every time), spanning
2026-07-26T12:10:11Z to 2026-07-26T12:59:02Z (~49 minutes wall clock,
within the mission's 30-60 minute bound). Every one of the 40 dispatches
returned promptly (`result.get(timeout=90)` never raised) with
`{'strategies_run': 1, 'signals_generated': 0, 'orders_submitted': 0,
'risk_blocks': 0, 'errors': []}` — identical shape all 40 times.
`run_strategy_signals`'s operator/status readback
(`build_worker_health(db)`, the function `GET /v1/operator/status` calls)
showed `status="ok"`, `age_seconds=30` at read time immediately after the
40th dispatch — the same fresh-heartbeat contract as §4/§4.4/
`SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §4.4, across all 40 real runs,
not just one. A tripwire grep for
`Trading212Adapter|KrakenAdapter|trading212\.com|kraken\.com|api\.trading212`
across the full 40-dispatch worker log matched nothing. Final DB state after
all 40 dispatches: 0 `Signal` rows, 0 `Order` rows, 0 `RiskEvent` rows, 0
`AuditLog` rows — nothing was ever created, at any layer, in any of the 40
runs.

**risk_blocks=0 in all 40 dispatches** — a materially different result from
§4.4's 11-dispatch run (4/11 blocked, `trending_down` suppressing `orb`).
This run's `AppSettings.extra["market_intelligence_monitor"]["last_regime"]`
readback (taken after each dispatch, one call lagged from the regime the
concurrent dispatch actually evaluated, so treated here as directional, not
per-dispatch-exact) showed the same real distribution §4.4 found —
`ranging`, `trending_up`, `trending_down`, `risk_off` all observed — so
`MarketRegimeService` continues working as fixed in #214; `orb` simply
was not caught by a suppressing regime this time by chance across 40 draws.

### 4.6 The second blocker: a distinct, deterministic, structural gap — not the same one #214 fixed

This codebase has **two independent, same-named "regime" concepts**, and
this session's evidence disambiguates them for the first time in this
document:

1. `MarketRegimeService.evaluate()` (`app/services/market_regime.py`) — feeds
   `RiskEngine.check_market_conditions()` (`app/risk/engine.py`), the outer
   gate `_process_ticker` calls *before* `generate_signal()`. This is what
   §4.4 fixed and what `risk_blocks` in the task result counts.
2. `market_regime()` (`app/strategies/indicators.py:202-238`) — a
   **separate**, Choppiness-Index-based classifier called *only inside*
   `OpeningRangeBreakoutStrategy._check_filters()`
   (`app/strategies/orb_production.py:174,229`), using only the ticker's own
   OHLCV bars. It is unrelated to, and untouched by, the #214 fix.

With `risk_blocks=0` across the board this run, gate (1) was almost never
the obstacle. A read-only, non-DB, non-Celery diagnostic (a scratchpad-only
script, run alongside the real dispatch loop, calling the exact same
`MockMarketDataProvider.get_ohlcv()` and `OpeningRangeBreakoutStrategy`
production classes) sampled 300 independent 36-bar draws through the full
`generate_signal()` funnel:

| Stage | Count / 300 |
|---|---|
| No breakout above the opening-range high | 186 |
| Breakout, but `_check_filters` rejected | 114 |
| — rejected: RVOL < 1.0 | 59 |
| — rejected: `"Market choppy — skip"` | 55 |
| Passed both RVOL and choppy checks | **0** |
| Signal generated | **0 / 300** |

A follow-up, more targeted sample — calling `market_regime()`
(`indicators.py`) directly on 200 independent 36-bar
`MockMarketDataProvider.get_ohlcv("NVDA", ...)` draws, with no ORB filtering
at all — returned `"choppy"` **200 / 200 times**.

**Root cause.** `MockMarketDataProvider.get_ohlcv()`
(`app/market_data/mock_provider.py:74-103`) generates a purely driftless,
memoryless random walk: each bar's `open` is the prior bar's `close`, and
the new `close` is drawn uniformly inside a fresh, symmetric `±1%` band with
no momentum or autocorrelation term carried forward. The Choppiness Index
(`indicators.py:230`, `100 * ATR(14) * sqrt(14) / (14-bar high-low range)`)
is, by design, close to its maximum for exactly this kind of memoryless
walk: real (or realistically-simulated) trends have a persistent drift
component that widens the multi-bar high-low range faster than the
per-bar ATR grows, driving the index down; a symmetric, memoryless walk's
range grows in line with its own per-bar noise, keeping the index
structurally high. `_check_filters` (`orb_production.py:174-176`,
`:229-231`) unconditionally rejects `regime == "choppy"` for both the long
and short ORB paths. The 55/59 split between "choppy" and low-RVOL
rejections in the 300-sample funnel (summing to exactly the 114 breakout
draws, with zero surviving both) is consistent with this: RVOL is close to
an independent 50/50 draw (volume is i.i.d. uniform, unrelated to price),
while the choppy classification is *not* independent of anything price
related — it is, in this data generator, close to deterministic.

**This is data-shape, not a risk-gate bug, not a strategy-logic bug, and not
this session's config choice.** It reproduces identically regardless of
`session_open_utc` (36 bars here vs. ~5 in §4.4 — both still saw the same
outcome once enough bars existed to compute the index at all, i.e. `len(bars)
>= 30`, `indicators.py:211`), `min_rvol` (1.0 here vs. 1.5 in §4.4 — RVOL
was never the sole blocker in either), ticker, or dispatch count: no
parameter this session controls, and no parameter a real strategy owner
would tune per-deployment, changes the fact that `MockMarketDataProvider`'s
bars have no trend component for the Choppiness Index to detect as
non-choppy. `app/risk/engine.py` and `app/services/market_regime.py` — the
files #214 touched — are unrelated to this gap and were not touched this
session either. Fixing it would mean changing `MockMarketDataProvider` (to
add a momentum/drift component — but see §4.4's explicit reasoning for why
giving it new async-context-manager-adjacent behavior has a wide,
unintended blast radius across `position_monitor.py`,
`portfolio_execution_service.py`, `portfolio_attribution*.py`, and
`strategy_runner.py`'s own `_fetch_market_context` branch selection) or
`indicators.market_regime()`/`OpeningRangeBreakoutStrategy._check_filters()`
(which would change real production trading logic for a testing-data
limitation) — both are materially broader changes than this observation
session is scoped to, exactly the same category of judgment call §4.4 made
about `app/risk/engine.py`.

**Route C (real beat) was not attempted**, per the mission's own
instruction: Route C is only worth the 5-minute wait if Route B succeeded
and beat-cadence adds new evidence. Route B did not reach a fill this
session either, and the blocker found (§4.6) is deterministic and
data-shape-driven, not timing-driven — a real beat tick would dispatch the
identical task through the identical code path already exercised 40 times
via direct `send_task()` above, adding no new evidence.

**Teardown.** Worker process stopped
(`pkill -f "celery -A app.workers.celery_app worker"`, confirmed no process
remains); both disposable containers removed
(`docker rm -f agent-a-longrun-postgres agent-a-longrun-redis`); sibling
worktree's `t212_postgres`/`t212_redis`/`t212_worker`/`t212_beat`/
`t212_api`/`t212_web` containers confirmed unchanged (same `Exited` status,
consistent age progression) before and after this session.

**No real broker credentials, no real market-data credentials, no real
network call, and no live-money order of any kind were involved in this
procedure.**

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
- **Superseded 2026-07-26 (§4.5, §4.6):** the 2026-07-23 write-up above
  guessed the zero-fill cause was ordinary randomness compounded by too few
  session bars. A longer, 40-dispatch, ~49-minute real-worker run with a
  deliberately extended session window (36 session bars, well past the
  21-bar trend-check threshold) ruled that out and found the real cause:
  `OpeningRangeBreakoutStrategy._check_filters()`'s own internal Choppiness
  Index regime check (`app/strategies/indicators.py::market_regime()` — a
  second, separate "regime" concept from `MarketRegimeService`/`RiskEngine`,
  unrelated to and untouched by the #214 fix) classifies
  `MockMarketDataProvider`'s driftless, memoryless random-walk bars as
  `"choppy"` deterministically (200/200 in a direct sample), and
  `_check_filters` unconditionally rejects choppy regimes for `orb`. See
  §4.6 for the full root-cause analysis. **This is a data-shape limitation
  of `MockMarketDataProvider`, not a risk-gate bug, not a strategy-logic
  bug, and not fixable by any strategy param this session controls** (session
  window length, RVOL threshold, ticker, and dispatch count were all varied
  across §4.4/§4.5 without changing the outcome). It is out of this
  session's scope to fix (would mean changing either the shared mock
  provider or real ORB production filter logic for a testing-data
  limitation — a materially broader change, same category of judgment call
  §4.4 made about not touching `app/risk/engine.py`).
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
- **Partially resolved 2026-07-26 (§4.5):** a longer, 40-dispatch, ~49-minute
  supervised Route B run has now been observed (up from 11 dispatches/~6
  minutes in §4.4), with the operator/status heartbeat readback re-confirmed
  fresh after every dispatch and zero DB rows created at any point. This is
  still a supervised session, not an unattended multi-hour/multi-day soak —
  that remains unobserved.

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
