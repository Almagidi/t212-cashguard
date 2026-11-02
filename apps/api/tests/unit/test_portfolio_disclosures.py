"""Disclosures: FX, dropped windows, holdout reuse, the renamed benchmark and the cash buffer."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.api.v1.routes import backtest as route
from app.api.v1.routes.backtest import PortfolioBacktestRequest
from app.backtest.portfolio_costs import TransactionCostModel
from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_strategies import TrendFollowingTacticalStrategy
from app.backtest.portfolio_walk_forward import (
    WalkForwardConfig,
    run_portfolio_walk_forward,
    serialize_walk_forward,
)
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history
from tests.unit.test_portfolio_backtest_route import (
    FakeFetcher,
    _fake_result,
    _fake_wf,
)

LONG = 720


class LongFetcher(FakeFetcher):
    async def fetch_bars(self, *, ticker: str, **kwargs: Any):
        del kwargs
        seeds = {"AAA": 31, "BBB": 32, "SPY": 33}
        return synth_history(LONG, seed=seeds[ticker])


def _engine_run(**kwargs: Any):
    history = synth_history(260, seed=4)
    return PortfolioBacktester(
        strategy=ConstantWeightStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
        benchmark_history=synth_history(260, seed=5),
        **kwargs,
    ).run({"AAA": history})


# ── renamed benchmark ────────────────────────────────────────────────────────


def test_blend_is_named_for_exposure_and_the_old_key_is_an_accepted_alias() -> None:
    result = _engine_run()
    assert "exposure_matched_blend" in result.benchmarks
    assert "risk_matched_blend" not in result.benchmarks
    assert result.benchmarks["exposure_matched_blend"].name.startswith("Exposure-matched blend")

    legacy = _engine_run(primary_benchmark="risk_matched_blend")
    assert legacy.primary_benchmark == "exposure_matched_blend"

    body = PortfolioBacktestRequest(
        tickers=["AAA"],
        benchmark_ticker="spy",
        primary_benchmark="risk_matched_blend",  # type: ignore[arg-type]
        from_date=synth_history(5, seed=1)[1][0].date(),
        to_date=synth_history(5, seed=1)[1][-1].date(),
    )
    assert body.primary_benchmark == "exposure_matched_blend"


# ── cash buffer ──────────────────────────────────────────────────────────────


def test_strategy_and_benchmark_share_the_same_cash_buffer() -> None:
    # A steady +0.2% a day makes any difference in the invested fraction show up in the total.
    history = synth_history(200, seed=4, drift=0.002, vol=0.0)
    result = PortfolioBacktester(
        strategy=ConstantWeightStrategy(rebalance_frequency="annual"),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=history[1][0].date(),
        end_date=history[1][-1].date(),
        cost_model=TransactionCostModel.explicit_zero(),  # costs would otherwise cap the spend
    ).run({"AAA": history})
    benchmark = result.benchmarks["universe_equal_weight_costed"]
    ratio = (1 + float(result.total_return_pct) / 100) / (
        1 + float(benchmark.total_return_pct) / 100
    )
    # A 99.8% strategy against a fully invested benchmark would differ by about 0.2%.
    assert ratio == pytest.approx(1.0, abs=1e-4)


# ── result disclosures ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_discloses_fx_annualisation_turnover_and_cash_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.backtest.data_fetcher.BacktestDataFetcher", LongFetcher)
    _, times = synth_history(LONG, seed=1)
    body = PortfolioBacktestRequest(
        tickers=["AAA", "BBB"],
        from_date=times[0].date(),
        to_date=times[-1].date(),
    )
    await route._run_portfolio_backtest_job("pf-test-disclose", body)
    job = route._portfolio_jobs.pop("pf-test-disclose")

    disclosures = job["result"]["disclosures"]
    assert "NOT modelled" in disclosures["currency"]
    assert "252" in disclosures["annualisation"]
    assert "one-way" in disclosures["turnover"]
    assert "99.8%" in disclosures["cash_buffer"]
    assert job["result"]["trading_days_per_year"] == 252


# ── dropped windows ──────────────────────────────────────────────────────────


def _walk_forward(**config: Any):
    histories = {
        t: synth_history(LONG, seed=70 + i, drift=0.0004) for i, t in enumerate(["AAA", "BBB"])
    }
    _, times = histories["AAA"]
    return run_portfolio_walk_forward(
        strategy_class=TrendFollowingTacticalStrategy,
        base_params=None,
        param_grid=[{"sma_period": 20}, {"sma_period": 50}],
        histories=histories,
        universe=["AAA", "BBB"],
        start_date=times[0].date(),
        end_date=times[-1].date(),
        benchmark_history=synth_history(LONG, seed=99),
        benchmark_ticker="SPY",
        config=WalkForwardConfig(initial_train_bars=300, test_bars=100, holdout_bars=120, **config),
    )


def test_windows_dropped_for_too_few_rebalances_are_counted_and_listed() -> None:
    result = _walk_forward(min_rebalances_per_window=999, min_valid_windows=1)
    payload = serialize_walk_forward(result)

    assert result.oos["windows_dropped"] == 3
    assert result.oos["dropped_window_ids"] == [0, 1, 2]
    assert result.oos["dropped_window_reasons"]["0"].endswith("< 999")
    assert payload["dropped_windows"]["count"] == 3
    assert payload["dropped_windows"]["ids"] == [0, 1, 2]
    assert payload["dropped_windows"]["min_rebalances_per_window"] == 999


def test_no_windows_dropped_when_every_window_is_valid() -> None:
    result = _walk_forward(min_rebalances_per_window=1, min_valid_windows=1)
    assert result.oos["windows_dropped"] == 0
    assert serialize_walk_forward(result)["dropped_windows"]["ids"] == []


def test_interpretation_warns_about_dropped_windows() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=0.8)
    walk_forward.oos.update(windows_dropped=2, dropped_window_ids=[1, 3])
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8), walk_forward)
    assert interpretation["validation"]["dropped_window_ids"] == [1, 3]
    assert any(
        "[1, 3]" in warning and "dropped" in warning for warning in interpretation["warnings"]
    )


# ── holdout reuse ────────────────────────────────────────────────────────────


def test_walk_forward_payload_carries_the_holdout_reuse_warning() -> None:
    payload = serialize_walk_forward(_walk_forward(min_rebalances_per_window=1))
    assert "spent by every call" in payload["holdout_reuse_warning"]
    assert "advisory" in payload["holdout_reuse_warning"]


@pytest.mark.asyncio
async def test_repeated_runs_on_the_same_data_are_flagged_and_run_id_is_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.backtest.data_fetcher.BacktestDataFetcher", LongFetcher)
    _, times = synth_history(LONG, seed=1)

    def request(grid: list[dict[str, Any]], run_id: str) -> PortfolioBacktestRequest:
        return PortfolioBacktestRequest(
            tickers=["AAA", "BBB"],
            strategy_type="trend_following_tactical",
            benchmark_ticker="SPY",
            from_date=times[0].date(),
            to_date=times[-1].date(),
            run_id=run_id,
            walk_forward={
                "param_grid": grid,
                "initial_train_bars": 300,
                "test_bars": 100,
                "holdout_bars": 120,
            },
        )

    ids = ["pf-reuse-1", "pf-reuse-2", "pf-reuse-3"]
    try:
        await route._run_portfolio_backtest_job(ids[0], request([{"sma_period": 20}], "first"))
        await route._run_portfolio_backtest_job(ids[1], request([{"sma_period": 50}], "second"))
        await route._run_portfolio_backtest_job(ids[2], request([{"sma_period": 50}], "third"))
        first, second, third = (route._portfolio_jobs[i]["holdout_reuse"] for i in ids)
    finally:
        for job_id in ids:
            route._portfolio_jobs.pop(job_id, None)

    assert first["run_id"] == "first"
    assert first["reused"] is False
    assert first["earlier_jobs_on_same_data"] == []
    assert second["data_fingerprint"] == first["data_fingerprint"]
    assert second["run_fingerprint"] != first["run_fingerprint"]  # different grid
    assert second["reused"] is True
    assert second["earlier_jobs_on_same_data"] == [ids[0]]
    assert second["earlier_jobs_identical_run"] == []
    assert third["run_fingerprint"] == second["run_fingerprint"]
    assert third["earlier_jobs_identical_run"] == [ids[1]]
    assert third["tracking"] == "in_process_memory_only"


class DatedFetcher(LongFetcher):
    """Honours the requested date range, as a real provider does."""

    async def fetch_bars(self, *, ticker: str, from_date: Any, to_date: Any, **kwargs: Any):
        bars, times = await super().fetch_bars(ticker=ticker, **kwargs)
        keep = [i for i, t in enumerate(times) if from_date <= t.date() <= to_date]
        return [bars[i] for i in keep], [times[i] for i in keep]


@pytest.mark.asyncio
async def test_reuse_is_flagged_when_universe_or_start_date_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fingerprint is on strategy family + holdout end date, so a new universe or start
    date cannot hide a second look at the same holdout; a different end date is a new holdout."""
    monkeypatch.setattr("app.backtest.data_fetcher.BacktestDataFetcher", DatedFetcher)
    _, times = synth_history(LONG, seed=1)

    def request(tickers: list[str], start: int, end: int, grid: list[dict[str, Any]]):
        return PortfolioBacktestRequest(
            tickers=tickers,
            strategy_type="trend_following_tactical",
            benchmark_ticker="SPY",
            from_date=times[start].date(),
            to_date=times[end].date(),
            walk_forward={
                "param_grid": grid,
                "initial_train_bars": 300,
                "test_bars": 100,
                "holdout_bars": 120,
            },
        )

    ids = ["pf-fam-1", "pf-fam-2", "pf-fam-3"]
    try:
        await route._run_portfolio_backtest_job(
            ids[0], request(["AAA", "BBB"], 0, LONG - 1, [{"sma_period": 20}])
        )
        await route._run_portfolio_backtest_job(
            ids[1], request(["AAA"], 15, LONG - 1, [{"sma_period": 50}, {"sma_period": 100}])
        )
        await route._run_portfolio_backtest_job(
            ids[2], request(["AAA", "BBB"], 0, LONG - 40, [{"sma_period": 20}])
        )
        jobs = [route._portfolio_jobs[i] for i in ids]
        first, second, third = (job["holdout_reuse"] for job in jobs)
        statuses = [job["status"] for job in jobs]
    finally:
        for job_id in ids:
            route._portfolio_jobs.pop(job_id, None)

    assert statuses == ["complete"] * 3
    assert second["data_fingerprint"] != first["data_fingerprint"]
    assert second["family_fingerprint"] == first["family_fingerprint"]
    assert second["reused"] is True
    assert second["earlier_jobs_same_strategy_and_holdout_end"] == [ids[0]]
    assert third["family_fingerprint"] != first["family_fingerprint"]
    assert third["reused"] is False
    assert second["enforcement"] == "advisory_only"


def test_reuse_warning_is_worded_as_advisory() -> None:
    from app.backtest.portfolio_walk_forward import HOLDOUT_REUSE_WARNING

    assert "advisory" in HOLDOUT_REUSE_WARNING
