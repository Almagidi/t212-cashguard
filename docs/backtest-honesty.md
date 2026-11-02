# Backtest honesty

What the portfolio backtest measures, what it cannot claim, and how to run a minimal honest
research pass. Code: `apps/api/app/backtest/portfolio_*.py`, route `POST /v1/backtest/portfolio/run`.

## What it measures now

- **Metrics start at the first allocation.** Sharpe, Sortino, max drawdown, average exposure,
  turnover, annualised return and every benchmark all start on the first day the strategy can
  trade. Warm-up days spent 100% in cash are excluded (`metrics_start_date`,
  `warmup_bars_excluded` in the result).
- **Costs, per side, on the strategy and on every benchmark.** Half the quoted spread
  (default 10 bps full spread), slippage (5 bps), commission (2 bps, 0.01 minimum) and an FX fee
  of 0.15% (`fx_fee_rate = 0.0015`) on any instrument whose currency differs from the account
  currency (default GBP). An instrument with no known currency is treated as foreign and pays
  the FX fee; say which listings are in GBP via `instrument_currencies` or
  `default_instrument_currency`. A single unlisted-currency buy therefore costs at least 15 bps
  plus half the spread.
- **Cost stress.** `stress_multiplier` of 1, 2 or 3 scales every cost; run all three. A result
  that disappears at 2x costs is not an edge.
- **Zero cost is opt-in and labelled.** `zero_cost: true` is the only way to run frictionless;
  the result says `zero_cost_explicit` and can never be rated promising or strong.
- **Cost floor for positive verdicts.** The all-in round-trip cost (buy then sell: half-spread,
  slippage, commission and, for foreign listings, the FX fee, times `stress_multiplier`) on the
  cheapest instrument in the universe must be at least **10 bps** (`MIN_ROUND_TRIP_COST_BPS`).
  The defaults are 24 bps (GBP listing) to 54 bps (foreign). The commission minimum fee is not
  counted, so the floor is conservative. All cost components are compared in bps (the FX fee
  rate is a fraction and is converted). The legacy `transaction_cost_bps` flat-fee path, only
  reachable from code, is labelled `legacy_flat_bps`, has no spread, slippage or FX, and is
  never eligible for promising or strong.
- **Benchmarks, all costed:** `universe_equal_weight_costed` (same basket, buy and hold),
  `broad_index` (a ticker you supply, e.g. SPY or a global ETF) and `exposure_matched_blend`
  (that index plus cash at the strategy's average exposure; it matches exposure, not risk).
  Requests that still say `risk_matched_blend` are accepted and mapped to the new key; results
  only use the new key. Each reports excess return, excess Sharpe (from unrounded Sharpe
  ratios) and excess max drawdown (strategy minus benchmark; positive drawdown excess is
  worse). Benchmarks are bought on day one and marked to market; there is no exit cost for
  either side.
- **Same cash buffer.** A rebalance allocates 99.8% of equity (`ALLOCATABLE_EQUITY_FRACTION`)
  and fees come out of the rest. The buy-and-hold benchmarks size their entry the same way, so
  the buffer does not tilt the comparison.
- **Metric conventions.** One year is 252 trading sessions everywhere: Sharpe and Sortino scale
  by sqrt(252); annualised return is `(final/initial)^(252/sessions) - 1` (it used to use 365
  calendar days). Sortino divides the mean excess return by the downside deviation, the root mean
  square of `min(r - rf, 0)` over **all** periods (it used to use the stdev of losing days
  only, which flattered the ratio and was undefined for one loss). `turnover_pct` is annualised
  one-way turnover: half of (buys + sells) over average equity, per 252 sessions.
- **Risk-free rate.** `risk_free_rate_annual` (default 0.0) is subtracted from daily returns in
  Sharpe and Sortino and is echoed in the result. Cash inside the simulation earns nothing, so a
  higher rate can only lower the ratios.
- **Optional walk-forward** (`walk_forward`, off by default): expanding train window, disjoint
  test windows, parameters picked from your grid on train Sharpe only and then frozen, a final
  holdout never used for selection, a minimum rebalance count per window, and the deflated Sharpe
  ratio with the number of trials recorded (grid size plus `extra_trials` for ideas already
  tried). The trial-Sharpe variance fed to the deflated Sharpe is
  `max(grid variance, 1 / (T - 1))` whenever there is more than one trial, where the grid
  variance is the sample variance of the per-period train Sharpe across the grid on the final
  pre-holdout fit and T is the number of stitched out-of-sample observations. `1 / (T - 1)` is
  the sampling variance of a per-period Sharpe estimate for a skill-less strategy with normal
  returns. The floor assumes your trials (grid entries and `extra_trials`) are at least as
  dispersed as estimation noise; it stops near-duplicate grid entries or a one-entry grid with
  `extra_trials` from earning a weaker penalty than noise implies. It is an assumption, not a
  measurement: independent ideas may be more dispersed, so the penalty can still be too small.
  Both inputs are reported (`trials_sr_variance_grid`, `trials_sr_variance_floor`).
- **The gate for 'promising' and 'strong'.** Server-side floors in
  `app/backtest/gate_floors.py` that a request can raise but not lower (a setting below a floor
  is itself a gate failure, and the enforced value never drops below the floor):

  | Floor | Value |
  | --- | --- |
  | Holdout length | at least 126 sessions |
  | Test window length | at least 63 sessions |
  | Valid out-of-sample windows | at least 3 (`min_valid_windows`) |
  | Rebalances per window | at least 3 (`min_rebalances_per_window`) |
  | Deflated Sharpe probability (out-of-sample) | at least 0.95, mandatory |
  | Windows beating each gated benchmark | more than half of the valid windows |
  | Round-trip cost | at least 10 bps, not zero-cost, not the legacy path |
  | Broad-index benchmark sanity | ticker not in the universe, annualised volatility at least 8%, correlation with the universe equal-weight returns at least 0.5 |

  On top of the floors the stitched out-of-sample return and Sharpe and the holdout return and
  Sharpe must each beat every gated costed benchmark, and a broad-index series must have been
  supplied. Without a broad index the best verdict is `unvalidated`.
- **The broad index is sanity-checked.** `benchmark_ticker` is free-form, so a cash or T-bill
  ETF (beaten trivially) or a universe member would otherwise be a weak "index". Over the
  evaluated period (valid out-of-sample windows plus the holdout) the harness computes the
  benchmark's annualised volatility and its correlation with the universe equal-weight daily
  returns. The gate fails, and the verdict is `unvalidated`, if the ticker is in the universe,
  volatility is below 8% or correlation is below 0.5. The ticker and both statistics are in
  `benchmark_sanity` (walk-forward result and `validation.benchmark_sanity`) and in the
  interpretation warnings. Passing proves the benchmark moves with the universe, not that it is
  the right index for your question.
- **Two verdicts, shown together.** `in_sample_verdict` comes from the full-period run with
  `strategy_params`. `validated_verdict` classifies the walk-forward's out-of-sample and holdout
  evidence (the weaker of the two on Sharpe and return, the deeper drawdown) with the same
  thresholds; it describes the grid-selected procedure, which is a different thing and can
  disagree. promising or strong is emitted only if the gate passes **and** both verdicts are
  the same positive label; otherwise the verdict is `unvalidated` and the reason says which
  condition failed.

## What it still cannot claim

- **Currency.** FX moves between the account currency and the instruments' currencies are not
  modelled. Bars are in the instrument's quote currency (typically USD) and the returns are
  presented as account-currency returns; only the FX conversion fee is charged. For a GBP
  account holding USD assets this ignores the dollar's own contribution or drag, which can be
  large. The result carries this under `disclosures.currency`.
- **Dropped windows.** A walk-forward window with fewer rebalances than `min_rebalances_per_window`
  is dropped from every out-of-sample statistic, not counted as a loss. The count, ids and
  reasons are reported (`out_of_sample.windows_dropped`, `dropped_window_ids`,
  `dropped_windows` in the walk-forward result, and a warning in the interpretation). Many
  dropped windows mean the evidence is thinner than the headline numbers suggest.
- **Holdout reuse is not enforced.** The holdout is spent by every call. Nothing durable tracks
  reuse across calls: results live in the process's memory only. Each walk-forward job returns
  `holdout_reuse_warning` and a `holdout_reuse` record: your optional `run_id`, a
  `data_fingerprint` (universe, dates, benchmark, dataset manifests) and a `run_fingerprint`
  (that plus strategy, parameters, costs, grid and walk-forward settings), and the earlier jobs
  in the same process that used the same data. Keep your own log of fingerprints; a second run on
  the same data with a changed grid has already spent the holdout and must be counted in
  `extra_trials`.

- **Survivorship.** The default scanner universe is 14 hand-picked winners. There are no
  point-in-time index constituents, so delisted and failed names are absent. Results on that
  universe are upper bounds, not estimates. This text is `disclosures.survivorship` on every
  portfolio result and is added to the interpretation warnings whenever a promising or strong
  label is emitted or withheld, or the walk-forward gate passes.
- **Adjusted prices.** Bars are provider-adjusted and the corporate-action treatment is
  unqualified; dividends and splits are only as right as the provider's adjustment.
- **Costs are a model.** Spread and slippage are flat assumptions, not measured per instrument;
  there is no market impact, no stamp duty or financing and no tax. Fractional shares are
  assumed.
- **Few independent observations.** Daily bars over a few years give a small number of
  independent decisions; the deflated Sharpe uses the grid size as the trial count and an
  approximation for trial variance (see `portfolio_walk_forward.py`). It does not know about
  strategy ideas you tried elsewhere unless you pass `extra_trials`.
- **A backtest is not a forecast.** Passing the gate is a reason to paper trade, not to fund.

## A minimal honest research pass

1. **Write the success rule first**, before looking at any result. For example: "out-of-sample
   and holdout net return and Sharpe beat the costed broad index, a majority of windows ahead,
   deflated Sharpe probability at least 0.95 (now enforced by the server), still ahead at 2x
   costs, at least 3 rebalances per window." Commit it. Do not edit it after seeing numbers.
2. **Choose the universe by liquidity, not by hindsight:** a small set of liquid UCITS ETFs
   listed in GBP (so no FX fee and tight spreads), plus the index you will be compared with. Do
   not build it from last year's winners.
3. **Use real costs:** leave the FX fee on, mark GBP listings explicitly, run 1x, 2x and 3x.
4. **Fix the parameter grid up front** and keep it small. Every extra grid entry or discarded
   idea raises the trial count; pass `extra_trials` for ideas tried in earlier runs.
5. **Run the walk-forward once per hypothesis** with a holdout you have not looked at. If you
   change the strategy after seeing the holdout, the holdout is spent; get new data or stop.
6. **Compare with the costed benchmarks** and read `gate_reasons`. A failed gate is the answer.
7. **Only then paper trade** for a period fixed in advance, and judge it by the same rule. Live
   money comes after the paper results meet the rule, not before.

Typical result for most ideas: no edge after costs. That is a valid outcome and should be
recorded, not retried until it changes.
