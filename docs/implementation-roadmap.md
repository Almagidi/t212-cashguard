# Implementation Roadmap

This roadmap turns the current CashGuard build review into a tracked plan tied to the actual repository.

Status legend:

- `[Done]` already implemented and materially working
- `[Partial]` implemented, but not yet deep enough for the intended production-quality outcome
- `[Planned]` not yet complete and should be built next

This document is intentionally practical: each item includes the main files and modules that should be changed when the work starts.

---

## Must Do Now

These are the highest-priority gaps because they most directly affect capital protection, promotion discipline, and whether the system can be trusted in demo/live conditions.

### 1. Strategy Promotion Pipeline

Status: `[Done]`

Goal:
- enforce hard graduation rules from `dry-run -> demo -> live`
- require minimum sample size, soak duration, and execution-quality thresholds
- refuse promotion automatically when data quality, slippage, or risk behavior is weak

Why this is first:
- the app already has live-readiness controls, but strategy-level promotion is still lighter than it should be
- this is the cleanest way to avoid enabling weak strategies too early

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/live_readiness.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/system_control.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/strategy_runner.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/portfolio_execution_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/settings.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/strategies.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/strategies/[id]/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/settings/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/integration/test_api.py`

Implementation checklist:
- add per-strategy demo soak counters and minimum thresholds
- store promotion evidence and rejection reasons per strategy
- require execution-quality checks before live enablement
- surface promotion state and blockers clearly in the strategy detail UI
- add tests for failed and successful promotion flows

---

### 2. Portfolio-Level Signal Allocator

Status: `[Done]`

Goal:
- allocate capital across simultaneous valid setups instead of letting each strategy act too independently
- rank opportunities by quality, regime fit, risk-adjusted reward, and portfolio overlap
- avoid over-concentration across correlated names and sectors

Why this is first-tier:
- the strategies are stronger than the current allocation logic
- this is the biggest missing piece between “many valid signals” and “good portfolio decisions”

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/strategy_runner.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/portfolio_execution_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/risk/engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/scanner/morning_scan.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/strategies.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/schemas.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/strategies/[id]/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_market_intelligence_monitor.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_portfolio_execution_service.py`

Implementation checklist:
- create a scoring/allocation service between scan output and order submission
- include sector/correlation/exposure penalties in ranking
- limit total concurrent risk by regime
- show “won allocation / lost allocation” reasons in the UI
- add attribution by allocator decision, not only by strategy

---

### 3. Execution-Quality Analytics

Status: `[Partial]`

Goal:
- measure slippage, fill delay, reject rates, cancel reasons, and broker behavior by symbol and order type
- make execution quality visible enough to pause weak environments automatically

Why this is first-tier:
- strategy quality cannot be trusted if execution quality is unmeasured
- Trading 212 is the main operational constraint, so the platform needs stronger execution diagnostics

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/execution/engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/db/models/__init__.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/orders.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/reports.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/alert_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/orders/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/components/orders/order-detail-dialog.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/reports/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_execution_engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/integration/test_api.py`

Implementation checklist:
- add explicit slippage and latency fields
- track first-ack time, fill time, and reconcile time
- report reject/cancel patterns by symbol/order type
- alert when slippage or rejection behavior becomes abnormal
- add execution-quality summaries to reports and order detail screens

---

## Should Do Next

These are important and high-value, but they can follow the first tier once promotion discipline and capital allocation are stricter.

### 4. Richer Regime Model

Status: `[Partial]`

Goal:
- move from a useful compact regime service to a deeper market-structure classifier
- include breadth, sector leadership, realized-volatility clustering, stress conditions, and stronger regime attribution

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/market_regime.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/news_intelligence.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/intelligence.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/market_intelligence_monitor.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/risk/engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/components/dashboard/regime-badge.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/strategies/[id]/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_market_regime.py`

Implementation checklist:
- add breadth and sector internals
- split broad “unsafe” state into more specific risk causes
- record regime attribution for trades and skipped trades
- expose regime-history snapshots in the UI

---

### 5. Stronger Catalyst and Event Engine

Status: `[Partial]`

Goal:
- improve event classification, source credibility, horizon modeling, and “noise vs thesis change” logic
- make news intelligence more useful for both intraday and swing decision quality

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/news_intelligence.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/scanner/morning_scan.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/strategy_runner.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/telegram_control.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/intelligence.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/strategies/[id]/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_news_intelligence.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_morning_scan.py`

Implementation checklist:
- refine event taxonomy
- add impact decay / persistence modeling
- reduce size automatically for lower-credibility or rumor-driven catalysts
- show catalyst freshness and confidence in the UI

---

### 6. Shadow / Paper vs Live Comparison Layer

Status: `[Planned]`

Goal:
- compare what the strategy wanted to do, what the broker did, and what shadow execution would have produced
- detect when live execution quality is degrading relative to research/demo assumptions

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/execution/engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/strategy_runner.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/portfolio_execution_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/reports.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/reports/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/tests/unit/test_execution_engine.py`

Implementation checklist:
- record intended vs submitted vs filled lifecycle explicitly
- compare expected fill assumptions with realized outcomes
- show variance by strategy and symbol
- feed poor live-vs-shadow gaps back into promotion gating

---

### 7. Strategy and Risk Reporting Upgrades

Status: `[Partial]`

Goal:
- make daily and weekly reporting more decision-useful
- answer “why no trades,” “why this trade,” and “why did this strategy degrade” with minimal operator effort

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/reports.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/alert_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/telegram_control.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/reports/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/docs/runbook.md`

Implementation checklist:
- add “why no trades today” summaries
- add strategy degradation and recovery reporting
- add risk/rejection trend reports
- add Telegram daily/weekly structured summaries

---

## Later But Valuable

These are still worthwhile, but they come after the system is stricter about promotion, allocation, and execution quality.

### 8. Incident Replay and Recovery Bundles

Status: `[Planned]`

Goal:
- package feed failures, broker mismatches, kill-switch triggers, and worker issues into replayable incident views

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/startup_validation.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/worker_health.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/market_intelligence_monitor.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/v1/routes/health.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/audit/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/alerts/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/docs/troubleshooting.md`
- `/Users/Ameer/Desktop/t212-cashguard/docs/runbook.md`

Implementation checklist:
- add incident grouping and exportable bundles
- connect alerts, health, audit, and order history into one operator view
- make postmortems easier to reconstruct

---

### 9. More Complete Telemetry and Tracing

Status: `[Partial]`

Goal:
- deepen runtime metrics and traces so performance and failure patterns are measurable over time

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/api/metrics.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/main.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/worker_health.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/dashboard/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/audit/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/docs/architecture.md`

Implementation checklist:
- add more broker/feed/latency metrics
- add trace IDs through execution and alerts
- expose runtime trend panels in the UI

---

### 10. Broader Research-Live Parity

Status: `[Partial]`

Goal:
- reduce the gap between backtest assumptions and live behavior
- improve transaction-cost realism, reject modeling, and promotion confidence

Main files to change:
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/backtest/engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/backtest/portfolio_engine.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/strategy_runner.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/api/app/services/portfolio_execution_service.py`
- `/Users/Ameer/Desktop/t212-cashguard/apps/web/app/app/backtest/page.tsx`
- `/Users/Ameer/Desktop/t212-cashguard/docs/testing.md`

Implementation checklist:
- model more realistic fill and reject paths
- compare backtest assumptions with live execution evidence
- record model-confidence drift across regimes

---

## Summary Ranking

### Must Do Now

1. Strategy promotion pipeline
2. Portfolio-level signal allocator
3. Execution-quality analytics

### Should Do Next

4. Richer regime model
5. Stronger catalyst and event engine
6. Shadow / paper vs live comparison
7. Strategy and risk reporting upgrades

### Later But Valuable

8. Incident replay and recovery bundles
9. More complete telemetry and tracing
10. Broader research-live parity

---

## How To Use This Roadmap

When starting a workstream:

1. update the status marker for the item
2. break the item into one or more repo checkpoints
3. add or extend the tests in the file group listed above
4. update this document and the README when the checkpoint lands

This keeps the roadmap honest and tied to the actual codebase rather than drifting into a generic product wishlist.
