# Backtest vs Execution-Quality Parity Investigation

## 1. Executive summary

Current backtests are **PARTIALLY TRUSTWORTHY** as research tools, but **NOT YET TRUSTWORTHY** as strategy-promotion authority.

The single-symbol backtest engine models next-bar market fills, fixed synthetic slippage, zero default commission, stop/take-profit exits, partial exits, cash reduction, and drawdown metrics. The portfolio backtest engine models daily rebalance turnover, cash constraints, fractional shares, and a flat transaction-cost rate. These assumptions are useful, but they are not calibrated to the demo/runtime execution-quality records.

The runtime/demo stack records richer order execution evidence than the backtests consume: expected price, broker order id, submitted/ack/fill/reject/cancel timestamps, average fill price, filled quantity, slippage percentage/value, latency, reconciliation latency, quality score/grade/notes, broker request/response, order events, and audit logs.

The main gap is attribution and promotion trust. Runtime reports can expose execution-quality data separately, but ordinary performance and portfolio attribution outputs do not fully join slippage, fees, broker-side failures, reconciliation delay, or rejected-order evidence into final PnL and promotion language. Current attribution is **PARTIALLY TRUSTWORTHY** for historical ledger reconstruction when fills are present, but **NOT YET TRUSTWORTHY** for strategy promotion if slippage/fees/execution-quality data are not joined into the final attribution output.

## 2. Current project state

- Repository baseline requested: `Almagidi/t212-cashguard`.
- Local path inspected: `/Users/Ameer/Desktop/t212-cashguard-codex`.
- Initial branch before work: `main`.
- Verified `HEAD`: `2349199defff92fc7311076a2689d9c57be06685`.
- Verified `origin/main`: `2349199defff92fc7311076a2689d9c57be06685`.
- Verified initial status: clean `main...origin/main`.
- Verified `gh pr list --limit 20`: no open PRs returned.
- Created investigation branch: `docs/backtest-execution-quality-parity`.
- Scope level: Level A docs-only investigation.
- Live-trading posture preserved: Trading 212 DEMO / paper-mode hardening only; live trading remains disabled and not live-ready.

## 3. Files and systems inspected

Inspected required areas where present:

- `apps/api/app/backtest/engine.py`
- `apps/api/app/backtest/portfolio_engine.py`
- `apps/api/app/backtest/data_fetcher.py`
- `apps/api/app/backtest/portfolio_strategies.py`
- `apps/api/app/backtest/strategy_registry.py`
- `apps/api/app/services/execution_quality.py`
- `apps/api/app/services/performance_attribution.py`
- `apps/api/app/services/portfolio_attribution_service.py`
- `apps/api/app/services/portfolio_attribution.py`
- `apps/api/app/execution/engine.py`
- `apps/api/app/execution/paper_engine.py`
- `apps/api/app/execution/state_machine.py`
- `apps/api/app/services/demo_order_reconciliation.py`
- `apps/api/app/services/demo_reconciliation_worker.py`
- `apps/api/app/services/demo_reconciliation_scheduler.py`
- `apps/api/app/workers/tasks.py`
- `apps/api/app/workers/tasks_dca.py`
- `apps/api/app/workers/tasks_heartbeat.py`
- `apps/api/app/db/models/__init__.py`
- `apps/api/app/api/v1/routes/backtest.py`
- `apps/api/app/api/v1/routes/reports.py`
- `apps/api/app/api/v1/routes/operator.py`
- `apps/api/app/services/strategy_promotion.py`
- `apps/api/tests/unit/test_backtest_engine.py`
- `apps/api/tests/unit/test_portfolio_backtest.py`
- `apps/api/tests/unit/test_execution_quality.py`
- `apps/api/tests/unit/test_performance_attribution.py`
- `apps/api/tests/unit/test_portfolio_attribution_service.py`
- `apps/api/tests/unit/test_strategy_promotion.py`
- `apps/api/tests/integration/test_demo_reconciliation.py`
- `apps/api/tests/integration/test_demo_reconciliation_worker.py`
- `apps/api/tests/integration/test_paper_execution.py`
- `docs/implementation-roadmap.md`
- `docs/SAFETY_MODEL.md`
- `docs/paper-execution.md`
- `docs/runbook.md`
- `docs/operator-manual-qa.md`
- `docs/architecture/portfolio-attribution-duplication-investigation.md`

Path note: the request listed `apps/api/app/models.py`; this checkout stores the SQLAlchemy models in `apps/api/app/db/models/__init__.py`.

## 4. Backtest assumptions map

| Area | Status | Evidence | Trust assessment |
| --- | --- | --- | --- |
| Slippage | Covered | `ExecutionSimulator` applies fixed half-spread plus market impact for market fills. Single-symbol trades expose `slippage_cost` and total slippage. | **PARTIALLY TRUSTWORTHY** because assumptions are synthetic and not calibrated to recorded demo slippage. |
| Fees / transaction costs | Partially covered | Single-symbol default `commission_per_trade` is `0` with a Trading 212 zero-commission comment. Portfolio backtests use `transaction_cost_bps=10` by default. | **PARTIALLY TRUSTWORTHY** because fees, FX, taxes, funding, and broker-specific charges are not proved against actual records. |
| Fills | Covered | Market orders fill on the next bar open plus slippage. Limit orders fill when the next bar range crosses the limit. | **PARTIALLY TRUSTWORTHY** because this does not model broker queueing, liquidity, rejected orders, or actual fill history. |
| Partial fills | Not covered | Single-symbol strategy exits can be partial, but order execution itself fills the requested quantity atomically. Portfolio rebalance orders also execute full computed shares. | **NOT YET TRUSTWORTHY** for brokers that can partially fill. |
| Rejected orders | Not covered | Backtest orders can remain pending, fill, cancel, or expire; there is no broker rejection/error simulation. | **NOT YET TRUSTWORTHY** for promotion decisions. |
| Delayed fills | Partially covered | Backtest market orders fill on the next bar; limit orders may wait up to three bars before cancellation. | **PARTIALLY TRUSTWORTHY** but not equivalent to runtime broker/reconciliation latency. |
| Spread / price impact | Covered | Single-symbol market fills include fixed 3 bps half-spread plus 2 bps market impact. Portfolio backtests use a flat transaction-cost rate but no explicit bid/ask spread model. | **PARTIALLY TRUSTWORTHY** because assumptions are fixed, not venue/order-size dependent. |
| Rebalance costs | Covered | Portfolio backtests subtract transaction costs on buys and sells and track turnover. | **PARTIALLY TRUSTWORTHY** because runtime portfolio attribution does not separately surface equivalent execution-quality costs. |
| Cash constraints | Covered | Single-symbol backtest updates available cash after fills; portfolio backtest caps buys by available cash and reserves 0.2% through allocatable equity. | **PARTIALLY TRUSTWORTHY** because runtime broker buying-power and settlement constraints are not mirrored. |
| Position sizing | Covered | Single-symbol strategy signals provide `suggested_quantity`; backtester enforces one open position and maximum-position constructor settings. Portfolio backtests compute target-weight shares with fractional precision. | **PARTIALLY TRUSTWORTHY** because strategy-generated sizing is accepted without broker-side minimum/precision/rejection modeling. |
| Drawdown / daily-loss controls | Partially covered | Backtest results compute max drawdown, drawdown duration, consecutive losses, and warnings. There is no full runtime daily-loss gate simulation inside the backtest loop. | **PARTIALLY TRUSTWORTHY** for analytics; **NOT YET TRUSTWORTHY** as proof that runtime safety gates behave identically. |

## 5. Runtime execution-quality map

| Runtime data | Status | Evidence | Trust assessment |
| --- | --- | --- | --- |
| Submitted order price | Recorded | `Order.expected_fill_price` is set from estimated, limit, stop, or signal price. | **PARTIALLY TRUSTWORTHY** because market expected price can be an estimate rather than broker quote. |
| Broker fill price | Recorded | Broker responses and Trading 212 history snapshots populate `avg_fill_price`; raw broker payloads are retained in `broker_response`. | **PARTIALLY TRUSTWORTHY** because parsing depends on broker payload fields and reconciliation availability. |
| Average fill price | Recorded | `Order.avg_fill_price` exists and is used by execution quality and portfolio attribution. | **TRUSTWORTHY** when broker history supplies it; **UNKNOWN** when broker does not. |
| Filled quantity | Recorded | `Order.filled_quantity` exists and is populated from broker response/history or local paper fill. | **TRUSTWORTHY** when populated; partial-fill semantics are not fully propagated into promotion/backtest parity. |
| Slippage amount | Computed | `calculate_order_execution_quality()` computes `slippage_value` from expected vs actual price and filled quantity when not already stored. | **PARTIALLY TRUSTWORTHY** because it is separate from final PnL attribution in ordinary reports. |
| Slippage percentage | Computed | `slippage_pct` is computed side-aware and scored. | **PARTIALLY TRUSTWORTHY** for execution-quality reporting; **NOT YET TRUSTWORTHY** as a PnL adjustment everywhere. |
| Fees or fee assumptions | Missing | No `Order` fee/commission column was found. Backtest fee assumptions are not mapped to broker/runtime fee data. | **UNKNOWN** for true net performance. |
| Order rejection/cancellation | Recorded | `Order.status`, `rejected_at`, `cancelled_at`, `error_message`, order events, and audit rows record terminal failures. | **PARTIALLY TRUSTWORTHY** because performance attribution mainly reads closed trades/fills. |
| Reconciliation delay | Computed | `reconciliation_latency_ms`, `last_reconciled_at`, demo reconciliation worker, scheduled reconciliation, and order events record broker-history delay. | **TRUSTWORTHY** as operational telemetry; **PARTIALLY TRUSTWORTHY** for promotion unless explicitly joined. |
| Broker history/fills | Recorded | Demo reconciliation reads Trading 212 history and maps status, filled quantity, average fill price, and terminal timestamps. | **PARTIALLY TRUSTWORTHY** because missing history match leaves local state unchanged and audited. |
| Audit events | Recorded | Execution, paper execution, reconciliation, safety policy, and worker paths add audit logs and order events. | **TRUSTWORTHY** for traceability, assuming retention is available. |

## 6. Backtest vs runtime parity gaps

1. Synthetic slippage versus observed slippage: backtests use fixed slippage/cost assumptions, while runtime computes order-level observed slippage from expected and actual fills. There is no evidence that backtest parameters are calibrated from `ExecutionQualityService` output.

2. Rejected and error orders: runtime records rejected, cancelled, and error statuses. Backtests do not model broker rejection/error rates, safety-policy rejection, reconciliation missing-history outcomes, or broker payload parse failures. This can make backtest fill rates look cleaner than demo execution.

3. Partial fills: runtime has `filled_quantity`, but backtests assume atomic order fills. Partial exits in strategy logic are not the same as broker partial fills.

4. Reconciliation timing: runtime can move orders from accepted/submitted to terminal states later through direct order polling or Trading 212 demo history. Backtests assume deterministic next-bar fills or limited pending windows.

5. Fees and true net costs: single-symbol backtests assume zero commission by default; portfolio backtests use a flat bps cost. Runtime order records do not expose explicit fees/commissions, so report-level net performance cannot prove it matches either assumption.

6. Attribution split: execution-quality reports separately compute slippage, score, reject/error/cancel rates, and latency. Performance reports use `Trade.realized_pnl`; portfolio attribution replays filled rebalance orders and mark-to-market prices. These outputs are not fully unified.

7. Promotion language: backtest interpretation can say a result is promising or strong, while strategy promotion gates check sample counts, reviews, fill rate, error rate, risk blocks, and global live readiness. They do not appear to require slippage/fee parity proof before promotion.

## 7. Attribution/reporting trust assessment

The legacy `PerformanceAttributor.slippage_report()` can compute slippage records for filled non-dry-run orders joined to signals. That is useful and **PARTIALLY TRUSTWORTHY**.

`PerformanceAttributor.symbol_attribution()` is **NOT YET TRUSTWORTHY** for execution-adjusted strategy promotion because `avg_slippage_pct` and `total_slippage_cost` are hardcoded to `0.0` with an inline note to join slippage records. This can overstate symbol-level quality whenever slippage is material.

The primary `/reports/performance` route is **PARTIALLY TRUSTWORTHY** for ledger-level realized PnL because it uses closed non-dry-run `Trade.realized_pnl`. It is **NOT YET TRUSTWORTHY** as a full execution-quality net report because it does not disclose or join slippage, fee assumptions, rejected orders, reconciliation delay, or execution-quality grades.

`PortfolioAttributionService` is **PARTIALLY TRUSTWORTHY** for replaying filled rebalance orders because it uses actual `filled_quantity` and `avg_fill_price` when present. It is **NOT YET TRUSTWORTHY** for promotion-grade net attribution because it does not separately expose execution-quality score, slippage value, fee assumptions, rejected rebalance attempts, missing broker history, or reconciliation delays in the final attribution response.

Blunt conclusion: current reports can overstate strategy quality if a reader treats realized PnL or backtest output as fully net of slippage, fees, broker failure modes, and reconciliation uncertainty. The numbers are not fully trustworthy for strategy promotion until execution-quality evidence is joined into the promotion/reporting surface.

## 8. Operator visibility assessment

The operator endpoint is **TRUSTWORTHY** for high-level read-only safety posture: mode, live-readiness summary, venue config, active/recent orders, scheduler/heartbeat status, paper execution summary, and recent audit activity.

The execution-quality report endpoint is **PARTIALLY TRUSTWORTHY** for surfacing actual order-quality telemetry: fill/reject/cancel/error rates, adverse slippage, latency, quality scores, bucketed symbol/order-type stats, reject/cancel patterns, and worst orders.

Operator/reporting visibility is **NOT YET TRUSTWORTHY** for clearly communicating backtest/execution-quality parity because:

- backtest responses serialize slippage and commission totals but do not label the assumptions as synthetic and not calibrated to demo data;
- ordinary performance reports do not state whether execution-quality data is included;
- portfolio attribution does not show whether slippage/fees/rejections/reconciliation gaps are included;
- promotion checks include demo fill and error rates, but not slippage/fee thresholds or a backtest-assumption parity warning;
- the existing roadmap already identifies performance-attribution slippage caveats and this investigation as planned follow-up work.

## 9. Risk ranking

| Rank | Risk | Impact | Current label |
| --- | --- | --- | --- |
| 1 | Strategy promotion from clean backtests while demo fills are worse, delayed, rejected, or not fully reconciled. | False confidence in strategy edge. | **High / NOT YET TRUSTWORTHY** |
| 2 | Performance reports omit explicit execution-quality and fee caveats. | Operators may read realized PnL as fully net and promotion-ready. | **High / NOT YET TRUSTWORTHY** |
| 3 | Symbol attribution hardcodes slippage metrics to zero. | Symbol-level ranking can be materially misleading. | **High / NOT YET TRUSTWORTHY** |
| 4 | Portfolio attribution uses actual fill prices but omits rejected attempts and cost caveats. | Rebalance sleeve performance may look cleaner than operational reality. | **Medium / PARTIALLY TRUSTWORTHY** |
| 5 | Backtest slippage and transaction-cost assumptions are static. | Research results may be under- or over-conservative for Trading 212 demo behavior. | **Medium / PARTIALLY TRUSTWORTHY** |
| 6 | Runtime records no explicit fee/commission field. | True net performance is unknown where non-price costs matter. | **Medium / UNKNOWN** |

## 10. Recommended PR sequence

### PR 1: Report Explicit Attribution Caveats

Target: Add read-only caveats to performance and portfolio attribution responses/docs that state whether slippage, fees, rejects, cancellations, and reconciliation delay are included.

Autonomy level: Level B.

Files likely touched: `apps/api/app/api/v1/routes/reports.py`, `apps/api/app/services/portfolio_attribution_service.py`, API schemas if response shape changes, focused report/attribution tests, docs.

Why it matters: Operators should not mistake ledger PnL for promotion-grade net performance.

Risk: Low to medium; read-only response changes can affect frontend/API consumers.

Validation required: Unit/API tests proving caveats appear and no order/execution paths change.

Stop-before-merge required: Yes, if response schemas change or frontend contract updates are needed.

### PR 2: Join Existing Slippage Metrics Into Symbol Attribution

Target: Replace hardcoded symbol slippage values with data from existing slippage/execution-quality records.

Autonomy level: Level C.

Files likely touched: `apps/api/app/services/performance_attribution.py`, `apps/api/tests/unit/test_performance_attribution.py`.

Why it matters: Symbol profitability should not rank as clean when fills are poor.

Risk: Medium; attribution numbers will change.

Validation required: Tests for buy/sell slippage aggregation, no-signal fallback, and zero-data behavior.

Stop-before-merge required: Yes; this changes analytics logic.

### PR 3: Add Backtest Assumption Metadata To Backtest Results

Target: Return an `assumptions` block with slippage model, fee model, fill model, partial-fill/rejection limitations, and promotion caveat.

Autonomy level: Level B.

Files likely touched: `apps/api/app/api/v1/routes/backtest.py`, backtest route tests, frontend only if the UI displays the metadata.

Why it matters: Backtests should describe their model limits at the point of use.

Risk: Low to medium; API shape grows but trading behavior is unchanged.

Validation required: Tests proving single-symbol and portfolio result payloads include assumption metadata.

Stop-before-merge required: Yes, if frontend contract changes are needed.

### PR 4: Calibrate Backtest Friction From Execution-Quality Windows

Target: Produce a read-only calibration report comparing synthetic backtest assumptions with observed demo slippage/fill/reject/error/latency data.

Autonomy level: Level B initially; Level C if backtest logic starts consuming the calibration.

Files likely touched: new service/report route, `apps/api/app/services/execution_quality.py`, report tests, docs.

Why it matters: Fixed friction assumptions should be compared against observed broker behavior before promotion.

Risk: Medium; easy to overstate confidence if sample sizes are thin.

Validation required: Tests for empty, sparse, normal, and degraded execution-quality windows.

Stop-before-merge required: Yes, especially before any runtime/backtest behavior consumes calibration.

### PR 5: Add Promotion Gate Disclosure For Execution-Quality Completeness

Target: Extend promotion evaluation output with explicit execution-quality completeness and slippage/fee caveats.

Autonomy level: Level B for disclosure only; Level C for hard blocking gates.

Files likely touched: `apps/api/app/services/strategy_promotion.py`, strategy promotion route/tests, docs.

Why it matters: Promotion should not imply execution-quality parity when that evidence is missing.

Risk: Medium; operators may see new blockers or caveats.

Validation required: Tests for promotion payloads with no demo orders, filled orders with slippage, rejected/error orders, and missing fee data.

Stop-before-merge required: Yes, if any gate changes from advisory to blocking.

### PR 6: Model Broker Failure Modes In Backtest Research

Target: Add optional research-only backtest scenario overlays for reject/error rate, delayed fills, partial fills, and reconciliation uncertainty.

Autonomy level: Level C.

Files likely touched: `apps/api/app/backtest/engine.py`, `apps/api/app/backtest/portfolio_engine.py`, backtest tests, docs.

Why it matters: Strategy edge should survive realistic failure-mode stress, not just idealized fills.

Risk: High; this changes backtest outputs and interpretation.

Validation required: Characterization tests for existing behavior, then tests for each new failure-mode overlay.

Stop-before-merge required: Yes; this is a runtime-adjacent analytics change.

## 11. Work that must remain blocked

- Runtime trading changes.
- Broker/provider/execution/safety/auth/live-trading logic changes.
- Strategy logic changes.
- Backtest logic changes in this PR.
- Attribution logic changes in this PR.
- Test changes in this PR without separate approval.
- Slippage integration implementation.
- Fee model implementation.
- Promotion-gate enforcement changes.
- Legacy attribution deletion.
- Kraken/crypto work.
- Frontend buy/sell/order controls.
- Any wording implying the project is live-ready.
- Any wording weakening safety constraints.

## 12. Exact recommended next step

Run PR 1 as a small Level B disclosure PR: add explicit report/attribution caveats showing whether slippage, fees, rejected/cancelled/error orders, and reconciliation delay are included in each output. Do not change trading behavior. Do not change promotion gates yet. This gives operators honest labels before deeper Level C attribution or backtest-parity changes.
