from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.api.v1.routes import backtest as route
from app.api.v1.routes.backtest import PortfolioBacktestRequest, PortfolioCostRequest
from tests.unit.portfolio_synth import synth_history

LENGTH = 260


def _window() -> dict[str, Any]:
    _, times = synth_history(LENGTH, seed=1)
    return {"from_date": times[0].date(), "to_date": times[-1].date()}


class FakeFetcher:
    """Serves synthetic bars; never touches the network."""

    def __init__(self, api_key: str) -> None:
        del api_key
        self.requested: list[str] = []

    async def fetch_bars(self, *, ticker: str, **kwargs: Any):
        del kwargs
        self.requested.append(ticker)
        seeds = {"AAA": 31, "BBB": 32, "SPY": 33}
        return synth_history(LENGTH, seed=seeds[ticker])

    def manifest_for(self, ticker: str) -> dict[str, Any]:
        return {"manifest_id": f"fake-{ticker}"}


def test_request_cost_defaults_and_validation() -> None:
    body = PortfolioBacktestRequest(tickers=["aaa"], **_window())
    model = body.cost_model.to_model()

    assert body.risk_free_rate_annual == 0.0
    assert body.primary_benchmark == "universe_equal_weight_costed"
    assert model.account_currency == "GBP"
    assert str(model.fx_fee_rate) == "0.0015"
    assert model.is_foreign("AAA")

    with pytest.raises(ValidationError):
        PortfolioCostRequest(stress_multiplier=4)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="explicitly"):
        PortfolioCostRequest(spread_bps=0, slippage_bps=0, commission_bps=0, fx_fee_rate=0)
    zero = PortfolioCostRequest(zero_cost=True).to_model()
    assert zero.label == "zero_cost_explicit"
    with pytest.raises(ValidationError, match="needs benchmark_ticker"):
        PortfolioBacktestRequest(tickers=["AAA"], primary_benchmark="broad_index", **_window())


@pytest.mark.asyncio
async def test_job_reports_costs_benchmarks_and_risk_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.backtest.data_fetcher.BacktestDataFetcher", FakeFetcher)
    body = PortfolioBacktestRequest(
        tickers=["AAA", "BBB"],
        strategy_type="equal_weight_rebalance",
        benchmark_ticker="spy",
        primary_benchmark="broad_index",
        risk_free_rate_annual=0.02,
        cost_model=PortfolioCostRequest(stress_multiplier=2, instrument_currencies={"AAA": "GBP"}),
        **_window(),
    )
    await route._run_portfolio_backtest_job("pf-test-costs", body)
    job = route._portfolio_jobs.pop("pf-test-costs")

    assert job["status"] == "complete", job
    result = job["result"]
    assert result["cost_model"]["stress_multiplier"] == 2.0
    assert result["cost_model"]["label"] == "standard"
    assert result["risk_free_rate_annual"] == 0.02
    assert result["primary_benchmark"] == "broad_index"
    assert set(result["benchmarks"]) == {
        "universe_equal_weight_costed",
        "broad_index",
        "exposure_matched_blend",
    }
    assert "excess_sharpe_ratio" in result["benchmarks"]["broad_index"]
    assert result["benchmark_name"] == result["benchmarks"]["broad_index"]["name"]
    assert result["warmup_bars_excluded"] == 0
    assert {d["manifest_id"] for d in job["datasets"]} == {"fake-AAA", "fake-BBB", "fake-SPY"}
    fx_by_ticker = {t["ticker"]: t["fx_fee_cost"] for t in result["trades"]}
    assert fx_by_ticker["AAA"] == 0.0  # GBP-listed
    assert fx_by_ticker["BBB"] > 0.0  # unknown currency => foreign


# ── verdict gating ───────────────────────────────────────────────────────────


def _fake_result(*, sharpe: float, total_return: float = 20.0, zero_cost: bool = False):
    from decimal import Decimal
    from types import SimpleNamespace

    return SimpleNamespace(
        coverage_report=SimpleNamespace(complete=True, retained_coverage_pct=Decimal("100")),
        sharpe_ratio=Decimal(str(sharpe)),
        total_return_pct=Decimal(str(total_return)),
        max_drawdown_pct=Decimal("10"),
        rebalance_count=12,
        turnover_pct=Decimal("50"),
        avg_exposure_pct=Decimal("90"),
        cost_model={
            "label": "zero_cost_explicit" if zero_cost else "standard",
            "zero_cost": zero_cost,
            "min_round_trip_cost_bps": 0.0 if zero_cost else 54.0,
        },
    )


def _fake_wf(
    *,
    passes: bool,
    reasons: list[str] | None = None,
    sharpe: float = 0.8,
    total_return: float = 10.0,
    benchmarks: tuple[str, ...] = ("universe_equal_weight_costed", "broad_index"),
    **config_overrides: Any,
):
    from types import SimpleNamespace

    evidence = {"sharpe_ratio": sharpe, "total_return_pct": total_return, "max_drawdown_pct": 10.0}

    config = {
        "holdout_bars": 126,
        "test_bars": 63,
        "min_valid_windows": 3,
        "min_rebalances_per_window": 3,
        "min_deflated_sharpe_probability": 0.95,
        **config_overrides,
    }
    return SimpleNamespace(
        config=SimpleNamespace(**config),
        passes_gate=passes,
        gate_reasons=reasons
        or ([] if passes else ["out-of-sample return does not beat costed benchmark broad_index"]),
        gate_warnings=[],
        n_trials=12,
        oos=dict(evidence),
        holdout_summary=dict(evidence),
        benchmarks_evaluated=list(benchmarks),
    )


@pytest.mark.parametrize(("sharpe", "label"), [(0.8, "promising"), (1.3, "strong")])
def test_positive_verdicts_are_refused_without_walk_forward(sharpe: float, label: str) -> None:
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=sharpe))

    assert interpretation["verdict"] == "unvalidated"
    assert interpretation["verdict"] not in {"promising", "strong"}
    assert interpretation["validation"]["refused_verdict"] == label
    assert interpretation["validation"]["walk_forward_run"] is False
    assert "no out-of-sample" in interpretation["validation"]["refusal_reason"]
    assert any("withheld" in warning for warning in interpretation["warnings"])


@pytest.mark.parametrize("sharpe", [0.8, 1.3])
def test_positive_verdicts_are_refused_when_walk_forward_does_not_beat_benchmark(
    sharpe: float,
) -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=sharpe), _fake_wf(passes=False, sharpe=sharpe)
    )
    assert interpretation["verdict"] == "unvalidated"
    assert "does not beat costed benchmark" in interpretation["validation"]["refusal_reason"]
    assert interpretation["validation"]["n_trials"] == 12


@pytest.mark.parametrize(("sharpe", "label"), [(0.8, "promising"), (1.3, "strong")])
def test_positive_verdicts_allowed_only_when_walk_forward_gate_passes(
    sharpe: float, label: str
) -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=sharpe), _fake_wf(passes=True, sharpe=sharpe)
    )
    assert interpretation["verdict"] == label
    assert interpretation["validation"]["passes_gate"] is True
    assert "refused_verdict" not in interpretation["validation"]


def test_zero_cost_run_cannot_be_promising_even_with_a_passing_walk_forward() -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=1.3, zero_cost=True), _fake_wf(passes=True, sharpe=1.3)
    )
    assert interpretation["verdict"] == "unvalidated"
    assert "zero-cost" in interpretation["validation"]["refusal_reason"]


def test_non_positive_verdicts_are_unchanged_by_gating() -> None:
    losing = route._interpret_portfolio_results(_fake_result(sharpe=0.8, total_return=-3.0))
    marginal = route._interpret_portfolio_results(_fake_result(sharpe=0.1))
    assert losing["verdict"] == "losing"
    assert marginal["verdict"] == "marginal"
    assert "refused_verdict" not in marginal["validation"]


class LongFakeFetcher(FakeFetcher):
    async def fetch_bars(self, *, ticker: str, **kwargs: Any):
        del kwargs
        seeds = {"AAA": 31, "BBB": 32, "SPY": 33}
        return synth_history(720, seed=seeds[ticker])


@pytest.mark.asyncio
async def test_job_runs_optional_walk_forward_and_never_labels_it_promising_unvalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.backtest.data_fetcher.BacktestDataFetcher", LongFakeFetcher)
    _, times = synth_history(720, seed=1)
    grid = [{"sma_period": 20}, {"sma_period": 50}]
    body = PortfolioBacktestRequest(
        tickers=["AAA", "BBB"],
        strategy_type="trend_following_tactical",
        benchmark_ticker="SPY",
        from_date=times[0].date(),
        to_date=times[-1].date(),
        walk_forward={
            "param_grid": grid,
            "initial_train_bars": 300,
            "test_bars": 100,
            "holdout_bars": 120,
            "min_rebalances_per_window": 1,
            "min_valid_windows": 1,
            "extra_trials": 3,
        },
    )
    await route._run_portfolio_backtest_job("pf-test-wf", body)
    job = route._portfolio_jobs.pop("pf-test-wf")

    assert job["status"] == "complete", job
    wf = job["walk_forward"]
    assert wf["n_trials"] == 5
    assert len(wf["windows"]) == 3
    assert wf["holdout"]["kind"] == "holdout"
    assert "deflated_sharpe" in wf
    validation = job["interpretation"]["validation"]
    assert validation["walk_forward_run"] is True
    assert validation["n_trials"] == 5
    assert validation["passes_gate"] == wf["passes_gate"]
    if not wf["passes_gate"]:
        assert job["interpretation"]["verdict"] not in {"promising", "strong"}
