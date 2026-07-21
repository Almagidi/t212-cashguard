# Scheduled Signal-to-Paper-Fill Observation

Status date: 2026-07-21.
Source of truth: `origin/main` at `8f896eb4f65062016f1e3233a0500c99e6321d49`
(the #205 merge; unchanged by this update).

This document is scoped to the gap left open by
[`SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md`](SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md)
§5: that prior session observed a real Celery beat + worker firing
`run_strategy_signals` end-to-end, but only against the repo's seeded
defaults (no enabled strategies) — a safe no-op. It explicitly left "an
enabled strategy actually producing a signal through this same real-process
route, still ending in a paper-only fill" as unobserved.

This session attempted exactly that observation. **It found a real,
reproducible gap that currently prevents it from ever succeeding, and
stopped short of making the code change that would close it** (out of this
session's allowed-edit scope — see §4). It adds no new runtime behaviour.

**Live trading remains disabled and not live-ready.** `LIVE_TRADING_ENABLED`
defaults to `false`, `APP_MODE` defaults to `"mock"`
(`apps/api/app/core/config.py:37,135`). Nothing in this document, or in the
test it describes, changes either default or any broker/order/safety/
kill-switch implementation.

## 1. Route attempted

Per this session's route hierarchy, Route A (deterministic service-level
proof, real DB, no live broker) was attempted first, since it is the
narrowest, lowest-risk way to determine whether the higher-blast-radius
Routes B/C (real worker dispatch, real beat tick) would ever be able to
reach a fill. It was not necessary to proceed to Routes B/C: the blocker
found in Route A is inside `StrategyRunner` itself, so a real worker or beat
process would hit the identical exception — running the more expensive
routes would only re-demonstrate the same failure with more infrastructure,
which the mission's own route hierarchy says to avoid ("Do not force Route C
if it requires unsafe runtime changes").

**Route A, executed:** `StrategyRunner.run_all_enabled()` — the exact method
`app.workers.tasks.run_strategy_signals` calls — invoked directly against a
real SQLite test DB, with:

- A real, enabled (`is_enabled=True`), live-routed (`is_live=True`) strategy
  row (`type="orb"`), all gates open (`AppSettings.auto_trading_enabled=True`,
  `AppSettings.kill_switch_active=False`, `VenueConfig(venue="t212",
  auto_trading_enabled=True, kill_switch_active=False,
  degraded_mode_active=False)`).
- The real `OpeningRangeBreakoutStrategy` engine (via `_make_engine`), with
  relaxed params and a deterministic breakout bar sequence (only the
  market-data fetch, `_fetch_market_context`, is stubbed — a test-only
  monkeypatch, not a production change) so a "buy" entry signal is produced
  every run, not probabilistically.
- The real `MockBrokerAdapter` (`APP_MODE=mock` → `_get_broker()`'s
  documented short-circuit — unchanged, re-confirmed).
- The real `ExecutionEngine.create_order_intent()` / `.submit_order()`.
- The real `require_order_submission_allowed()` / `require_broker_environment()`
  safety-policy gates.

Only two subsystems unrelated to the order-submission path were stubbed —
`MarketIntelligenceMonitor` and `RiskEngine`'s scoring — matching the
precedent already established in `test_strategy_runner_provider_equivalence.py`
for testing `run_all_enabled()` without unrelated infrastructure (a
`RiskProfile` row, correlation/regime data) that has nothing to do with
order submission.

## 2. What was found: a real, pre-existing gap

**An enabled, `is_live=True` strategy that generates a real signal through
the real scheduled path does not produce a paper/mock fill in `APP_MODE=mock`
— it errors, and the resulting `Order` is orphaned at `status="pending_intent"`
forever.**

Root cause, verified by direct code reading (not just test output) and by
directly invoking the safety-policy function in a Python shell:

- `ExecutionEngine.create_order_intent()` defaults `is_dry_run=False`
  (`apps/api/app/execution/engine.py:101`).
- Four other order-creation call sites in this codebase explicitly override
  that default with `is_dry_run=(settings.APP_MODE == "mock")`:
  `apps/api/app/services/position_monitor.py:506,607`,
  `apps/api/app/api/v1/routes/orders.py:355`,
  `apps/api/app/services/system_control.py:246`,
  `apps/api/app/services/portfolio_execution_service.py:482`.
- **`apps/api/app/services/strategy_runner.py` does not** — its two
  `create_order_intent()` call sites (`_process_ticker:943`,
  `_check_exit:1087`) never pass `is_dry_run` at all, so every order a
  live-routed strategy creates through the scheduled path is `is_dry_run=False`.
- `ExecutionEngine.submit_order()` checks `is_dry_run` only *after* calling
  `require_order_submission_allowed()` (`apps/api/app/execution/engine.py:262-269`).
  That function's mock/paper-mode block
  (`apps/api/app/services/safety_policy.py:94-98`,
  `decision_code=f"{mode}_broker_block"`) fires unconditionally for any
  non-dry-run order when `APP_MODE` is `"mock"` or `"paper"` — before the
  dry-run check that would otherwise let it simulate a local fill.
- Net effect: the order is created (`status="pending_intent"`), then
  `submit_order()` raises `SafetyPolicyViolation("order submission blocked:
  APP_MODE=mock must not call real broker endpoints.",
  decision_code="mock_broker_block")` before any status transition happens.
  `_process_ticker`'s enclosing `except Exception` sets
  `Signal.status="error"` and returns — the `Order` row is never touched
  again and stays at `pending_intent` indefinitely.

This is **not** a live-broker call — the same gate that blocks the fill is
exactly what guarantees no live order is ever sent while `APP_MODE=mock`
(§3). It is a *missing* dry-run flag, not a *missing* safety check: the
safety check is doing exactly what it is supposed to do, given what
`strategy_runner.py` tells it.

**This means:** in the codebase as it currently exists, there is no DB
configuration, seeding, or disposable-environment setup that gets an enabled
live-routed strategy from the real scheduled path to a real paper fill.
Every attempt reaches the identical `mock_broker_block` error, deterministically.

## 3. No-live-broker proof (still holds)

Independent of the gap above, three redundant, independent gates still make
it structurally impossible for this codebase to place a live order while
`APP_MODE=mock` — none of them were touched, and the observation above
re-confirms rather than weakens this:

1. `StrategyRunner._get_broker()` (`apps/api/app/services/strategy_runner.py:102-106`)
   — hard early-return to `MockBrokerAdapter()` before any DB query or
   credential decryption when `APP_MODE=="mock"`.
2. `validate_broker_provider_request()` (`apps/api/app/broker/provider.py:139-142`)
   — the only function anywhere in the repo that constructs a real
   `Trading212Adapter` refuses to do so while `APP_MODE` is `"mock"`/`"paper"`.
3. `require_broker_environment()` (`apps/api/app/services/safety_policy.py:94-98`)
   — the same function that causes the §2 gap also independently blocks
   `ExecutionEngine.submit_order()`'s real-submission branch.

The audit trail from the new test (§4) shows exactly this: an
`order_blocked_by_runtime_policy` / `mock_broker_block` entry, and zero
`strategy_order_placed` entries — no successful submission of any kind,
mock or live.

## 4. Evidence: regression test, not a runtime fix

Per this session's constraints, `apps/api/app/services/strategy_runner.py`
is a runtime file outside the allowed-edit scope, and the instruction for
exactly this situation is to stop and report rather than change behaviour.
**No production code was changed.**

Instead, [`apps/api/tests/integration/test_scheduled_signal_paper_fill_gap.py`](../apps/api/tests/integration/test_scheduled_signal_paper_fill_gap.py)
was added (2 tests, both passing against current `origin/main`):

| Test | Proves |
|---|---|
| `test_scheduled_live_strategy_signal_errors_instead_of_paper_filling` | The full scenario in §2, end-to-end, with real `ExecutionEngine`/`MockBrokerAdapter`/safety-policy code: 1 signal generated, 0 orders submitted, `Signal.status="error"` with the exact `mock_broker_block` reason, `Order.status` stuck at `pending_intent`, matching `order_blocked_by_runtime_policy` audit log entry, and no `strategy_order_placed` entry anywhere. |
| `test_kill_switch_blocks_the_real_submission_path_independent_of_the_top_level_gate` | The kill switch defense-in-depth check inside `require_order_submission_allowed()` (independent of `run_all_enabled()`'s own top-level gate) still blocks the real submission path first, with a distinguishable `order_blocked_by_kill_switch` reason, when both are active. |

Both tests are written so that **if the missing `is_dry_run` guard is later
added to `strategy_runner.py`**, the first test's
`assert summary["orders_submitted"] == 0` (with an explanatory failure
message) will fail — forcing whoever makes that change to deliberately
update this test (and this document) to assert the new, fixed behaviour,
rather than the fix silently going unnoticed by the suite.

Full local validation (this worktree, current `origin/main` HEAD,
pinned `ruff 0.15.22`):

```
ruff check tests/integration/test_scheduled_signal_paper_fill_gap.py   → All checks passed!
ruff format --check ...same file...                                    → 1 file already formatted
pytest tests -q                                                        → 1722 passed, 4 skipped, 84.70% coverage
```

## 5. What remains unproven / next step

- **Unproven:** that an enabled strategy can reach an actual `Order.status=="filled"`
  row through the real scheduled path in `APP_MODE=mock`. As of this
  session, it cannot — see §2.
- **Proven, and newly so:** that when it fails, it fails *safely* — no live
  broker call, a clear audit trail, and (separately) that the kill switch
  still takes precedence over the same failure mode.
- **Precise next step for a maintainer** (out of this session's scope):
  add the same guard already used at the other four order-creation call
  sites to `apps/api/app/services/strategy_runner.py`'s two
  `create_order_intent()` calls (`_process_ticker:943`, `_check_exit:1087`):
  `is_dry_run=(settings.APP_MODE == "mock")`. This is a small, narrowly
  scoped change that makes `strategy_runner.py` consistent with the rest of
  the codebase's established dry-run convention — not a new pattern. After
  that change, `test_scheduled_live_strategy_signal_errors_instead_of_paper_filling`
  in this session's new test file should be updated to assert
  `orders_submitted == 1` and `Order.status == "filled"` instead of the
  current error-path assertions, and this document's §2/§5 should be
  updated to reflect the closed gap.
- Once that fix lands, the unattended Route B/C observation
  (`SCHEDULED_STRATEGY_DRY_RUN_OBSERVATION.md` §5) with an enabled strategy
  becomes meaningful to attempt — before this fix, it would only reproduce
  the same `mock_broker_block` error with more infrastructure and more risk
  surface, for no additional signal.

## 6. Kill-switch / safety status

- No safety, risk, kill-switch, or readiness gate was modified, loosened, or
  bypassed by this session. The only change is one new, additive test file.
- The gap identified in §2 is a **missing dry-run flag inside
  `strategy_runner.py`**, not a weakened safety check — `require_broker_environment()`
  and `require_order_submission_allowed()` behaved exactly as designed
  throughout.
- The kill switch's defense-in-depth check (independent of the top-level
  `run_all_enabled()` gate) was newly verified against the real
  `ExecutionEngine.submit_order()` path, not a stubbed one (§4, second test).
