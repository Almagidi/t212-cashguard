"""The caller-chosen broad index must be a believable index before it can anchor a verdict."""

from __future__ import annotations

from decimal import Decimal

from app.api.v1.routes import backtest as route
from app.backtest.gate_floors import (
    MIN_BENCHMARK_ANNUAL_VOLATILITY,
    MIN_BENCHMARK_CORRELATION,
    benchmark_sanity,
)
from app.strategies.indicators import Bar
from tests.unit.portfolio_synth import synth_history
from tests.unit.test_portfolio_backtest_route import _fake_result, _fake_wf
from tests.unit.test_portfolio_walk_forward import LENGTH, TICKERS, _histories, _run


def _average_index(histories) -> tuple[list[Bar], list]:
    """An index built from the mean daily return of the universe: a high-correlation proxy."""
    series = list(histories.values())
    times = series[0][1]
    price, bars = 100.0, []
    for i in range(len(times)):
        closes = [float(bars_[i].close) for bars_, _ in series]
        opens = [float(bars_[i].open) for bars_, _ in series]
        ret = sum(c / o - 1.0 for c, o in zip(closes, opens, strict=True)) / len(series)
        new = max(1.0, price * (1.0 + ret))
        bars.append(
            Bar(
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{max(price, new) * 1.001:.4f}"),
                low=Decimal(f"{min(price, new) * 0.999:.4f}"),
                close=Decimal(f"{new:.4f}"),
                volume=Decimal("100000"),
            )
        )
        price = new
    return bars, list(times)


def _sanity_reasons(result) -> str:
    return " | ".join(result.gate_reasons)


def test_valid_index_passes_and_statistics_are_reported() -> None:
    histories = _histories()
    result = _run(histories, benchmark_history=_average_index(histories), benchmark_ticker="SPY")
    sanity = result.benchmark_sanity
    assert sanity is not None
    assert sanity["ticker"] == "SPY"
    assert sanity["in_universe"] is False
    assert sanity["annualised_volatility"] >= MIN_BENCHMARK_ANNUAL_VOLATILITY
    assert sanity["correlation_with_universe"] >= MIN_BENCHMARK_CORRELATION
    assert sanity["passes"] is True
    assert not any("benchmark SPY" in reason for reason in result.gate_reasons)


def test_cash_like_ticker_is_refused() -> None:
    histories = _histories()
    cash = synth_history(LENGTH, seed=5, drift=0.00012, vol=0.0001)
    result = _run(histories, benchmark_history=cash, benchmark_ticker="BIL")
    assert result.benchmark_sanity["passes"] is False
    assert result.benchmark_sanity["annualised_volatility"] < MIN_BENCHMARK_ANNUAL_VOLATILITY
    assert "annualised volatility" in _sanity_reasons(result)
    assert result.passes_gate is False


def test_in_universe_ticker_is_refused() -> None:
    histories = _histories()
    result = _run(histories, benchmark_history=histories["AAA"], benchmark_ticker="aaa")
    assert result.benchmark_sanity["in_universe"] is True
    assert "member of the strategy universe" in _sanity_reasons(result)
    assert result.passes_gate is False


def test_uncorrelated_ticker_is_refused() -> None:
    histories = _histories()
    result = _run(
        histories,
        benchmark_history=synth_history(LENGTH, seed=991, drift=0.0004, vol=0.01),
        benchmark_ticker="XYZ",
    )
    sanity = result.benchmark_sanity
    assert sanity["annualised_volatility"] >= MIN_BENCHMARK_ANNUAL_VOLATILITY
    assert sanity["correlation_with_universe"] < MIN_BENCHMARK_CORRELATION
    assert "correlation" in _sanity_reasons(result)
    assert result.passes_gate is False


def test_sanity_is_serialised_with_the_walk_forward() -> None:
    from app.backtest.portfolio_walk_forward import serialize_walk_forward

    histories = _histories()
    result = _run(histories, benchmark_history=_average_index(histories), benchmark_ticker="SPY")
    assert serialize_walk_forward(result)["benchmark_sanity"]["ticker"] == "SPY"


def test_pure_function_edge_cases() -> None:
    flat = benchmark_sanity(
        benchmark_ticker="x",
        universe=TICKERS,
        universe_returns=[0.01, -0.01, 0.02],
        benchmark_returns=[0.0, 0.0, 0.0],
    )
    assert flat["passes"] is False
    assert flat["correlation_with_universe"] is None


def test_verdict_refuses_and_states_the_benchmark_when_sanity_fails() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=0.8)
    walk_forward.benchmark_sanity = {
        "ticker": "BIL",
        "in_universe": False,
        "annualised_volatility": 0.004,
        "correlation_with_universe": 0.02,
        "passes": False,
        "reasons": ["benchmark BIL annualised volatility 0.40% is below the minimum 8%"],
    }
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8), walk_forward)
    assert interpretation["verdict"] == "unvalidated"
    assert "BIL" in interpretation["validation"]["refusal_reason"]
    assert any("Benchmark sanity" in w and "BIL" in w for w in interpretation["warnings"])


def test_verdict_text_states_benchmark_when_sanity_passes() -> None:
    interpretation = route._interpret_portfolio_results(
        _fake_result(sharpe=0.8), _fake_wf(passes=True, sharpe=0.8)
    )
    assert interpretation["verdict"] == "promising"
    assert interpretation["validation"]["benchmark_sanity"]["ticker"] == "SPY"
    assert any("broad-index benchmark SPY" in w for w in interpretation["warnings"])


def test_gate_pass_without_sanity_record_is_refused() -> None:
    walk_forward = _fake_wf(passes=True, sharpe=0.8)
    walk_forward.benchmark_sanity = None
    interpretation = route._interpret_portfolio_results(_fake_result(sharpe=0.8), walk_forward)
    assert interpretation["verdict"] == "unvalidated"


def test_floors_are_exposed() -> None:
    from app.backtest.gate_floors import floors_as_dict

    floors = floors_as_dict()
    assert floors["min_benchmark_annualised_volatility"] == MIN_BENCHMARK_ANNUAL_VOLATILITY
    assert floors["min_benchmark_correlation"] == MIN_BENCHMARK_CORRELATION
