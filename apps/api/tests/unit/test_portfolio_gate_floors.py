"""Server-side floors of the walk-forward gate: a request cannot lower them."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.api.v1.routes import backtest as route
from app.api.v1.routes.backtest import PortfolioWalkForwardRequest
from app.backtest.gate_floors import (
    MIN_DEFLATED_SHARPE_PROBABILITY,
    MIN_HOLDOUT_BARS,
    MIN_REBALANCES_PER_WINDOW,
    MIN_TEST_WINDOW_BARS,
    MIN_VALID_WINDOWS,
)
from app.backtest.portfolio_strategies import TrendFollowingTacticalStrategy
from app.backtest.portfolio_walk_forward import (
    WalkForwardConfig,
    WalkForwardWindow,
    _stitched_summary,
    evaluate_gate,
    run_portfolio_walk_forward,
)
from tests.unit.portfolio_synth import synth_history
from tests.unit.test_portfolio_backtest_route import _fake_result, _fake_wf

STANDARD_COST = {"label": "standard", "zero_cost": False, "min_round_trip_cost_bps": 54.0}
BROAD = "broad_index"
EW = "universe_equal_weight_costed"


def _window(
    index: int,
    *,
    strategy: float = 0.002,
    bench: float = 0.001,
    bars: int = 63,
    rebalances: int = 4,
    valid: bool = True,
    kind: str = "window",
    keys: tuple[str, ...] = (EW, BROAD),
) -> WalkForwardWindow:
    # Alternate the sign slightly so Sharpe is defined (std > 0).
    daily = [strategy + (0.0004 if i % 2 else -0.0004) for i in range(bars)]
    bench_daily = [bench + (0.0004 if i % 2 else -0.0004) for i in range(bars)]
    start = date(2020, 1, 1) + timedelta(days=index * 100)
    return WalkForwardWindow(
        index=index,
        kind=kind,
        train_start=start,
        train_end=start,
        test_start=start,
        test_end=start,
        train_bars=300,
        test_bars=bars,
        selected_index=0,
        selected_params={},
        train_sharpe=1.0,
        candidate_train_sharpes=[1.0, 0.5],
        rebalance_count=rebalances,
        valid=valid,
        invalid_reason=None if valid else f"{rebalances} rebalances < 3",
        total_return_pct=0.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        benchmark_return_pct=dict.fromkeys(keys, 1.0),
        excess_return_pct=dict.fromkeys(keys, 1.0 if strategy > bench else -1.0),
        daily_returns=daily,
        benchmark_daily_returns=dict.fromkeys(keys, bench_daily),
    )


def _gate(
    windows: list[WalkForwardWindow],
    holdout: WalkForwardWindow,
    *,
    config: WalkForwardConfig | None = None,
    probability: float | None = 0.99,
    keys: tuple[str, ...] = (EW, BROAD),
    cost_model: dict[str, Any] | None = None,
) -> list[str]:
    config = config or WalkForwardConfig(
        initial_train_bars=300,
        test_bars=MIN_TEST_WINDOW_BARS,
        holdout_bars=MIN_HOLDOUT_BARS,
    )
    benchmark_keys = list(keys)
    oos = _stitched_summary(windows, 0.0, benchmark_keys)
    holdout_summary = _stitched_summary([holdout], 0.0, benchmark_keys)
    reasons, _ = evaluate_gate(
        config=config,
        windows=windows,
        holdout=holdout,
        oos=oos,
        holdout_summary=holdout_summary,
        deflated={"oos": {"deflated_sharpe_probability": probability}},
        benchmark_keys=benchmark_keys,
        cost_model=cost_model or STANDARD_COST,
    )
    return reasons


def _good_windows() -> list[WalkForwardWindow]:
    return [_window(i) for i in range(4)]


def _good_holdout() -> WalkForwardWindow:
    return _window(-1, bars=MIN_HOLDOUT_BARS, kind="holdout")


def test_positive_control_passes_when_every_floor_is_met() -> None:
    assert _gate(_good_windows(), _good_holdout()) == []


def test_tiny_holdout_cannot_pass_even_when_it_beats_the_benchmarks() -> None:
    config = WalkForwardConfig(
        initial_train_bars=300, test_bars=MIN_TEST_WINDOW_BARS, holdout_bars=20
    )
    reasons = _gate(_good_windows(), _window(-1, bars=20, kind="holdout"), config=config)
    assert any("holdout of 20 sessions is below the server minimum" in r for r in reasons)


def test_short_test_windows_cannot_pass() -> None:
    config = WalkForwardConfig(initial_train_bars=300, test_bars=10, holdout_bars=MIN_HOLDOUT_BARS)
    reasons = _gate(_good_windows(), _good_holdout(), config=config)
    assert any("test window of 10 sessions" in r for r in reasons)


def test_single_lucky_window_does_not_carry_stitched_totals() -> None:
    windows = [
        _window(0, strategy=0.02, bench=0.001),  # one huge win
        _window(1, strategy=0.0, bench=0.001),
        _window(2, strategy=0.0, bench=0.001),
        _window(3, strategy=0.0, bench=0.001),
    ]
    reasons = _gate(windows, _good_holdout())
    assert any("only 1 of 4 valid windows beat costed benchmark" in r for r in reasons)
    # ...even though the stitched out-of-sample return does beat the benchmark.
    assert not any("out-of-sample return does not beat" in r for r in reasons)


def test_exactly_half_of_the_windows_is_not_a_majority() -> None:
    windows = [_window(0), _window(1), _window(2, strategy=0.0), _window(3, strategy=0.0)]
    reasons = _gate(windows, _good_holdout())
    assert any("2 of 4 valid windows" in r for r in reasons)


def test_windows_dropped_for_too_few_rebalances_reduce_the_valid_count() -> None:
    windows = [_window(0), _window(1), _window(2, rebalances=1, valid=False)]
    reasons = _gate(windows, _good_holdout())
    assert any("only 2 valid out-of-sample window(s); need 3" in r for r in reasons)


def test_request_cannot_lower_window_or_rebalance_floors() -> None:
    config = WalkForwardConfig(
        initial_train_bars=300,
        test_bars=MIN_TEST_WINDOW_BARS,
        holdout_bars=MIN_HOLDOUT_BARS,
        min_valid_windows=1,
        min_rebalances_per_window=1,
    )
    windows = [_window(0), _window(1, rebalances=2)]  # two windows, 2 rebalances in one
    reasons = _gate(windows, _good_holdout(), config=config)
    assert any("min_valid_windows 1 is below the server minimum" in r for r in reasons)
    assert any("min_rebalances_per_window 1 is below the server minimum" in r for r in reasons)
    # The count that is enforced is the floor, not the lowered request.
    assert any("need 3" in r for r in reasons)


@pytest.mark.parametrize("requested", [None, 0.0, 0.5])
def test_deflated_sharpe_probability_is_mandatory_and_cannot_be_lowered(
    requested: float | None,
) -> None:
    config = WalkForwardConfig(
        initial_train_bars=300,
        test_bars=MIN_TEST_WINDOW_BARS,
        holdout_bars=MIN_HOLDOUT_BARS,
        min_deflated_sharpe_probability=requested,
    )
    reasons = _gate(_good_windows(), _good_holdout(), config=config, probability=0.90)
    assert any(f"is below the required {MIN_DEFLATED_SHARPE_PROBABILITY}" in r for r in reasons)
    missing = _gate(_good_windows(), _good_holdout(), config=config, probability=None)
    assert any("deflated Sharpe probability None" in r for r in missing)


def test_default_config_enforces_the_deflated_sharpe_gate() -> None:
    assert WalkForwardConfig(
        initial_train_bars=300, test_bars=63, holdout_bars=126
    ).min_deflated_sharpe_probability == pytest.approx(0.95)
    request = PortfolioWalkForwardRequest(
        param_grid=[{"a": 1}], initial_train_bars=300, test_bars=63, holdout_bars=126
    )
    assert request.min_deflated_sharpe_probability == pytest.approx(0.95)
    assert request.min_valid_windows == MIN_VALID_WINDOWS
    assert request.min_rebalances_per_window == MIN_REBALANCES_PER_WINDOW


def test_holdout_must_beat_the_benchmark_sharpe_not_only_its_return() -> None:
    # Higher holdout return than the benchmark but with far more volatility: lower Sharpe.
    holdout = _window(-1, strategy=0.0011, bench=0.001, bars=MIN_HOLDOUT_BARS, kind="holdout")
    noisy = [0.05 if i % 2 else -0.047 for i in range(MIN_HOLDOUT_BARS)]
    holdout.daily_returns = noisy
    holdout.excess_return_pct = dict.fromkeys((EW, BROAD), 1.0)  # return "beats" the benchmark
    reasons = _gate(_good_windows(), holdout)
    assert any("holdout Sharpe does not beat costed benchmark" in r for r in reasons)


def test_broad_index_benchmark_is_required_to_pass() -> None:
    windows = [_window(i, keys=(EW,)) for i in range(4)]
    holdout = _window(-1, bars=MIN_HOLDOUT_BARS, kind="holdout", keys=(EW,))
    reasons = _gate(windows, holdout, keys=(EW,))
    assert any("no broad-index benchmark series" in r for r in reasons)


def test_zero_cost_still_fails() -> None:
    zero = {"label": "zero_cost_explicit", "zero_cost": True, "min_round_trip_cost_bps": 0.0}
    reasons = _gate(_good_windows(), _good_holdout(), cost_model=zero)
    assert any("zero-cost" in r for r in reasons)


def test_near_zero_cost_fails_the_gate() -> None:
    cheap = {"label": "standard", "zero_cost": False, "min_round_trip_cost_bps": 3.0}
    reasons = _gate(_good_windows(), _good_holdout(), cost_model=cheap)
    assert any("round-trip cost of 3 bps" in r for r in reasons)


def test_single_entry_grid_does_not_zero_out_the_trial_penalty() -> None:
    histories = {
        t: synth_history(720, seed=50 + i, drift=0.0004) for i, t in enumerate(["AAA", "BBB"])
    }
    _, times = histories["AAA"]

    def run(extra: int) -> dict[str, Any]:
        result = run_portfolio_walk_forward(
            strategy_class=TrendFollowingTacticalStrategy,
            base_params=None,
            param_grid=[{"sma_period": 50}],
            histories=histories,
            universe=["AAA", "BBB"],
            start_date=times[0].date(),
            end_date=times[-1].date(),
            config=WalkForwardConfig(
                initial_train_bars=300,
                test_bars=100,
                holdout_bars=120,
                min_rebalances_per_window=1,
                min_valid_windows=1,
                extra_trials=extra,
            ),
        )
        return result.deflated_sharpe["oos"]

    assert run(0)["expected_max_sharpe_per_period"] == 0.0
    burned = run(30)
    assert burned["n_trials"] == 31
    assert burned["expected_max_sharpe_per_period"] > 0.0


def test_run_with_lowered_floors_fails_the_gate_end_to_end() -> None:
    histories = {
        t: synth_history(720, seed=60 + i, drift=0.0004) for i, t in enumerate(["AAA", "BBB"])
    }
    _, times = histories["AAA"]
    result = run_portfolio_walk_forward(
        strategy_class=TrendFollowingTacticalStrategy,
        base_params=None,
        param_grid=[{"sma_period": 20}, {"sma_period": 50}],
        histories=histories,
        universe=["AAA", "BBB"],
        start_date=times[0].date(),
        end_date=times[-1].date(),
        benchmark_history=synth_history(720, seed=99),
        benchmark_ticker="SPY",
        config=WalkForwardConfig(
            initial_train_bars=300,
            test_bars=100,
            holdout_bars=30,
            min_rebalances_per_window=1,
            min_valid_windows=1,
            min_deflated_sharpe_probability=0.0,
        ),
    )
    assert result.passes_gate is False
    assert any("holdout of 30 sessions" in r for r in result.gate_reasons)
    assert any("min_deflated_sharpe_probability 0.0" in r for r in result.gate_reasons)


# ── route: a walk-forward claiming a pass on sub-floor settings is still refused ──


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"holdout_bars": 20}, "holdout of 20 sessions"),
        ({"test_bars": 10}, "test window of 10 sessions"),
        ({"min_valid_windows": 1}, "min_valid_windows 1"),
        ({"min_rebalances_per_window": 1}, "min_rebalances_per_window 1"),
        ({"min_deflated_sharpe_probability": 0.5}, "min_deflated_sharpe_probability 0.5"),
    ],
)
def test_route_refuses_positive_verdict_when_config_is_below_the_floors(
    override: dict[str, Any], fragment: str
) -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=1.3), _fake_wf(passes=True, **override)
    )
    assert interpretation["verdict"] == "unvalidated"
    assert fragment in interpretation["validation"]["refusal_reason"]
