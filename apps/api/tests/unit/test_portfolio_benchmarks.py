from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import (
    BENCHMARK_BROAD_INDEX,
    BENCHMARK_EXPOSURE_MATCHED,
    BENCHMARK_UNIVERSE_EW,
    PortfolioBacktester,
)
from app.backtest.portfolio_metrics import (
    daily_risk_free,
    max_drawdown_pct_from_returns,
    sharpe_ratio,
    total_return_pct_from_returns,
)
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history

LENGTH = 300


class HalfInvestedStrategy(ConstantWeightStrategy):
    def target_weights(self, history, *, as_of_index):
        del as_of_index
        return {"AAA": Decimal("0.5")}


def _run(*, strategy=None, benchmark=True, **kwargs):
    aaa = synth_history(LENGTH, seed=21)
    index = synth_history(LENGTH, seed=22, drift=0.0003, vol=0.008)
    extra = {}
    if benchmark:
        extra = {"benchmark_history": index, "benchmark_ticker": "spy"}
    return PortfolioBacktester(
        strategy=strategy or ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=aaa[1][0].date(),
        end_date=aaa[1][-1].date(),
        **extra,
        **kwargs,
    ).run({"AAA": aaa})


def test_all_three_benchmarks_reported_with_excess_metrics() -> None:
    result = _run()

    assert set(result.benchmarks) == {
        BENCHMARK_UNIVERSE_EW,
        BENCHMARK_BROAD_INDEX,
        BENCHMARK_EXPOSURE_MATCHED,
    }
    for bench in result.benchmarks.values():
        assert bench.excess_return_pct == (
            result.total_return_pct - bench.total_return_pct
        ).quantize(Decimal("0.01"))
        assert bench.total_execution_cost > 0
        assert bench.sharpe_ratio is not None
        assert result.sharpe_ratio is not None
        assert bench.excess_sharpe_ratio == result.sharpe_ratio - bench.sharpe_ratio
        assert bench.excess_max_drawdown_pct == (
            result.max_drawdown_pct - bench.max_drawdown_pct
        ).quantize(Decimal("0.01"))
    assert result.primary_benchmark == BENCHMARK_UNIVERSE_EW
    assert result.benchmark_return_pct == result.benchmarks[BENCHMARK_UNIVERSE_EW].total_return_pct
    assert "costed" in result.benchmark_name


def test_without_index_only_universe_benchmark_exists() -> None:
    result = _run(benchmark=False)
    assert set(result.benchmarks) == {BENCHMARK_UNIVERSE_EW}


def test_benchmarks_are_net_of_the_same_costs_as_the_strategy() -> None:
    costed = _run()
    free = _run(cost_model=TransactionCostModel.explicit_zero())

    for key in (BENCHMARK_UNIVERSE_EW, BENCHMARK_BROAD_INDEX, BENCHMARK_EXPOSURE_MATCHED):
        assert costed.benchmarks[key].total_return_pct < free.benchmarks[key].total_return_pct
        assert free.benchmarks[key].total_execution_cost == 0
    assert costed.total_return_pct < free.total_return_pct
    # >= 27 bps entry cost on a fully invested index purchase (FX 15 + half spread 5 + slippage 5 + fee 2)
    broad = costed.benchmarks[BENCHMARK_BROAD_INDEX]
    assert broad.total_execution_cost >= Decimal("10000") * Decimal("0.0027") * Decimal("0.99")


def test_index_cost_uses_its_own_currency_mapping() -> None:
    gbp_index = _run(cost_model=TransactionCostModel(instrument_currencies={"SPY": "GBP"}))
    usd_index = _run()
    assert (
        gbp_index.benchmarks[BENCHMARK_BROAD_INDEX].total_execution_cost
        < usd_index.benchmarks[BENCHMARK_BROAD_INDEX].total_execution_cost
    )


def test_exposure_matched_blend_uses_strategy_average_exposure() -> None:
    result = _run(strategy=HalfInvestedStrategy())
    blend = result.benchmarks[BENCHMARK_EXPOSURE_MATCHED]
    index = result.benchmarks[BENCHMARK_BROAD_INDEX]

    assert abs(blend.avg_exposure_pct - result.avg_exposure_pct) <= Decimal("0.1")
    assert Decimal("45") < blend.avg_exposure_pct < Decimal("55")
    assert blend.max_drawdown_pct < index.max_drawdown_pct
    assert blend.total_execution_cost < index.total_execution_cost


def test_broad_index_equals_universe_benchmark_when_same_series_and_costs() -> None:
    aaa = synth_history(LENGTH, seed=21)
    result = PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=aaa[1][0].date(),
        end_date=aaa[1][-1].date(),
        benchmark_history=aaa,
        benchmark_ticker="AAA",
    ).run({"AAA": aaa})

    ew = result.benchmarks[BENCHMARK_UNIVERSE_EW]
    broad = result.benchmarks[BENCHMARK_BROAD_INDEX]
    assert ew.total_return_pct == broad.total_return_pct
    assert ew.sharpe_ratio == broad.sharpe_ratio


def test_primary_benchmark_selection_drives_legacy_fields() -> None:
    result = _run(primary_benchmark=BENCHMARK_BROAD_INDEX)
    broad = result.benchmarks[BENCHMARK_BROAD_INDEX]
    assert result.benchmark_name == broad.name
    assert result.benchmark_return_pct == broad.total_return_pct
    assert result.alpha_vs_benchmark_pct == broad.excess_return_pct


def test_benchmark_must_cover_every_portfolio_date() -> None:
    aaa = synth_history(LENGTH, seed=21)
    short = synth_history(LENGTH - 5, seed=22)
    with pytest.raises(ValueError, match="missing"):
        PortfolioBacktester(
            strategy=ConstantWeightStrategy(),
            universe=["AAA"],
            initial_capital=Decimal("10000"),
            start_date=aaa[1][0].date(),
            end_date=aaa[1][-1].date(),
            benchmark_history=short,
        ).run({"AAA": aaa})


def test_index_primary_benchmark_requires_a_series_and_unknown_key_rejected() -> None:
    base = {
        "strategy": ConstantWeightStrategy(),
        "universe": ["AAA"],
        "initial_capital": Decimal("1"),
        "start_date": synth_history(2, seed=1)[1][0].date(),
        "end_date": synth_history(2, seed=1)[1][1].date(),
    }
    with pytest.raises(ValueError, match="needs benchmark_history"):
        PortfolioBacktester(**base, primary_benchmark=BENCHMARK_BROAD_INDEX)
    with pytest.raises(ValueError, match="primary_benchmark must be one of"):
        PortfolioBacktester(**base, primary_benchmark="spy")


def test_risk_free_rate_is_configurable_documented_and_reported() -> None:
    default = _run()
    with_rf = _run(risk_free_rate=Decimal("0.08"))

    assert default.risk_free_rate_annual == Decimal("0")
    assert with_rf.risk_free_rate_annual == Decimal("0.08")
    assert default.sharpe_ratio is not None
    assert with_rf.sharpe_ratio is not None
    assert with_rf.sharpe_ratio < default.sharpe_ratio  # cash earns 0 in the simulation
    assert (
        with_rf.benchmarks[BENCHMARK_BROAD_INDEX].sharpe_ratio
        < default.benchmarks[BENCHMARK_BROAD_INDEX].sharpe_ratio
    )
    assert with_rf.total_return_pct == default.total_return_pct  # rf only touches the ratios


def test_metric_helpers_match_hand_computation() -> None:
    returns = [0.01, 0.03]
    # mean 0.02, population stdev 0.01 -> 2 * sqrt(252)
    assert sharpe_ratio(returns) == pytest.approx(2 * 252**0.5)
    rf = daily_risk_free(0.05)
    assert rf == pytest.approx(1.05 ** (1 / 252) - 1)
    assert sharpe_ratio(returns, 0.05) == pytest.approx((0.02 - rf) / 0.01 * 252**0.5)
    assert sharpe_ratio([0.01, 0.01]) is None
    assert total_return_pct_from_returns([0.1, -0.1]) == pytest.approx(-1.0)
    assert max_drawdown_pct_from_returns([0.1, -0.2, 0.05]) == pytest.approx(20.0)
