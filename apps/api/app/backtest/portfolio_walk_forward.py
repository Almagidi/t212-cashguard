"""
Expanding-window walk-forward harness for the portfolio strategies.

Layout over the aligned sessions ``0 .. N-1``::

    |<-- initial train -->|<- test 0 ->|<- test 1 ->| ... |<- test k ->|<-- holdout -->|

* Test windows are consecutive and disjoint; window ``k`` trains on everything before it
  (expanding window) and nothing else.
* Parameters are picked from the caller's grid on the **train** Sharpe only (ties go to the
  earliest grid entry) and are frozen before the test window is run.  No test or holdout data
  enters a selection; selection runs on slices that end at the train boundary.
* The holdout is the final ``holdout_bars`` sessions.  It is never used for selection:
  parameters are re-selected on every pre-holdout session and the holdout is run exactly once.
* A window needs ``min_rebalances_per_window`` executed rebalances to count; invalid windows
  are listed but excluded from the stitched out-of-sample series.
* The deflated Sharpe ratio (Bailey & Lopez de Prado, 2014) records the number of trials
  (grid size plus any ``extra_trials`` the caller already burned on other ideas).

Approximations, stated plainly: each window restarts from cash (so every window pays an entry
cost; stitching windows is conservative), the trial-Sharpe variance used by the deflated Sharpe
ratio is the sample variance of the per-period train Sharpe across the grid on the final
pre-holdout fit, and the coverage report attached to slice runs is the full-range report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import NormalDist
from statistics import variance as sample_variance
from typing import TYPE_CHECKING, Any

from app.backtest.gate_floors import (
    MIN_DEFLATED_SHARPE_PROBABILITY,
    MIN_VALID_WINDOWS,
    MIN_WINDOW_WIN_FRACTION,
    benchmark_sanity,
    config_floor_violations,
    cost_gate_refusal,
    effective_deflated_sharpe_threshold,
    floors_as_dict,
)
from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import (
    BENCHMARK_BROAD_INDEX,
    BENCHMARK_UNIVERSE_EW,
    PortfolioBacktester,
    PortfolioCoveragePolicy,
    _align_benchmark_bars,
    _align_research_histories,
)
from app.backtest.portfolio_metrics import (
    TRADING_DAYS_PER_YEAR,
    daily_risk_free,
    max_drawdown_pct_from_returns,
    sharpe_ratio,
    total_return_pct_from_returns,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from app.strategies.indicators import Bar

EULER_MASCHERONI = 0.5772156649015329
MAX_GRID_SIZE = 50

# Benchmarks the gate compares against; the risk-matched blend is reported but not gated.
GATED_BENCHMARKS = (BENCHMARK_UNIVERSE_EW, BENCHMARK_BROAD_INDEX)

HOLDOUT_REUSE_WARNING = (
    "The holdout is spent by every call: parameters are re-selected and the holdout is scored "
    "once per run. Reuse detection is advisory only: it compares fingerprints (strategy family, "
    "holdout end date, parameter grid, universe and dates) with jobs still in this process's "
    "memory, and cannot see other processes, restarts or other tools. Re-running after looking "
    "at a result (new grid, new parameters, new universe, new dates) turns the holdout into "
    "training data. Keep your own log of run_id and the fingerprints below and count every "
    "re-run in extra_trials."
)


# ── Deflated Sharpe ratio ────────────────────────────────────────────────────


def expected_max_sharpe(n_trials: int, trials_sr_variance: float) -> float:
    """SR0: expected maximum per-period Sharpe among ``n_trials`` skill-less trials.

    SR0 = sqrt(V) * ((1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N e))), g = Euler-Mascheroni.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be at least 1")
    if n_trials == 1 or trials_sr_variance <= 0:
        return 0.0
    normal = NormalDist()
    return math.sqrt(trials_sr_variance) * (
        (1.0 - EULER_MASCHERONI) * normal.inv_cdf(1.0 - 1.0 / n_trials)
        + EULER_MASCHERONI * normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(
    *,
    sharpe_hat: float,
    n_obs: int,
    skew: float,
    kurtosis: float,
    n_trials: int,
    trials_sr_variance: float,
) -> float | None:
    """Probability that the true Sharpe exceeds SR0 after correcting for selection bias.

    DSR = Phi( (SR_hat - SR0) * sqrt(T - 1) / sqrt(1 - skew * SR_hat + (kurtosis - 1)/4 * SR_hat^2) )

    All Sharpe values are per-period (not annualised); ``kurtosis`` is raw (normal = 3).
    Returns None when the variance term is not positive or there are fewer than 2 observations.
    """
    if n_obs < 2:
        return None
    sr0 = expected_max_sharpe(n_trials, trials_sr_variance)
    denominator = 1.0 - skew * sharpe_hat + (kurtosis - 1.0) / 4.0 * sharpe_hat**2
    if denominator <= 0:
        return None
    z = (sharpe_hat - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denominator)
    return NormalDist().cdf(z)


def _moments(returns: list[float]) -> tuple[float, float]:
    n = len(returns)
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 <= 0:
        return 0.0, 3.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return m3 / m2**1.5, m4 / m2**2


def deflated_sharpe_for_returns(
    returns: list[float],
    *,
    risk_free_annual: float,
    n_trials: int,
    trials_sr_variance: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "n_trials": n_trials,
        "trials_sr_variance": trials_sr_variance,
        "n_obs": len(returns),
        "sharpe_per_period": None,
        "sharpe_annualised": None,
        "expected_max_sharpe_per_period": expected_max_sharpe(n_trials, trials_sr_variance),
        "skew": None,
        "kurtosis": None,
        "deflated_sharpe_probability": None,
    }
    annual = sharpe_ratio(returns, risk_free_annual)
    if annual is None or len(returns) < 2:
        return result
    excess = [value - daily_risk_free(risk_free_annual) for value in returns]
    skew, kurtosis = _moments(excess)
    per_period = annual / math.sqrt(TRADING_DAYS_PER_YEAR)
    result.update(
        sharpe_per_period=per_period,
        sharpe_annualised=annual,
        skew=skew,
        kurtosis=kurtosis,
        deflated_sharpe_probability=deflated_sharpe_ratio(
            sharpe_hat=per_period,
            n_obs=len(returns),
            skew=skew,
            kurtosis=kurtosis,
            n_trials=n_trials,
            trials_sr_variance=trials_sr_variance,
        ),
    )
    return result


# ── Configuration and results ────────────────────────────────────────────────


@dataclass(frozen=True)
class WalkForwardConfig:
    initial_train_bars: int
    test_bars: int
    holdout_bars: int
    min_rebalances_per_window: int = 3
    min_valid_windows: int = MIN_VALID_WINDOWS
    extra_trials: int = 0
    # The deflated-Sharpe gate is mandatory.  None means "the server default"; a value below
    # the server floor (0.95) is enforced at the floor and also fails the gate (see gate_floors).
    min_deflated_sharpe_probability: float | None = MIN_DEFLATED_SHARPE_PROBABILITY

    def __post_init__(self) -> None:
        if self.initial_train_bars < 2 or self.test_bars < 2 or self.holdout_bars < 2:
            raise ValueError("initial_train_bars, test_bars and holdout_bars must each be >= 2")
        if self.min_rebalances_per_window < 1 or self.min_valid_windows < 1:
            raise ValueError("min_rebalances_per_window and min_valid_windows must be >= 1")
        if self.extra_trials < 0:
            raise ValueError("extra_trials cannot be negative")


@dataclass
class WalkForwardWindow:
    index: int  # -1 for the holdout
    kind: str  # "window" | "holdout"
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_bars: int
    test_bars: int
    selected_index: int
    selected_params: dict[str, Any]
    train_sharpe: float | None
    candidate_train_sharpes: list[float | None]
    rebalance_count: int
    valid: bool
    invalid_reason: str | None
    total_return_pct: float
    sharpe_ratio: float | None
    max_drawdown_pct: float
    benchmark_return_pct: dict[str, float]
    excess_return_pct: dict[str, float]
    daily_returns: list[float] = field(default_factory=list, repr=False)
    benchmark_daily_returns: dict[str, list[float]] = field(default_factory=dict, repr=False)


@dataclass
class WalkForwardResult:
    config: WalkForwardConfig
    n_grid: int
    n_trials: int
    total_sessions: int
    windows: list[WalkForwardWindow]
    holdout: WalkForwardWindow | None
    oos: dict[str, Any]
    holdout_summary: dict[str, Any]
    deflated_sharpe: dict[str, Any]
    risk_free_rate_annual: float
    cost_model: dict[str, Any]
    benchmarks_evaluated: list[str]
    passes_gate: bool
    gate_reasons: list[str]
    gate_warnings: list[str]
    benchmark_sanity: dict[str, Any] | None = None


def _train_sharpe(returns: list[float], risk_free: float) -> float | None:
    return sharpe_ratio(returns, risk_free)


def _select(sharpes: list[float | None]) -> int:
    """Index of the best train Sharpe; None ranks last; ties keep the earliest grid entry."""
    best = 0
    best_value = -math.inf
    for index, value in enumerate(sharpes):
        score = -math.inf if value is None else value
        if score > best_value:
            best, best_value = index, score
    return best


def _stitched_summary(
    windows: list[WalkForwardWindow], risk_free: float, benchmark_keys: list[str]
) -> dict[str, Any]:
    valid = [w for w in windows if w.valid]
    dropped = [w for w in windows if not w.valid]
    returns = [r for w in valid for r in w.daily_returns]
    summary: dict[str, Any] = {
        "windows_total": len(windows),
        "windows_valid": len(valid),
        # Windows below the minimum rebalance count are excluded from every statistic here.
        "windows_dropped": len(dropped),
        "dropped_window_ids": [w.index for w in dropped],
        "dropped_window_reasons": {str(w.index): w.invalid_reason for w in dropped},
        "n_obs": len(returns),
        "total_return_pct": total_return_pct_from_returns(returns) if returns else None,
        "sharpe_ratio": sharpe_ratio(returns, risk_free) if returns else None,
        "max_drawdown_pct": max_drawdown_pct_from_returns(returns) if returns else None,
        "benchmarks": {},
    }
    for key in benchmark_keys:
        bench = [r for w in valid for r in w.benchmark_daily_returns.get(key, [])]
        bench_total = total_return_pct_from_returns(bench) if bench else None
        bench_sharpe = sharpe_ratio(bench, risk_free) if bench else None
        strat_total = summary["total_return_pct"]
        strat_sharpe = summary["sharpe_ratio"]
        summary["benchmarks"][key] = {
            "total_return_pct": bench_total,
            "sharpe_ratio": bench_sharpe,
            "max_drawdown_pct": max_drawdown_pct_from_returns(bench) if bench else None,
            "excess_return_pct": (
                strat_total - bench_total
                if strat_total is not None and bench_total is not None
                else None
            ),
            "excess_sharpe_ratio": (
                strat_sharpe - bench_sharpe
                if strat_sharpe is not None and bench_sharpe is not None
                else None
            ),
        }
    return summary


def window_excess_return_pct(window: WalkForwardWindow, key: str) -> float | None:
    """Strategy minus benchmark total return for one window, from unrounded daily returns."""
    bench = window.benchmark_daily_returns.get(key)
    if not window.daily_returns or not bench:
        return None
    return total_return_pct_from_returns(window.daily_returns) - total_return_pct_from_returns(
        bench
    )


def evaluate_gate(
    *,
    config: WalkForwardConfig,
    windows: list[WalkForwardWindow],
    holdout: WalkForwardWindow,
    oos: dict[str, Any],
    holdout_summary: dict[str, Any],
    deflated: dict[str, Any],
    benchmark_keys: list[str],
    cost_model: dict[str, Any],
    sanity: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Gate reasons (any reason fails the gate) and warnings.

    Server floors in ``gate_floors`` apply whatever the request asked for: settings below a
    floor are themselves a failure reason, and the enforced thresholds never drop below them.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    if cost_reason := cost_gate_refusal(cost_model):
        reasons.append(cost_reason)
    reasons.extend(
        config_floor_violations(
            holdout_bars=config.holdout_bars,
            test_bars=config.test_bars,
            min_valid_windows=config.min_valid_windows,
            min_rebalances_per_window=config.min_rebalances_per_window,
            min_deflated_sharpe_probability=config.min_deflated_sharpe_probability,
        )
    )
    required_windows = max(config.min_valid_windows, MIN_VALID_WINDOWS)
    if oos["windows_valid"] < required_windows:
        reasons.append(
            f"only {oos['windows_valid']} valid out-of-sample window(s); need {required_windows}"
        )
    if not holdout.valid:
        reasons.append(f"holdout invalid: {holdout.invalid_reason}")
    if BENCHMARK_BROAD_INDEX not in benchmark_keys:
        warnings.append(
            "no broad-index benchmark supplied; only the same-universe benchmark was tested"
        )
        reasons.append(
            "no broad-index benchmark series supplied: a positive verdict needs a broad-index comparison"
        )
    if BENCHMARK_BROAD_INDEX in benchmark_keys:
        if sanity is None:
            reasons.append("broad-index benchmark could not be sanity-checked")
        else:
            reasons.extend(sanity["reasons"])
    valid_windows = [w for w in windows if w.valid]
    for key in benchmark_keys:
        oos_bench = oos["benchmarks"][key]
        if oos_bench["excess_return_pct"] is None or oos_bench["excess_return_pct"] <= 0:
            reasons.append(f"out-of-sample return does not beat costed benchmark {key}")
        if oos_bench["excess_sharpe_ratio"] is None or oos_bench["excess_sharpe_ratio"] <= 0:
            reasons.append(f"out-of-sample Sharpe does not beat costed benchmark {key}")
        # Stitched totals can be carried by one lucky window: most windows must win on their own.
        wins = sum(1 for w in valid_windows if (window_excess_return_pct(w, key) or 0.0) > 0)
        if not valid_windows or wins <= MIN_WINDOW_WIN_FRACTION * len(valid_windows):
            reasons.append(
                f"only {wins} of {len(valid_windows)} valid windows beat costed benchmark {key}; "
                "need a majority"
            )
        if holdout.excess_return_pct.get(key, -1.0) <= 0:
            reasons.append(f"holdout return does not beat costed benchmark {key}")
        holdout_excess_sharpe = (
            holdout_summary["benchmarks"].get(key, {}).get("excess_sharpe_ratio")
        )
        if holdout_excess_sharpe is None or holdout_excess_sharpe <= 0:
            reasons.append(f"holdout Sharpe does not beat costed benchmark {key}")
    threshold = effective_deflated_sharpe_threshold(config.min_deflated_sharpe_probability)
    probability = deflated["oos"]["deflated_sharpe_probability"]
    if probability is None or probability < threshold:
        reasons.append(
            f"deflated Sharpe probability {probability} is below the required {threshold}"
        )
    return reasons, warnings


def run_portfolio_walk_forward(
    *,
    strategy_class: Callable[[dict[str, Any] | None], Any],
    base_params: dict[str, Any] | None,
    param_grid: list[dict[str, Any]],
    histories: dict[str, tuple[list[Bar], list[datetime]]],
    universe: list[str],
    start_date: date,
    end_date: date,
    config: WalkForwardConfig,
    initial_capital: Decimal = Decimal("10000"),
    cost_model: TransactionCostModel | None = None,
    risk_free_rate: Decimal = Decimal("0"),
    benchmark_history: tuple[list[Bar], list[datetime]] | None = None,
    benchmark_ticker: str = "BROAD_INDEX",
    coverage_policy: PortfolioCoveragePolicy | None = None,
) -> WalkForwardResult:
    if not param_grid:
        raise ValueError("param_grid must contain at least one parameter set")
    if len(param_grid) > MAX_GRID_SIZE:
        raise ValueError(f"param_grid is limited to {MAX_GRID_SIZE} entries")

    cost_model = cost_model or TransactionCostModel()
    coverage_policy = coverage_policy or PortfolioCoveragePolicy()
    universe = sorted(dict.fromkeys(universe))
    risk_free = float(risk_free_rate)

    dates, history, coverage_report = _align_research_histories(
        histories,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        coverage_policy=coverage_policy,
    )
    bench_bars: list[Bar] | None = None
    if benchmark_history is not None:
        bench_bars = _align_benchmark_bars(
            benchmark_history,
            dates,
            ticker=benchmark_ticker.strip().upper(),
            calendar_venue=coverage_policy.calendar_venue,
        )

    candidates = [{**(base_params or {}), **entry} for entry in param_grid]
    max_warmup = max(int(strategy_class(params).min_history_bars) for params in candidates)
    if config.initial_train_bars <= max_warmup:
        raise ValueError(
            f"initial_train_bars ({config.initial_train_bars}) must exceed the largest "
            f"strategy warm-up in the grid ({max_warmup})"
        )

    total = len(dates)
    pre_holdout_end = total - config.holdout_bars
    if pre_holdout_end < config.initial_train_bars + config.test_bars:
        raise ValueError(
            f"{total} aligned sessions are too few for initial_train_bars + test_bars + "
            f"holdout_bars = {config.initial_train_bars + config.test_bars + config.holdout_bars}"
        )

    def run_slice(params: dict[str, Any], end: int, *, trade_start: int | None) -> Any:
        backtester = PortfolioBacktester(
            strategy=strategy_class(params),
            universe=universe,
            initial_capital=initial_capital,
            start_date=dates[0],
            end_date=dates[end - 1],
            coverage_policy=coverage_policy,
            cost_model=cost_model,
            risk_free_rate=risk_free_rate,
            benchmark_ticker=benchmark_ticker,
            trade_start_date=dates[trade_start] if trade_start is not None else None,
        )
        return backtester.run_aligned(
            dates[:end],
            {ticker: bars[:end] for ticker, bars in history.items()},
            coverage_report,
            benchmark_bars=bench_bars[:end] if bench_bars is not None else None,
        )

    def fit(train_end: int) -> tuple[int, list[float | None]]:
        """Train Sharpe per candidate on sessions [0, train_end); returns (winner, sharpes)."""
        sharpes = [
            _train_sharpe(run_slice(params, train_end, trade_start=None).daily_returns, risk_free)
            for params in candidates
        ]
        return _select(sharpes), sharpes

    def evaluate(
        *,
        index: int,
        kind: str,
        train_end: int,
        test_end: int,
        winner: int,
        sharpes: list[float | None],
    ) -> WalkForwardWindow:
        result = run_slice(candidates[winner], test_end, trade_start=train_end)
        valid = result.rebalance_count >= config.min_rebalances_per_window
        benchmark_keys = [k for k in GATED_BENCHMARKS if k in result.benchmarks]
        return WalkForwardWindow(
            index=index,
            kind=kind,
            train_start=dates[0],
            train_end=dates[train_end - 1],
            test_start=dates[train_end],
            test_end=dates[test_end - 1],
            train_bars=train_end,
            test_bars=test_end - train_end,
            selected_index=winner,
            selected_params=dict(candidates[winner]),
            train_sharpe=sharpes[winner],
            candidate_train_sharpes=list(sharpes),
            rebalance_count=result.rebalance_count,
            valid=valid,
            invalid_reason=(
                None
                if valid
                else f"{result.rebalance_count} rebalances < {config.min_rebalances_per_window}"
            ),
            total_return_pct=float(result.total_return_pct),
            sharpe_ratio=float(result.sharpe_ratio) if result.sharpe_ratio is not None else None,
            max_drawdown_pct=float(result.max_drawdown_pct),
            benchmark_return_pct={
                k: float(result.benchmarks[k].total_return_pct) for k in benchmark_keys
            },
            excess_return_pct={
                k: float(result.benchmarks[k].excess_return_pct) for k in benchmark_keys
            },
            daily_returns=list(result.daily_returns),
            benchmark_daily_returns={
                k: list(result.benchmarks[k].daily_returns) for k in benchmark_keys
            },
        )

    windows: list[WalkForwardWindow] = []
    train_end = config.initial_train_bars
    while train_end + config.test_bars <= pre_holdout_end:
        winner, sharpes = fit(train_end)
        windows.append(
            evaluate(
                index=len(windows),
                kind="window",
                train_end=train_end,
                test_end=train_end + config.test_bars,
                winner=winner,
                sharpes=sharpes,
            )
        )
        train_end += config.test_bars

    # Holdout: parameters chosen on every pre-holdout session, evaluated exactly once.
    final_winner, final_sharpes = fit(pre_holdout_end)
    holdout = evaluate(
        index=-1,
        kind="holdout",
        train_end=pre_holdout_end,
        test_end=total,
        winner=final_winner,
        sharpes=final_sharpes,
    )

    n_trials = len(param_grid) + config.extra_trials
    per_period = [
        value / math.sqrt(TRADING_DAYS_PER_YEAR) for value in final_sharpes if value is not None
    ]
    trials_variance = sample_variance(per_period) if len(per_period) >= 2 else 0.0

    benchmark_keys = [k for k in GATED_BENCHMARKS if k in holdout.benchmark_return_pct]
    oos = _stitched_summary(windows, risk_free, benchmark_keys)
    holdout_summary = _stitched_summary([holdout], risk_free, benchmark_keys)
    oos_returns = [r for w in windows if w.valid for r in w.daily_returns]
    grid_variance = trials_variance
    variance_floor = None
    if n_trials > 1 and len(oos_returns) >= 2:
        # Trial-variance floor: V = max(grid variance, 1 / (T - 1)), T = stitched out-of-sample
        # observations.  1 / (T - 1) is the sampling variance of a per-period Sharpe estimate
        # for a skill-less strategy (SR = 0, normal returns).  The floor assumes the untried
        # ideas counted by ``extra_trials``, and the grid entries themselves, are at least as
        # dispersed as pure estimation noise over T observations.  It stops a grid of
        # near-duplicate entries (tiny measured variance) or a single-entry grid with
        # ``extra_trials`` (zero measured variance) from buying a weaker penalty than noise
        # alone implies.  It is an assumption, not a measurement: truly independent ideas may
        # be more dispersed (the penalty is then too small) and the grid variance is measured
        # on the train period, not out of sample.
        variance_floor = 1.0 / (len(oos_returns) - 1)
        trials_variance = max(grid_variance, variance_floor)
    deflated = {
        "oos": deflated_sharpe_for_returns(
            oos_returns,
            risk_free_annual=risk_free,
            n_trials=n_trials,
            trials_sr_variance=trials_variance,
        ),
        "holdout": deflated_sharpe_for_returns(
            holdout.daily_returns if holdout.valid else [],
            risk_free_annual=risk_free,
            n_trials=n_trials,
            trials_sr_variance=trials_variance,
        ),
    }

    sanity = None
    if BENCHMARK_BROAD_INDEX in benchmark_keys:
        evaluated = [w for w in windows if w.valid] + [holdout]
        sanity = benchmark_sanity(
            benchmark_ticker=benchmark_ticker,
            universe=universe,
            universe_returns=[
                r
                for w in evaluated
                for r in w.benchmark_daily_returns.get(BENCHMARK_UNIVERSE_EW, [])
            ],
            benchmark_returns=[
                r
                for w in evaluated
                for r in w.benchmark_daily_returns.get(BENCHMARK_BROAD_INDEX, [])
            ],
        )
    for part in deflated.values():
        part["trials_sr_variance_grid"] = grid_variance
        part["trials_sr_variance_floor"] = variance_floor
    cost_description = cost_model.describe(tickers=universe)
    reasons, warnings = evaluate_gate(
        config=config,
        windows=windows,
        holdout=holdout,
        oos=oos,
        holdout_summary=holdout_summary,
        deflated=deflated,
        benchmark_keys=benchmark_keys,
        cost_model=cost_description,
        sanity=sanity,
    )
    passes = not reasons

    return WalkForwardResult(
        config=config,
        n_grid=len(param_grid),
        n_trials=n_trials,
        total_sessions=total,
        windows=windows,
        holdout=holdout,
        oos=oos,
        holdout_summary=holdout_summary,
        deflated_sharpe=deflated,
        risk_free_rate_annual=risk_free,
        cost_model=cost_description,
        benchmarks_evaluated=benchmark_keys,
        passes_gate=passes,
        gate_reasons=reasons,
        gate_warnings=warnings,
        benchmark_sanity=sanity,
    )


def serialize_walk_forward(result: WalkForwardResult) -> dict[str, Any]:
    def window(item: WalkForwardWindow) -> dict[str, Any]:
        return {
            "index": item.index,
            "kind": item.kind,
            "train_from": item.train_start.isoformat(),
            "train_to": item.train_end.isoformat(),
            "test_from": item.test_start.isoformat(),
            "test_to": item.test_end.isoformat(),
            "train_bars": item.train_bars,
            "test_bars": item.test_bars,
            "selected_index": item.selected_index,
            "selected_params": item.selected_params,
            "train_sharpe": item.train_sharpe,
            "candidate_train_sharpes": item.candidate_train_sharpes,
            "rebalance_count": item.rebalance_count,
            "valid": item.valid,
            "invalid_reason": item.invalid_reason,
            "total_return_pct": item.total_return_pct,
            "sharpe_ratio": item.sharpe_ratio,
            "max_drawdown_pct": item.max_drawdown_pct,
            "benchmark_return_pct": item.benchmark_return_pct,
            "excess_return_pct": item.excess_return_pct,
        }

    config = result.config
    return {
        "config": {
            "initial_train_bars": config.initial_train_bars,
            "test_bars": config.test_bars,
            "holdout_bars": config.holdout_bars,
            "min_rebalances_per_window": config.min_rebalances_per_window,
            "min_valid_windows": config.min_valid_windows,
            "extra_trials": config.extra_trials,
            "min_deflated_sharpe_probability": config.min_deflated_sharpe_probability,
        },
        "gate_floors": floors_as_dict(),
        "holdout_reuse_warning": HOLDOUT_REUSE_WARNING,
        "dropped_windows": {
            "count": result.oos["windows_dropped"],
            "ids": result.oos["dropped_window_ids"],
            "reasons": result.oos["dropped_window_reasons"],
            "min_rebalances_per_window": config.min_rebalances_per_window,
        },
        "effective_min_deflated_sharpe_probability": effective_deflated_sharpe_threshold(
            config.min_deflated_sharpe_probability
        ),
        "n_grid": result.n_grid,
        "n_trials": result.n_trials,
        "total_sessions": result.total_sessions,
        "windows": [window(item) for item in result.windows],
        "holdout": window(result.holdout) if result.holdout else None,
        "out_of_sample": result.oos,
        "holdout_summary": result.holdout_summary,
        "deflated_sharpe": result.deflated_sharpe,
        "risk_free_rate_annual": result.risk_free_rate_annual,
        "cost_model": result.cost_model,
        "benchmarks_evaluated": result.benchmarks_evaluated,
        "passes_gate": result.passes_gate,
        "gate_reasons": result.gate_reasons,
        "gate_warnings": result.gate_warnings,
        "benchmark_sanity": result.benchmark_sanity,
    }
