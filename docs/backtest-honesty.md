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
- **Benchmarks, all costed:** `universe_equal_weight_costed` (same basket, buy and hold),
  `broad_index` (a ticker you supply, e.g. SPY or a global ETF) and `risk_matched_blend`
  (that index plus cash at the strategy's average exposure). Each reports excess return, excess
  Sharpe and excess max drawdown (strategy minus benchmark; positive drawdown excess is worse).
  Benchmarks are bought on day one and marked to market; there is no exit cost for either side.
- **Risk-free rate.** `risk_free_rate_annual` (default 0.0) is subtracted from daily returns in
  Sharpe and Sortino and is echoed in the result. Cash inside the simulation earns nothing, so a
  higher rate can only lower the ratios.
- **Optional walk-forward** (`walk_forward`, off by default): expanding train window, disjoint
  test windows, parameters picked from your grid on train Sharpe only and then frozen, a final
  holdout never used for selection, a minimum rebalance count per window, and the deflated Sharpe
  ratio with the number of trials recorded (grid size plus `extra_trials` for ideas already
  tried). 'promising' and 'strong' are withheld (verdict `unvalidated`) unless the walk-forward
  gate passes: enough valid windows, out-of-sample and holdout return beating each costed
  benchmark, out-of-sample Sharpe beating each, and a non-zero-cost model.

## What it still cannot claim

- **Survivorship.** The default scanner universe is 14 hand-picked winners. There are no
  point-in-time index constituents, so delisted and failed names are absent. Results on that
  universe are upper bounds, not estimates.
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
   and holdout net return beat the costed broad index, deflated Sharpe probability at least 0.95
   (`min_deflated_sharpe_probability`), still ahead at 2x costs, at least 3 rebalances per
   window." Commit it. Do not edit it after seeing numbers.
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
