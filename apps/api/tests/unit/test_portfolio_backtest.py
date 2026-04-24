from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_strategies import (
    CrossSectionalMomentumStrategy,
    LowVolatilityTiltStrategy,
    TrendFollowingTacticalStrategy,
)
from app.strategies.indicators import Bar


def make_daily_bar(open_: str, close: str, high: str | None = None, low: str | None = None, volume: str = "100000") -> Bar:
    return Bar(
        open=Decimal(open_),
        high=Decimal(high or close),
        low=Decimal(low or open_),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def make_history(
    *,
    length: int,
    start_open: Decimal,
    daily_step: Decimal,
    intraday_spread: Decimal = Decimal("0.5"),
) -> tuple[list[Bar], list[datetime]]:
    bars: list[Bar] = []
    bar_times: list[datetime] = []
    current = start_open
    start = datetime(2021, 1, 1, tzinfo=UTC)

    for index in range(length):
        open_price = current
        close_price = current + daily_step
        bars.append(
            make_daily_bar(
                f"{open_price}",
                f"{close_price}",
                high=f"{max(open_price, close_price) + intraday_spread}",
                low=f"{min(open_price, close_price) - intraday_spread}",
            )
        )
        bar_times.append(start + timedelta(days=index))
        current = close_price

    return bars, bar_times


def make_volatile_history(*, start_open: Decimal, steps: list[Decimal]) -> tuple[list[Bar], list[datetime]]:
    bars: list[Bar] = []
    bar_times: list[datetime] = []
    current = start_open
    start = datetime(2021, 1, 1, tzinfo=UTC)

    for index, step in enumerate(steps):
        open_price = current
        close_price = current + step
        high_price = max(open_price, close_price) + abs(step)
        low_price = min(open_price, close_price) - abs(step)
        bars.append(make_daily_bar(f"{open_price}", f"{close_price}", high=f"{high_price}", low=f"{low_price}"))
        bar_times.append(start + timedelta(days=index))
        current = close_price

    return bars, bar_times


class TestPortfolioStrategies:
    def test_cross_sectional_momentum_selects_recent_winners(self):
        winner_bars, _ = make_history(length=220, start_open=Decimal("100"), daily_step=Decimal("1.0"))
        loser_bars, _ = make_history(length=220, start_open=Decimal("100"), daily_step=Decimal("-0.2"))

        strategy = CrossSectionalMomentumStrategy({"top_n": 1})
        weights = strategy.target_weights(
            {"WIN": winner_bars, "LOSE": loser_bars},
            as_of_index=200,
        )

        assert weights == {"WIN": Decimal("1.0000")}

    def test_low_volatility_tilt_prefers_quieter_series(self):
        calm_bars, _ = make_history(length=120, start_open=Decimal("100"), daily_step=Decimal("0.15"), intraday_spread=Decimal("0.2"))
        wild_steps = [Decimal("1.5") if index % 2 == 0 else Decimal("-1.2") for index in range(120)]
        wild_bars, _ = make_volatile_history(start_open=Decimal("100"), steps=wild_steps)

        strategy = LowVolatilityTiltStrategy({"selection_count": 1, "lookback_bars": 63})
        weights = strategy.target_weights(
            {"CALM": calm_bars, "WILD": wild_bars},
            as_of_index=100,
        )

        assert weights == {"CALM": Decimal("1.0000")}

    def test_trend_following_moves_to_assets_above_sma(self):
        uptrend_bars, _ = make_history(length=260, start_open=Decimal("100"), daily_step=Decimal("0.4"))
        downtrend_bars, _ = make_history(length=260, start_open=Decimal("180"), daily_step=Decimal("-0.25"))

        strategy = TrendFollowingTacticalStrategy({"sma_period": 200})
        weights = strategy.target_weights(
            {"UP": uptrend_bars, "DOWN": downtrend_bars},
            as_of_index=240,
        )

        assert weights == {"UP": Decimal("1.0000")}


class TestPortfolioBacktester:
    def test_portfolio_backtester_runs_and_records_rebalances(self):
        from app.backtest.portfolio_strategies import EqualWeightRebalanceStrategy

        aaa_history = make_history(length=320, start_open=Decimal("100"), daily_step=Decimal("0.35"))
        bbb_history = make_history(length=320, start_open=Decimal("90"), daily_step=Decimal("0.20"))
        ccc_history = make_history(length=320, start_open=Decimal("110"), daily_step=Decimal("-0.05"))

        backtester = PortfolioBacktester(
            strategy=EqualWeightRebalanceStrategy({"rebalance_frequency": "monthly"}),
            universe=["AAA", "BBB", "CCC"],
            initial_capital=Decimal("10000"),
            start_date=aaa_history[1][0].date(),
            end_date=aaa_history[1][-1].date(),
        )

        result = backtester.run(
            {
                "AAA": aaa_history,
                "BBB": bbb_history,
                "CCC": ccc_history,
            }
        )

        assert result.final_capital > result.initial_capital
        assert result.rebalance_count >= 3
        assert result.total_trades > 0
        assert result.turnover_pct > 0
        assert result.equity_curve
