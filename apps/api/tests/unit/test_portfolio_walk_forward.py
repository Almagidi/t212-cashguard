from __future__ import annotations

import json
import random
from decimal import Decimal

import pytest

from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_strategies import TrendFollowingTacticalStrategy
from app.backtest.portfolio_walk_forward import (
    WalkForwardConfig,
    _select,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    run_portfolio_walk_forward,
    serialize_walk_forward,
)
from tests.unit.portfolio_synth import synth_history

LENGTH = 720
TRAIN, TEST, HOLDOUT = 300, 100, 120
GRID = [{"sma_period": 20}, {"sma_period": 50}, {"sma_period": 100}]
TICKERS = ["AAA", "BBB", "CCC"]


def _histories(length: int = LENGTH):
    return {
        ticker: synth_history(length, seed=40 + i, drift=0.0003 + 0.0001 * i)
        for i, ticker in enumerate(TICKERS)
    }


def _perturb(histories, *, from_index: int, shuffle: bool):
    """Return a copy where bars at/after ``from_index`` are reordered (times unchanged)."""
    out = {}
    for ticker, (bars, times) in histories.items():
        head, tail = list(bars[:from_index]), list(bars[from_index:])
        if shuffle:
            random.Random(7).shuffle(tail)
        out[ticker] = (head + tail, list(times))
    return out


def _run(histories, **overrides):
    _, times = next(iter(histories.values()))
    kwargs = {
        "strategy_class": TrendFollowingTacticalStrategy,
        "base_params": None,
        "param_grid": GRID,
        "histories": histories,
        "universe": TICKERS,
        "start_date": times[0].date(),
        "end_date": times[-1].date(),
        "config": WalkForwardConfig(
            initial_train_bars=TRAIN,
            test_bars=TEST,
            holdout_bars=HOLDOUT,
            min_rebalances_per_window=1,
            min_valid_windows=1,
        ),
    }
    kwargs.update(overrides)
    return run_portfolio_walk_forward(**kwargs)


@pytest.fixture(scope="module")
def baseline():
    histories = _histories()
    return histories, _run(histories)


def test_windows_are_disjoint_expanding_and_ordered(baseline) -> None:
    _, result = baseline

    assert len(result.windows) == 3
    previous_test_end = None
    for window in result.windows:
        assert window.train_end < window.test_start
        assert window.test_bars == TEST
        assert window.train_start == result.windows[0].train_start  # expanding from the start
        if previous_test_end is not None:
            assert window.test_start > previous_test_end  # disjoint
            assert window.train_end == previous_test_end  # train grows to the previous test end
        previous_test_end = window.test_end
    assert [w.train_bars for w in result.windows] == [300, 400, 500]
    assert result.holdout is not None
    assert result.holdout.test_start > result.windows[-1].test_end
    assert result.holdout.test_bars == HOLDOUT
    assert result.holdout.train_bars == LENGTH - HOLDOUT
    assert result.holdout.kind == "holdout"


def test_shuffling_data_after_a_cutoff_does_not_change_earlier_selections(baseline) -> None:
    histories, original = baseline
    cutoff = original.windows[1].train_bars  # window 1 trains on sessions [0, 400)
    shuffled = _run(_perturb(histories, from_index=cutoff, shuffle=True))

    for index in (0, 1):
        a, b = original.windows[index], shuffled.windows[index]
        assert a.selected_index == b.selected_index
        assert a.selected_params == b.selected_params
        assert a.candidate_train_sharpes == b.candidate_train_sharpes
    # sanity: the shuffle really changed later information
    assert (
        original.windows[2].candidate_train_sharpes != shuffled.windows[2].candidate_train_sharpes
    )
    assert original.windows[1].total_return_pct != shuffled.windows[1].total_return_pct


def test_holdout_data_never_influences_any_selection(baseline) -> None:
    histories, original = baseline
    mutated = _run(_perturb(histories, from_index=LENGTH - HOLDOUT, shuffle=True))

    for a, b in zip(original.windows, mutated.windows, strict=True):
        assert a.candidate_train_sharpes == b.candidate_train_sharpes
        assert a.total_return_pct == b.total_return_pct
    assert original.holdout is not None
    assert mutated.holdout is not None
    assert original.holdout.candidate_train_sharpes == mutated.holdout.candidate_train_sharpes
    assert original.holdout.selected_params == mutated.holdout.selected_params
    assert original.holdout.total_return_pct != mutated.holdout.total_return_pct


def test_selection_uses_train_sharpe_only_and_ties_keep_first_grid_entry(baseline) -> None:
    _, result = baseline
    for window in [*result.windows, result.holdout]:
        sharpes = window.candidate_train_sharpes
        assert window.selected_index == _select(sharpes)
        assert window.train_sharpe == max(s for s in sharpes if s is not None)
        assert window.selected_params == GRID[window.selected_index]
    assert _select([1.0, 1.0, 0.5]) == 0
    assert _select([None, 0.1, 0.1]) == 1
    assert _select([None, None]) == 0


def test_number_of_trials_and_deflated_sharpe_are_recorded() -> None:
    histories = _histories()
    result = _run(
        histories,
        config=WalkForwardConfig(
            initial_train_bars=TRAIN,
            test_bars=TEST,
            holdout_bars=HOLDOUT,
            min_rebalances_per_window=1,
            min_valid_windows=1,
            extra_trials=7,
        ),
        risk_free_rate=Decimal("0.01"),
    )

    assert result.n_grid == 3
    assert result.n_trials == 10
    oos = result.deflated_sharpe["oos"]
    assert oos["n_trials"] == 10
    assert oos["n_obs"] == sum(len(w.daily_returns) for w in result.windows if w.valid)
    assert (
        oos["deflated_sharpe_probability"] is None
        or 0.0 <= oos["deflated_sharpe_probability"] <= 1.0
    )
    assert oos["trials_sr_variance"] > 0
    assert result.risk_free_rate_annual == 0.01
    payload = serialize_walk_forward(result)
    json.dumps(payload)  # JSON-safe
    assert payload["n_trials"] == 10
    assert payload["holdout"]["kind"] == "holdout"
    assert payload["cost_model"]["label"] == "standard"


def test_expected_max_sharpe_and_deflated_sharpe_match_hand_computation() -> None:
    # N=10 trials, variance of trial Sharpes 0.0004 (sd 0.02):
    #   Phi^-1(1-1/10)=1.2815516, Phi^-1(1-1/(10e))=1.7892418, g=0.5772157
    #   SR0 = 0.02 * ((1-g)*1.2815516 + g*1.7892418) = 0.02 * 1.5745984 = 0.0314920
    sr0 = expected_max_sharpe(10, 0.0004)
    assert sr0 == pytest.approx(0.031492, abs=2e-6)

    # SR_hat=0.1, T=101, normal returns: denominator sqrt(1 + 0.5*0.01)=1.0024969,
    #   z = (0.1-0.031492)*10/1.0024969 = 0.683374, DSR = Phi(z) = 0.752815
    dsr = deflated_sharpe_ratio(
        sharpe_hat=0.1, n_obs=101, skew=0.0, kurtosis=3.0, n_trials=10, trials_sr_variance=0.0004
    )
    assert dsr == pytest.approx(0.752815, abs=1e-5)

    # Negative skew and fat tails widen the denominator (1 + 0.05 + 0.0075 = 1.0575) and lower DSR.
    skewed = deflated_sharpe_ratio(
        sharpe_hat=0.1, n_obs=101, skew=-0.5, kurtosis=4.0, n_trials=10, trials_sr_variance=0.0004
    )
    assert skewed == pytest.approx(0.747357, abs=1e-5)
    assert skewed < dsr

    # One trial: SR0 = 0 and DSR reduces to the probabilistic Sharpe ratio, Phi(0.997509) = 0.840741.
    single = deflated_sharpe_ratio(
        sharpe_hat=0.1, n_obs=101, skew=0.0, kurtosis=3.0, n_trials=1, trials_sr_variance=0.0004
    )
    assert single == pytest.approx(0.840741, abs=1e-5)
    # More trials always raise the bar.
    assert expected_max_sharpe(100, 0.0004) > sr0
    assert (
        deflated_sharpe_ratio(
            sharpe_hat=0.1, n_obs=1, skew=0.0, kurtosis=3.0, n_trials=3, trials_sr_variance=0.0
        )
        is None
    )


def test_windows_below_minimum_rebalances_are_invalid_and_fail_the_gate() -> None:
    result = _run(
        _histories(),
        config=WalkForwardConfig(
            initial_train_bars=TRAIN,
            test_bars=TEST,
            holdout_bars=HOLDOUT,
            min_rebalances_per_window=999,
            min_valid_windows=1,
        ),
    )

    assert all(not w.valid for w in result.windows)
    assert all("rebalances <" in (w.invalid_reason or "") for w in result.windows)
    assert result.oos["windows_valid"] == 0
    assert result.oos["total_return_pct"] is None
    assert result.passes_gate is False
    assert any("valid out-of-sample" in reason for reason in result.gate_reasons)
    assert any("holdout invalid" in reason for reason in result.gate_reasons)


def test_zero_cost_walk_forward_can_never_pass_the_gate() -> None:
    result = _run(_histories(), cost_model=TransactionCostModel.explicit_zero())
    assert result.passes_gate is False
    assert any("zero-cost" in reason for reason in result.gate_reasons)
    assert result.cost_model["label"] == "zero_cost_explicit"


def test_index_benchmark_is_evaluated_per_window_and_in_aggregate() -> None:
    histories = _histories()
    result = _run(
        histories,
        benchmark_history=synth_history(LENGTH, seed=99, drift=0.0004, vol=0.008),
        benchmark_ticker="SPY",
    )

    assert result.benchmarks_evaluated == ["universe_equal_weight_costed", "broad_index"]
    for window in result.windows:
        assert set(window.excess_return_pct) == set(result.benchmarks_evaluated)
    assert set(result.oos["benchmarks"]) == set(result.benchmarks_evaluated)
    assert "excess_sharpe_ratio" in result.oos["benchmarks"]["broad_index"]
    assert not result.gate_warnings  # a broad index was supplied


def test_missing_broad_index_is_reported_as_a_warning() -> None:
    _, result = None, _run(_histories())
    assert result.benchmarks_evaluated == ["universe_equal_weight_costed"]
    assert any("broad-index" in warning for warning in result.gate_warnings)


def test_invalid_configurations_are_rejected() -> None:
    histories = _histories()
    with pytest.raises(ValueError, match="at least one parameter set"):
        _run(histories, param_grid=[])
    with pytest.raises(ValueError, match="must exceed the largest strategy warm-up"):
        _run(histories, param_grid=[{"sma_period": TRAIN}])
    with pytest.raises(ValueError, match="too few"):
        _run(
            histories,
            config=WalkForwardConfig(initial_train_bars=TRAIN, test_bars=TEST, holdout_bars=400),
        )
    with pytest.raises(ValueError, match="each be >= 2"):
        WalkForwardConfig(initial_train_bars=1, test_bars=10, holdout_bars=10)
