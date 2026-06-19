# Portfolio Attribution Duplication — Investigation

**Status:** Investigation only. No code changed by this report.
**Scope:** `app/services/portfolio_attribution.py` vs
`app/services/portfolio_attribution_service.py`.
**Branch:** `chore/investigate-attribution-duplication`.

## Summary

Two service modules under `apps/api/app/services/` both define classes named
`PortfolioAttributionService` and `PositionLedger`:

| Module | Role | Public entry points | Return types | Wired into |
| --- | --- | --- | --- | --- |
| `portfolio_attribution_service.py` (582 lines) | **LIVE** | `build_summary()`, `build_strategy_attribution()` | `app.api.schemas` `*Out` models | API routes + integration test + 1 unit test |
| `portfolio_attribution.py` (363 lines) | **Legacy / test-only** | `build_for_strategy()` | local dataclasses (`SleeveAttribution`, …) | only its own unit test |

Despite the shared class names, the two are **not interchangeable**: they expose
different method names and different return types. A naive "merge" or symbol-swap
is not possible without a rewrite.

## Evidence

### Live module — `portfolio_attribution_service.py`

Imported and instantiated by the API routes:

- `app/api/v1/routes/strategies.py:42` — `from app.services.portfolio_attribution_service import PortfolioAttributionService`
- `strategies.py:437` (`list_portfolio_attribution`) → `await attribution.build_summary(strategy)` (`strategies.py:444`)
- `strategies.py:562` (`get_portfolio_attribution`) → `await attribution.build_strategy_attribution(strategy)` (`strategies.py:563`)

Returns API schema objects directly (`app/services/portfolio_attribution_service.py:20-27`):
`PortfolioRebalanceEventOut`, `PortfolioRebalanceWeightChangeOut`,
`PortfolioStrategyAttributionOut`, `PortfolioStrategyAttributionSummaryOut`,
`PortfolioTickerAttributionOut`, `PortfolioTimelinePointOut`.

Test coverage:
- Integration: `tests/integration/test_api.py:1292` `test_list_portfolio_attribution`
  (patches `portfolio_attribution_service.get_live_provider`, exercises the live route).
- Unit: `tests/unit/test_portfolio_attribution_service.py` — **1** test
  (`test_portfolio_attribution_replays_rebalance_orders`).

### Legacy / test-only module — `portfolio_attribution.py`

- Imported **only** by `tests/unit/test_portfolio_attribution.py:15`.
- No route, service, `services/__init__.py` re-export, or runtime caller imports it.
- Its unique public surface — `build_for_strategy()`, and dataclasses
  `SleeveOrderFill`, `TimelinePoint`, `TickerAttribution`, `SleeveAttribution` —
  has **zero** references outside the module itself and its test.
- Test coverage: `tests/unit/test_portfolio_attribution.py` — **36** tests.

### Shared symbol names (collision, not reuse)

- `PortfolioAttributionService` is defined twice:
  `portfolio_attribution.py:91` and `portfolio_attribution_service.py:59`.
- `PositionLedger` is defined twice:
  `portfolio_attribution.py:40` and `portfolio_attribution_service.py:53`.

The collision is harmless at runtime because the modules are imported by distinct
fully-qualified paths; nothing imports both `PortfolioAttributionService` symbols
into the same namespace.

### Git history

Both files first appear in the same squashed commit
`0462579 "Initial project scaffold and test artifacts" (2026-04-24)` and **neither
has been modified since**. History therefore does not establish one as newer; the
live/legacy distinction rests entirely on current usage (routes use `_service.py`).

## Behavioural differences

| Aspect | `portfolio_attribution_service.py` (live) | `portfolio_attribution.py` (legacy) |
| --- | --- | --- |
| Entry method | `build_summary`, `build_strategy_attribution` | `build_for_strategy` |
| Output | API `*Out` Pydantic schemas | plain dataclasses (`SleeveAttribution`) |
| Schema coupling | imports `app.api.schemas` | none |
| Extra features | benchmark positions, weight-change snapshots, summary view | timeline replay + ticker attribution dataclasses |
| Fill source | `_load_rebalance_fills` (`RebalanceFill`) | `_load_order_fills` (`SleeveOrderFill`) |

Both depend on the same DB models (`Order`, `Signal`, `Strategy`) and the same
market-data provider (`app.market_data.get_live_provider`) and `Bar` indicator type.

## Conclusions

1. **Live implementation:** `portfolio_attribution_service.py`. It backs the two
   portfolio-attribution API routes and has integration coverage.
2. **Legacy / test-only implementation:** `portfolio_attribution.py`. Reachable
   only from its own 36-test unit suite; no runtime path.
3. **Is deletion/consolidation safe now?** Deleting the legacy module **and** its
   test is **runtime-safe** (no production import), but it is **not coverage-safe
   as-is**: it would drop 36 unit tests covering ledger/replay/attribution math,
   while the live module currently has only 1 dedicated unit test. Removing the
   legacy module without porting coverage would be a net **reduction** in tested
   attribution logic. Deletion is therefore **not recommended in a single step**.
4. The two cannot be "merged" by symbol substitution — different method names and
   return types. Any consolidation is effectively: keep the live module, salvage
   the legacy module's *test scenarios* against the live API, then remove the
   legacy module.

## Recommended follow-up (two separate PRs, each individually approved)

**PR 1 — strengthen live coverage (additive, no deletion):**
- Add unit tests targeting `portfolio_attribution_service.PortfolioAttributionService`
  that reproduce the *intent* of the legacy suite against the live API
  (`build_strategy_attribution` / `build_summary`): position-ledger accumulation,
  multi-fill replay, benchmark positioning, weight-change snapshots, and the
  empty/edge-case branches.
- Files: `tests/unit/test_portfolio_attribution_service.py` (extend) — no source change.
- Gate: full backend suite green; confirm the new tests fail if the live logic is
  perturbed (real coverage, not smoke).

**PR 2 — remove the legacy module (deletion-only, after PR 1 merges):**
- Delete `app/services/portfolio_attribution.py` and
  `tests/unit/test_portfolio_attribution.py`.
- Prove in the PR body: (a) no runtime import (`rg` for the module path and its
  unique symbols), (b) attribution-logic coverage preserved by PR 1.
- Gate: full backend suite green; CodeQL/secrets/E2E unaffected (no runtime change).

## Tests that must exist before any dedupe

Before the legacy module is removed (PR 2), the live module must carry equivalent
coverage of, at minimum:

- Position-ledger accumulation across multiple fills for one ticker.
- Multi-ticker replay producing per-ticker attribution.
- Benchmark / weight-change snapshot construction.
- Capital-base inference and price-history/provider-fallback paths.
- Empty-input and single-fill edge cases.

Each should map to a live-API method (`build_strategy_attribution` / `build_summary`)
rather than to the legacy dataclasses.

## Out of scope for this investigation

No source, route, import, test, dependency, broker/provider/execution, safety, or
auth change was made. Live trading gates untouched. This document is additive.
