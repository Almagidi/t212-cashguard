"""Sortino, annualisation, turnover and excess-Sharpe conventions (252 sessions per year)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_metrics import (
    TRADING_DAYS_PER_YEAR,
    annualised_return_pct_from_periods,
    daily_risk_free,
    sharpe_ratio,
    sortino_ratio,
)
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history

ROOT_252 = math.sqrt(TRADING_DAYS_PER_YEAR)


def test_sortino_uses_root_mean_square_of_shortfalls_over_all_periods() -> None:
    returns = [0.01, -0.02, 0.03, -0.01, 0.0]
    # downside deviation = sqrt(mean([0, 0.0004, 0, 0.0001, 0])) = 0.01 (denominator is 5, not 2)
    expected = (sum(returns) / 5) / 0.01 * ROOT_252
    assert sortino_ratio(returns) == pytest.approx(expected)
    assert expected == pytest.approx(3.1749, abs=1e-3)


def test_sortino_is_defined_with_a_single_losing_period() -> None:
    returns = [0.02, 0.01, -0.01, 0.03]
    # The old definition took the stdev of the losing days alone, which is 0 for one loss.
    downside = math.sqrt((0.01**2) / 4)
    assert sortino_ratio(returns) == pytest.approx((0.05 / 4) / downside * ROOT_252)


def test_sortino_target_is_the_risk_free_rate_and_no_downside_means_none() -> None:
    returns = [0.004, -0.001, 0.003, -0.002]
    rf = 0.05
    target = daily_risk_free(rf)
    shortfalls = [min(r - target, 0.0) for r in returns]
    downside = math.sqrt(sum(s * s for s in shortfalls) / len(returns))
    expected = (sum(r - target for r in returns) / len(returns)) / downside * ROOT_252
    assert sortino_ratio(returns, rf) == pytest.approx(expected)
    assert sortino_ratio([0.01, 0.02, 0.03]) is None


def test_annualised_return_uses_252_sessions_per_year() -> None:
    # +21% over two years of sessions is exactly +10% a year.
    value = annualised_return_pct_from_periods(Decimal("12100"), Decimal("10000"), 504)
    assert float(value) == pytest.approx(10.0, abs=1e-6)
    assert annualised_return_pct_from_periods(Decimal("0"), Decimal("10000"), 252) == 0


def _run(seed: int = 3, **kwargs):
    history = synth_history(300, seed=seed)
    return PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
        benchmark_history=synth_history(300, seed=seed + 100),
        **kwargs,
    ).run({"AAA": history})


def test_engine_annualised_return_and_sharpe_share_the_252_day_count() -> None:
    result = _run()
    periods = len(result.daily_returns)
    growth = float(result.equity_curve[-1].equity) / 10000.0
    expected = (growth ** (TRADING_DAYS_PER_YEAR / periods) - 1.0) * 100.0
    assert float(result.annualised_return_pct) == pytest.approx(expected, abs=0.006)
    for benchmark in result.benchmarks.values():
        bench_growth = 1.0 + float(benchmark.total_return_pct) / 100.0
        bench_expected = (bench_growth ** (TRADING_DAYS_PER_YEAR / periods) - 1.0) * 100.0
        assert float(benchmark.annualised_return_pct) == pytest.approx(bench_expected, abs=0.02)


def test_turnover_is_annualised_one_way_of_average_equity() -> None:
    result = _run()
    metrics_equities = [p.equity for p in result.equity_curve[result.warmup_bars_excluded :]]
    average_equity = sum(metrics_equities) / len(metrics_equities)
    traded = sum(t.notional for t in result.trades)
    periods = len(result.daily_returns)
    one_way_total = traded / 2
    expected = float(one_way_total / average_equity) * (TRADING_DAYS_PER_YEAR / periods) * 100.0
    assert result.turnover_pct > 0
    assert float(result.turnover_pct) == pytest.approx(expected, rel=2e-3)


@pytest.mark.parametrize("seed", range(1, 31))
def test_excess_sharpe_is_computed_from_unrounded_sharpes(seed: int) -> None:
    result = _run(seed=seed)
    strategy_raw = sharpe_ratio(result.daily_returns, 0.0)
    assert strategy_raw is not None
    for benchmark in result.benchmarks.values():
        bench_raw = sharpe_ratio(benchmark.daily_returns, 0.0)
        assert bench_raw is not None
        assert benchmark.excess_sharpe_ratio is not None
        assert float(benchmark.excess_sharpe_ratio) == pytest.approx(
            round(strategy_raw - bench_raw, 3), abs=1e-9
        )
