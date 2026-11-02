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
        "risk_matched_blend",
    }
    assert "excess_sharpe_ratio" in result["benchmarks"]["broad_index"]
    assert result["benchmark_name"] == result["benchmarks"]["broad_index"]["name"]
    assert result["warmup_bars_excluded"] == 0
    assert {d["manifest_id"] for d in job["datasets"]} == {"fake-AAA", "fake-BBB", "fake-SPY"}
    fx_by_ticker = {t["ticker"]: t["fx_fee_cost"] for t in result["trades"]}
    assert fx_by_ticker["AAA"] == 0.0  # GBP-listed
    assert fx_by_ticker["BBB"] > 0.0  # unknown currency => foreign
