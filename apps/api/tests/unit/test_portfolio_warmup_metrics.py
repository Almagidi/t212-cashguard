from __future__ import annotations

from decimal import Decimal

from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_metrics import returns_from_equity, sharpe_ratio
from tests.unit.portfolio_synth import ConstantWeightStrategy, synth_history

WARMUP = 200


def _run(histories, *, min_history: int, start_date=None, end_date=None):
    _, times = next(iter(histories.values()))
    return PortfolioBacktester(
        strategy=ConstantWeightStrategy(min_history_bars=min_history),
        universe=sorted(histories),
        initial_capital=Decimal("10000"),
        start_date=start_date or times[0].date(),
        end_date=end_date or times[-1].date(),
    ).run(histories)


def test_metrics_exclude_warmup_cash_days_and_match_post_warmup_slice() -> None:
    histories = {
        "AAA": synth_history(420, seed=1),
        "BBB": synth_history(420, seed=2, drift=0.0002),
    }
    full = _run(histories, min_history=WARMUP)

    assert full.warmup_bars_excluded == WARMUP
    assert full.metrics_start_date == full.equity_curve[WARMUP].date

    sliced_histories = {
        ticker: (bars[WARMUP:], times[WARMUP:]) for ticker, (bars, times) in histories.items()
    }
    sliced = _run(
        sliced_histories,
        min_history=0,
        start_date=full.metrics_start_date,
        end_date=full.end_date,
    )

    assert full.sharpe_ratio is not None
    assert full.sharpe_ratio == sliced.sharpe_ratio
    assert full.sortino_ratio == sliced.sortino_ratio
    assert full.max_drawdown_pct == sliced.max_drawdown_pct
    assert full.avg_exposure_pct == sliced.avg_exposure_pct
    assert full.annualised_return_pct == sliced.annualised_return_pct
    assert full.turnover_pct == sliced.turnover_pct
    assert full.benchmark_return_pct == sliced.benchmark_return_pct
    assert full.total_return_pct == sliced.total_return_pct


def test_warmup_included_sharpe_would_have_been_diluted() -> None:
    histories = {"AAA": synth_history(420, seed=3)}
    full = _run(histories, min_history=WARMUP)
    all_days = returns_from_equity([point.equity for point in full.equity_curve], Decimal("10000"))
    diluted = sharpe_ratio(all_days)

    assert diluted is not None
    assert full.sharpe_ratio is not None
    assert Decimal(str(round(diluted, 3))) != full.sharpe_ratio
    assert full.avg_exposure_pct > Decimal("90")  # not averaged with 200 days of 0% exposure
