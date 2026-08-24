from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.backtest.engine import (
    Backtester,
    BacktestOrder,
    BacktestResult,
    BacktestTrade,
    ExecutionSimulator,
    WalkForwardValidator,
    _compute_metrics,
    generate_strategy_signal,
    monte_carlo_trade_sequence,
    summarise_walk_forward_results,
)
from app.execution.paper_policy import evaluate_paper_fill
from app.strategies.indicators import Bar


def make_bar(
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "100000",
) -> Bar:
    return Bar(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def make_trade(pnl: str) -> BacktestTrade:
    now = datetime.now(UTC)
    pnl_decimal = Decimal(pnl)
    return BacktestTrade(
        id="t1",
        ticker="AAPL",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("1"),
        side="buy",
        pnl=pnl_decimal,
        pnl_pct=Decimal("1.00"),
        entry_bar_idx=0,
        exit_bar_idx=1,
        entry_time=now,
        exit_time=now,
        exit_reason="signal",
        slippage_cost=Decimal("0.10"),
        holding_bars=1,
        mfe=max(pnl_decimal, Decimal("0")),
        mae=abs(min(pnl_decimal, Decimal("0"))),
    )


class PrevCloseAwareStrategy:
    def generate_signal(
        self,
        *,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> Decimal | None:
        return prev_close


class SessionOpenAwareStrategy:
    def generate_signal(
        self,
        *,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        session_open: Decimal | None,
    ) -> Decimal | None:
        return session_open


class CaptureSessionStrategy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Decimal | None | int]] = []

    def generate_signal(
        self,
        *,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> None:
        self.calls.append(
            {
                "bars": len(bars),
                "session_open": bars[0].open if bars else None,
                "last_close": bars[-1].close if bars else None,
                "prev_close": prev_close,
            }
        )
        return None


class OneShotStrategy:
    def __init__(self, *, side: str = "buy", quantity: Decimal = Decimal("1")) -> None:
        self.side = side
        self.quantity = quantity
        self.called = False

    def generate_signal(self, **_: object) -> SimpleNamespace | None:
        if self.called:
            return None
        self.called = True
        return SimpleNamespace(
            side=self.side,
            suggested_quantity=self.quantity,
            entry_price=Decimal("100"),
            stop_price=Decimal("99"),
            take_profit_price=Decimal("110"),
        )


class TestBacktestHelpers:
    def test_execution_simulator_matches_standard_paper_policy(self):
        bar = make_bar("100", "101", "99", "100")
        order = BacktestOrder(
            id="order-1",
            ticker="AAPL",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
            limit_price=None,
            submitted_bar_idx=0,
        )

        fill = ExecutionSimulator().simulate_fill(order, bar, "buy")
        expected = evaluate_paper_fill(
            side="buy",
            quantity=Decimal("2"),
            quote_price=Decimal("100"),
            profile="standard",
        )

        assert fill.price == expected.fill_price
        assert fill.quantity == expected.filled_quantity
        assert fill.fee == expected.fee_amount
        assert fill.slippage_cost == Decimal("0.20")

    def test_limit_order_requires_price_to_cross(self):
        executor = ExecutionSimulator()
        order = BacktestOrder(
            id="limit-1",
            ticker="AAPL",
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("99"),
            submitted_bar_idx=0,
        )

        assert not executor.can_fill_limit(order, make_bar("100", "101", "99.01", "100"), "buy")
        assert executor.can_fill_limit(order, make_bar("100", "101", "99", "100"), "buy")

    def test_flat_round_trip_uses_paper_costs_and_net_trade_pnl(self):
        bars = [
            make_bar("100", "100.5", "99.5", "100"),
            make_bar("100", "100.5", "99.5", "100"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
        ]
        result = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
        ).run(bars, bar_times)
        buy = evaluate_paper_fill(
            side="buy",
            quantity=Decimal("1"),
            quote_price=Decimal("100"),
            profile="standard",
        )
        sell = evaluate_paper_fill(
            side="sell",
            quantity=Decimal("1"),
            quote_price=Decimal("100"),
            profile="standard",
        )
        assert buy.fill_price is not None
        assert sell.fill_price is not None
        expected_final = (
            Decimal("10000") - buy.fill_price - buy.fee_amount + sell.fill_price - sell.fee_amount
        )

        assert result.final_capital == expected_final
        assert result.total_commission_cost == buy.fee_amount + sell.fee_amount
        assert result.total_slippage_cost == Decimal("0.20")
        assert result.trades[0].pnl == result.net_pnl
        assert result.trades[0].commission_cost == buy.fee_amount + sell.fee_amount
        assert result.equity_curve[-1]["cash"] == float(expected_final)
        assert result.equity_curve[-1]["position_value"] == 0.0

        from app.api.v1.routes.backtest import _serialize_backtest_trade

        serialized_trade = _serialize_backtest_trade(result.trades[0])
        assert serialized_trade["commission_cost"] == float(buy.fee_amount + sell.fee_amount)

    def test_entry_quantity_is_capped_by_position_risk_and_cash(self):
        bars = [
            make_bar("100", "101", "99", "100"),
            make_bar("100", "101", "99.5", "100"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
        ]
        result = Backtester(
            strategy=OneShotStrategy(quantity=Decimal("100000")),
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.05"),
            max_position_pct=Decimal("10"),
        ).run(bars, bar_times)

        trade = result.trades[0]
        assert trade.entry_price * trade.quantity <= Decimal("1000")
        assert (trade.entry_price - Decimal("99")) * trade.quantity <= Decimal("5")
        assert min(Decimal(str(point["cash"])) for point in result.equity_curve) >= 0

    def test_final_liquidation_uses_final_close_not_consumed_open(self):
        bars = [
            make_bar("100", "100.5", "99.5", "100"),
            make_bar("100", "105", "99.5", "105"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
        ]
        result = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
        ).run(bars, bar_times)
        expected = evaluate_paper_fill(
            side="sell",
            quantity=Decimal("1"),
            quote_price=Decimal("105"),
            profile="standard",
        )

        assert result.trades[-1].exit_price == expected.fill_price
        assert result.trades[-1].exit_reason == "backtest_end"

    def test_higher_non_negative_cost_cannot_improve_return(self):
        bars = [
            make_bar("100", "100.5", "99.5", "100"),
            make_bar("100", "100.5", "99.5", "100"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
        ]
        free = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            commission_per_trade=Decimal("0"),
        ).run(bars, bar_times)
        costly = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            commission_per_trade=Decimal("1"),
        ).run(bars, bar_times)

        assert costly.final_capital < free.final_capital
        assert costly.trades[0].pnl < free.trades[0].pnl

    def test_long_only_backtester_rejects_sell_entry_signal(self):
        bars = [
            make_bar("100", "101", "99", "100"),
            make_bar("100", "101", "99", "100"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
        ]

        with pytest.raises(ValueError, match="long-only"):
            Backtester(strategy=OneShotStrategy(side="sell"), ticker="AAPL").run(bars, bar_times)

    def test_generate_strategy_signal_passes_prev_close_when_supported(self):
        result = generate_strategy_signal(
            PrevCloseAwareStrategy(),
            ticker="AAPL",
            bars=[make_bar("100", "101", "99", "100.5")],
            account_value=Decimal("10000"),
            available_cash=Decimal("10000"),
            current_time_utc="15:00",
            prev_close=Decimal("99.5"),
        )

        assert result == Decimal("99.5")

    def test_generate_strategy_signal_passes_session_open_when_supported(self):
        result = generate_strategy_signal(
            SessionOpenAwareStrategy(),
            ticker="AAPL",
            bars=[
                make_bar("100", "101", "99", "100.5"),
                make_bar("100.5", "102", "100", "101.5"),
            ],
            account_value=Decimal("10000"),
            available_cash=Decimal("10000"),
            current_time_utc="15:05",
            prev_close=Decimal("99.5"),
        )

        assert result == Decimal("100")

    def test_summarise_walk_forward_results_marks_sufficient_runs_research_only(self):
        summary = summarise_walk_forward_results(
            [
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 4,
                    "eligible_candidates": 2,
                    "oos_return_pct": 5.2,
                    "oos_sharpe": 1.1,
                    "oos_max_dd": 8.0,
                    "oos_trades": 10,
                    "oos_positions": 10,
                },
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 4,
                    "eligible_candidates": 2,
                    "oos_return_pct": 3.4,
                    "oos_sharpe": 0.9,
                    "oos_max_dd": 10.5,
                    "oos_trades": 12,
                    "oos_positions": 12,
                },
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 4,
                    "eligible_candidates": 2,
                    "oos_return_pct": 4.1,
                    "oos_sharpe": 1.3,
                    "oos_max_dd": 7.2,
                    "oos_trades": 11,
                    "oos_positions": 11,
                },
            ]
        )

        assert summary is not None
        assert summary["windows"] == 3
        assert summary["selected_windows"] == 3
        assert summary["total_oos_positions"] == 33
        assert summary["parameter_combinations_tested"] == 4
        assert summary["candidate_evaluations"] == 12
        assert summary["verdict"] == "research_only"
        assert summary["performance_assessment"] == "favourable"
        assert summary["robustness_score"] == 100.0

    @pytest.mark.parametrize(
        "results",
        [
            [
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 2,
                    "eligible_candidates": 1,
                    "oos_return_pct": 5.0,
                    "oos_sharpe": 2.0,
                    "oos_max_dd": 1.0,
                    "oos_trades": 30,
                    "oos_positions": 30,
                }
            ],
            [
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 2,
                    "eligible_candidates": 1,
                    "oos_return_pct": 5.0,
                    "oos_sharpe": 2.0,
                    "oos_max_dd": 1.0,
                    "oos_trades": 15,
                    "oos_positions": 15,
                },
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 2,
                    "eligible_candidates": 1,
                    "oos_return_pct": 5.0,
                    "oos_sharpe": 2.0,
                    "oos_max_dd": 1.0,
                    "oos_trades": 15,
                    "oos_positions": 15,
                },
            ],
        ],
    )
    def test_summarise_walk_forward_results_rejects_one_or_two_favourable_windows(
        self, results: list[dict[str, object]]
    ):
        summary = summarise_walk_forward_results(results)

        assert summary is not None
        assert summary["verdict"] == "insufficient_evidence"
        assert "3 selected held-out windows" in summary["message"]

    def test_summarise_walk_forward_results_rejects_low_position_windows(self):
        summary = summarise_walk_forward_results(
            [
                {
                    "selection_status": "selected",
                    "parameter_combinations_tested": 2,
                    "eligible_candidates": 1,
                    "oos_return_pct": 5.0,
                    "oos_sharpe": 2.0,
                    "oos_max_dd": 1.0,
                    "oos_trades": positions * 2,
                    "oos_positions": positions,
                }
                for positions in (10, 10, 9)
            ]
        )

        assert summary is not None
        assert summary["verdict"] == "insufficient_evidence"
        assert "10 independent positions in every held-out window" in summary["message"]

    def test_equity_path_ratios_do_not_change_when_trades_are_split(self):
        now = datetime.now(UTC)

        def result_with(trades: list[BacktestTrade]) -> BacktestResult:
            return BacktestResult(
                strategy_name="test",
                ticker="AAPL",
                start_date=now.date(),
                end_date=now.date(),
                initial_capital=Decimal("10000"),
                final_capital=Decimal("10020"),
                trades=trades,
                equity_curve=[
                    {"time": now.isoformat(), "equity": 10000.0},
                    {"time": (now + timedelta(minutes=5)).isoformat(), "equity": 10010.0},
                    {"time": (now + timedelta(minutes=10)).isoformat(), "equity": 10005.0},
                    {"time": (now + timedelta(minutes=15)).isoformat(), "equity": 10020.0},
                ],
            )

        unsplit = _compute_metrics(result_with([make_trade("20"), make_trade("-5")]))
        split = _compute_metrics(
            result_with(
                [make_trade("10"), make_trade("10"), make_trade("-2.5"), make_trade("-2.5")]
            )
        )

        assert unsplit.sharpe_ratio is not None
        assert unsplit.sortino_ratio is not None
        assert unsplit.sharpe_ratio == split.sharpe_ratio
        assert unsplit.sortino_ratio == split.sortino_ratio
        assert unsplit.completed_positions == split.completed_positions == 1

    def test_walk_forward_selection_cannot_see_held_out_test_prices(self, monkeypatch):
        calls: list[tuple[int, str]] = []

        class FakeStrategy:
            def __init__(self, params: dict[str, object]) -> None:
                self.params = params

        class FakeBacktester:
            def __init__(self, strategy, *_args, **_kwargs) -> None:
                self.strategy = strategy

            def run(self, bars, _times):
                marker = int(bars[0].close)
                name = str(self.strategy.params["name"])
                calls.append((marker, name))
                validation_sharpes = {
                    "a": Decimal("-10001"),
                    "b": Decimal("-10000"),
                    "z": Decimal("0"),
                }
                sharpe = validation_sharpes[name] if marker == 3 else Decimal("-1")
                return SimpleNamespace(
                    total_trades=10,
                    completed_positions=10,
                    total_return_pct=Decimal(marker),
                    sharpe_ratio=sharpe,
                    max_drawdown_pct=Decimal("1"),
                    win_rate=Decimal("0.5"),
                    profit_factor=Decimal("1.2"),
                )

        monkeypatch.setattr("app.backtest.engine.Backtester", FakeBacktester)
        validator = WalkForwardValidator(
            strategy_class=FakeStrategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            in_sample_bars=2,
            out_sample_bars=1,
            step_bars=1,
        )
        start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        times = [start.replace(minute=start.minute + index * 5) for index in range(4)]
        base_bars = [
            make_bar(str(value), str(value), str(value), str(value)) for value in (1, 2, 3, 4)
        ]
        mutated_bars = [*base_bars[:3], make_bar("400", "400", "400", "400")]
        grid = [{"name": "a"}, {"name": "b"}, {"name": "z"}]

        baseline = validator.run(base_bars, times, grid)
        mutated = validator.run(mutated_bars, times, grid)
        deeply_negative = validator.run(base_bars, times, grid[:2])

        assert baseline[0]["best_params"] == {"name": "z"}
        assert mutated[0]["best_params"] == {"name": "z"}
        assert deeply_negative[0]["best_params"] == {"name": "b"}
        assert baseline[0]["oos_return_pct"] == 4.0
        assert mutated[0]["oos_return_pct"] == 400.0
        assert calls[:7] == [
            (1, "a"),
            (3, "a"),
            (1, "b"),
            (3, "b"),
            (1, "z"),
            (3, "z"),
            (4, "z"),
        ]

    def test_walk_forward_has_explicit_no_selection_when_candidates_are_ineligible(
        self, monkeypatch
    ):
        class FakeStrategy:
            def __init__(self, params: dict[str, object]) -> None:
                self.params = params

        class FakeBacktester:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def run(self, _bars, _times):
                return SimpleNamespace(total_trades=0, completed_positions=0, sharpe_ratio=None)

        monkeypatch.setattr("app.backtest.engine.Backtester", FakeBacktester)
        validator = WalkForwardValidator(
            strategy_class=FakeStrategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            in_sample_bars=2,
            out_sample_bars=1,
            step_bars=1,
        )
        start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        times = [start.replace(minute=start.minute + index * 5) for index in range(4)]
        bars = [make_bar(str(value), str(value), str(value), str(value)) for value in (1, 2, 3, 4)]

        result = validator.run(bars, times, [{"name": "a"}, {"name": "b"}])

        assert result[0]["selection_status"] == "no_eligible_candidate"
        assert result[0]["best_params"] is None
        assert result[0]["parameter_combinations_tested"] == 2
        assert result[0]["eligible_candidates"] == 0
        assert result[0]["oos_return_pct"] is None
        summary = summarise_walk_forward_results(result)
        assert summary is not None
        assert summary["selected_windows"] == 0
        assert summary["verdict"] == "insufficient_evidence"
        assert summary["performance_assessment"] == "unassessed"

    def test_walk_forward_blocks_remain_disjoint_when_requested_step_is_small(self, monkeypatch):
        calls: list[int] = []

        class FakeStrategy:
            def __init__(self, params: dict[str, object]) -> None:
                self.params = params

        class FakeBacktester:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def run(self, bars, _times):
                calls.append(int(bars[0].close))
                return SimpleNamespace(
                    total_trades=10,
                    completed_positions=10,
                    total_return_pct=Decimal("1"),
                    sharpe_ratio=Decimal("1"),
                    max_drawdown_pct=Decimal("1"),
                    win_rate=Decimal("0.5"),
                    profit_factor=Decimal("1.2"),
                )

        monkeypatch.setattr("app.backtest.engine.Backtester", FakeBacktester)
        validator = WalkForwardValidator(
            strategy_class=FakeStrategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            in_sample_bars=2,
            out_sample_bars=1,
            step_bars=1,
        )
        start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        times = [start + timedelta(minutes=index * 5) for index in range(8)]
        bars = [make_bar(str(value), str(value), str(value), str(value)) for value in range(1, 9)]

        results = validator.run(bars, times, [{"name": "a"}])

        assert len(results) == 2
        assert calls == [1, 3, 4, 5, 7, 8]

    def test_monte_carlo_trade_sequence_returns_probability_metrics(self):
        summary = monte_carlo_trade_sequence(
            [
                make_trade("120"),
                make_trade("-60"),
                make_trade("80"),
                make_trade("-40"),
                make_trade("50"),
                make_trade("-30"),
            ],
            Decimal("10000"),
            iterations=100,
        )

        assert summary["iterations"] == 100
        assert 0 <= summary["probability_drawdown_gt_10pct"] <= 100
        assert 0 <= summary["probability_drawdown_gt_20pct"] <= 100
        assert summary["p95_max_drawdown_pct"] >= summary["median_max_drawdown_pct"]

    def test_backtester_resets_context_each_session_and_tracks_prev_close(self):
        strategy = CaptureSessionStrategy()
        bars = [
            make_bar("100", "101", "99", "100.5"),
            make_bar("100.5", "101.5", "100", "101"),
            make_bar("101", "103", "100.5", "102"),
            make_bar("110", "111", "109.5", "110.5"),
            make_bar("110.5", "112", "110", "111"),
        ]
        bar_times = [
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
            datetime(2026, 1, 5, 14, 40, tzinfo=UTC),
            datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 6, 14, 35, tzinfo=UTC),
        ]

        result = Backtester(strategy=strategy, ticker="AAPL", initial_capital=Decimal("10000")).run(
            bars, bar_times
        )

        assert result.total_trades == 0
        second_session_first_call = next(
            call for call in strategy.calls if call["session_open"] == Decimal("110")
        )
        assert second_session_first_call["bars"] == 1
        assert second_session_first_call["prev_close"] == Decimal("102")
