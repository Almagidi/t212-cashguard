"""
Server-side minimums for the 'promising' / 'strong' verdict gate.

These are not request parameters.  A caller may ask for *stricter* settings, but any setting
below a floor makes the walk-forward gate fail with an explicit reason, so a request cannot
buy a positive verdict by shrinking the evidence it is judged on.  The same constants are
checked by the walk-forward harness (``passes_gate``) and again by the route that turns a
walk-forward into a verdict, so neither layer trusts the other.

All lengths are in trading sessions (daily bars); see ``docs/backtest-honesty.md``.
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any

# The untouched final slice must be long enough to say something: ~6 months of sessions.
MIN_HOLDOUT_BARS = 126
# Each out-of-sample test window: ~3 months of sessions.
MIN_TEST_WINDOW_BARS = 63
# Independent out-of-sample windows that must each be valid (enough rebalances).
MIN_VALID_WINDOWS = 3
# A window with fewer executed rebalances than this is dropped (and reported) rather than counted.
MIN_REBALANCES_PER_WINDOW = 3
# The deflated-Sharpe probability gate is mandatory; a request can only raise it.
MIN_DEFLATED_SHARPE_PROBABILITY = 0.95
# Minimum all-in round-trip (buy then sell) cost, in basis points of notional, on the cheapest
# instrument in the universe, after the stress multiplier.  Below this the costs are too close
# to frictionless to support a positive verdict.  The default model charges 24 bps (GBP listing)
# to 54 bps (foreign listing); commission minimum fees are not counted, so this is conservative.
MIN_ROUND_TRIP_COST_BPS = 10.0
# More than this fraction of valid windows must beat each gated costed benchmark
# (strictly greater than, so a 50/50 split fails).
MIN_WINDOW_WIN_FRACTION = 0.5

# The broad-index benchmark is caller-supplied, so it is sanity-checked before it can anchor a
# verdict: it must not be a strategy universe member, must actually move (a cash or T-bill ETF
# is beaten trivially) and must co-move with the universe it is meant to be a market proxy for.
MIN_BENCHMARK_ANNUAL_VOLATILITY = 0.08
MIN_BENCHMARK_CORRELATION = 0.5
_TRADING_DAYS = 252


def benchmark_sanity(
    *,
    benchmark_ticker: str,
    universe: list[str] | tuple[str, ...],
    universe_returns: list[float],
    benchmark_returns: list[float],
) -> dict[str, Any]:
    """Is ``benchmark_ticker`` a believable broad index for this universe?

    Statistics are computed over the evaluated period (valid out-of-sample windows plus the
    holdout) from daily returns of the equal-weight universe benchmark and the benchmark series.
    ``passes`` is False, with ``reasons`` filled, if the ticker is in the universe, its
    annualised volatility is below ``MIN_BENCHMARK_ANNUAL_VOLATILITY`` or its correlation with
    the universe equal-weight returns is below ``MIN_BENCHMARK_CORRELATION`` (or undefined).
    """
    ticker = benchmark_ticker.strip().upper()
    in_universe = ticker in {item.strip().upper() for item in universe}
    n = min(len(universe_returns), len(benchmark_returns))
    vol: float | None = None
    corr: float | None = None
    if len(benchmark_returns) >= 2:
        mean_b = fmean(benchmark_returns)
        var_b = sum((r - mean_b) ** 2 for r in benchmark_returns) / (len(benchmark_returns) - 1)
        vol = math.sqrt(var_b) * math.sqrt(_TRADING_DAYS)
    if n >= 2:
        u, b = universe_returns[:n], benchmark_returns[:n]
        mu, mb = fmean(u), fmean(b)
        su = sum((x - mu) ** 2 for x in u)
        sb = sum((y - mb) ** 2 for y in b)
        if su > 0 and sb > 0:
            corr = sum((x - mu) * (y - mb) for x, y in zip(u, b, strict=True)) / math.sqrt(su * sb)
    reasons: list[str] = []
    if in_universe:
        reasons.append(
            f"benchmark ticker {ticker} is a member of the strategy universe, so it is not an "
            "independent broad-index comparison"
        )
    if vol is None or vol < MIN_BENCHMARK_ANNUAL_VOLATILITY:
        shown = "undefined" if vol is None else f"{vol:.2%}"
        reasons.append(
            f"benchmark {ticker} annualised volatility {shown} is below the minimum "
            f"{MIN_BENCHMARK_ANNUAL_VOLATILITY:.0%} (cash-like series are beaten trivially)"
        )
    if corr is None or corr < MIN_BENCHMARK_CORRELATION:
        shown = "undefined" if corr is None else f"{corr:.2f}"
        reasons.append(
            f"benchmark {ticker} correlation {shown} with the universe equal-weight returns is "
            f"below the minimum {MIN_BENCHMARK_CORRELATION:.2f}"
        )
    return {
        "ticker": ticker,
        "in_universe": in_universe,
        "annualised_volatility": vol,
        "correlation_with_universe": corr,
        "n_obs": n,
        "min_annualised_volatility": MIN_BENCHMARK_ANNUAL_VOLATILITY,
        "min_correlation": MIN_BENCHMARK_CORRELATION,
        "passes": not reasons,
        "reasons": reasons,
    }


def benchmark_sanity_text(sanity: dict[str, Any]) -> str:
    vol, corr = sanity.get("annualised_volatility"), sanity.get("correlation_with_universe")
    return (
        f"broad-index benchmark {sanity.get('ticker')}: annualised volatility "
        f"{'n/a' if vol is None else f'{vol:.1%}'}, correlation with universe equal-weight "
        f"{'n/a' if corr is None else f'{corr:.2f}'}, in universe: {sanity.get('in_universe')}"
    )


def config_floor_violations(
    *,
    holdout_bars: int,
    test_bars: int,
    min_valid_windows: int,
    min_rebalances_per_window: int,
    min_deflated_sharpe_probability: float | None,
) -> list[str]:
    """Reasons the requested walk-forward settings are below the server floors (empty if none)."""
    reasons: list[str] = []
    if holdout_bars < MIN_HOLDOUT_BARS:
        reasons.append(
            f"holdout of {holdout_bars} sessions is below the server minimum {MIN_HOLDOUT_BARS}"
        )
    if test_bars < MIN_TEST_WINDOW_BARS:
        reasons.append(
            f"test window of {test_bars} sessions is below the server minimum {MIN_TEST_WINDOW_BARS}"
        )
    if min_valid_windows < MIN_VALID_WINDOWS:
        reasons.append(
            f"min_valid_windows {min_valid_windows} is below the server minimum {MIN_VALID_WINDOWS}"
        )
    if min_rebalances_per_window < MIN_REBALANCES_PER_WINDOW:
        reasons.append(
            f"min_rebalances_per_window {min_rebalances_per_window} is below the server minimum "
            f"{MIN_REBALANCES_PER_WINDOW}"
        )
    if (
        min_deflated_sharpe_probability is not None
        and min_deflated_sharpe_probability < MIN_DEFLATED_SHARPE_PROBABILITY
    ):
        reasons.append(
            f"min_deflated_sharpe_probability {min_deflated_sharpe_probability} is below the server "
            f"minimum {MIN_DEFLATED_SHARPE_PROBABILITY}"
        )
    return reasons


def cost_gate_refusal(cost_model: dict[str, Any]) -> str | None:
    """Why a cost model description (``TransactionCostModel.describe``) cannot support a gated
    verdict, or None.  The legacy flat-bps path is never gated, whatever its size."""
    if cost_model.get("label") == "legacy_flat_bps":
        return "the legacy flat-bps cost path is never eligible for a positive verdict"
    if cost_model.get("zero_cost"):
        return "zero-cost run: friction-free results cannot qualify"
    round_trip = cost_model.get("min_round_trip_cost_bps")
    if round_trip is None:
        return "the round-trip cost of the run could not be verified against the cost floor"
    if round_trip < MIN_ROUND_TRIP_COST_BPS:
        return (
            f"round-trip cost of {round_trip:g} bps is below the minimum "
            f"{MIN_ROUND_TRIP_COST_BPS:g} bps required for a positive verdict"
        )
    return None


def effective_deflated_sharpe_threshold(requested: float | None) -> float:
    """The probability actually enforced: never below the floor, whatever was requested."""
    return max(requested if requested is not None else 0.0, MIN_DEFLATED_SHARPE_PROBABILITY)


def floors_as_dict() -> dict[str, float | int]:
    return {
        "min_holdout_bars": MIN_HOLDOUT_BARS,
        "min_test_window_bars": MIN_TEST_WINDOW_BARS,
        "min_valid_windows": MIN_VALID_WINDOWS,
        "min_rebalances_per_window": MIN_REBALANCES_PER_WINDOW,
        "min_deflated_sharpe_probability": MIN_DEFLATED_SHARPE_PROBABILITY,
        "min_window_win_fraction_exclusive": MIN_WINDOW_WIN_FRACTION,
        "min_round_trip_cost_bps": MIN_ROUND_TRIP_COST_BPS,
        "min_benchmark_annualised_volatility": MIN_BENCHMARK_ANNUAL_VOLATILITY,
        "min_benchmark_correlation": MIN_BENCHMARK_CORRELATION,
    }
