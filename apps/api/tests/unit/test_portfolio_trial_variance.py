"""Trial-variance floor: a near-duplicate grid must not buy a weaker deflation penalty."""

from __future__ import annotations

from app.backtest.portfolio_walk_forward import WalkForwardConfig, expected_max_sharpe
from tests.unit.test_portfolio_walk_forward import HOLDOUT, TEST, TRAIN, _histories, _run


def _config(extra_trials: int = 0) -> WalkForwardConfig:
    return WalkForwardConfig(
        initial_train_bars=TRAIN,
        test_bars=TEST,
        holdout_bars=HOLDOUT,
        min_rebalances_per_window=1,
        min_valid_windows=1,
        extra_trials=extra_trials,
    )


def test_near_identical_grid_is_not_penalised_less_than_a_single_entry_with_extra_trial() -> None:
    histories = _histories()
    near_duplicate = _run(
        histories,
        param_grid=[{"sma_period": 50}, {"sma_period": 51}],
        config=_config(),
    )
    single_plus_one = _run(
        histories, param_grid=[{"sma_period": 50}], config=_config(extra_trials=1)
    )
    assert near_duplicate.n_trials == single_plus_one.n_trials == 2

    dup = near_duplicate.deflated_sharpe["oos"]
    single = single_plus_one.deflated_sharpe["oos"]
    floor = 1.0 / (dup["n_obs"] - 1)
    assert dup["trials_sr_variance"] >= floor
    assert dup["n_obs"] == single["n_obs"]
    assert dup["expected_max_sharpe_per_period"] >= single["expected_max_sharpe_per_period"] - 1e-12
    assert dup["expected_max_sharpe_per_period"] > 0
    assert single["trials_sr_variance"] == floor


def test_variance_is_the_larger_of_grid_variance_and_the_null_floor() -> None:
    result = _run(
        _histories(),
        param_grid=[{"sma_period": 20}, {"sma_period": 50}, {"sma_period": 100}],
        config=_config(),
    )
    oos = result.deflated_sharpe["oos"]
    floor = 1.0 / (oos["n_obs"] - 1)
    assert oos["trials_sr_variance_floor"] == floor
    assert oos["trials_sr_variance"] == max(oos["trials_sr_variance_grid"], floor)
    assert oos["expected_max_sharpe_per_period"] == expected_max_sharpe(
        result.n_trials, oos["trials_sr_variance"]
    )


def test_single_trial_has_no_deflation() -> None:
    result = _run(_histories(), param_grid=[{"sma_period": 50}], config=_config())
    assert result.deflated_sharpe["oos"]["expected_max_sharpe_per_period"] == 0.0
