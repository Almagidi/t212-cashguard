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
from app.market_data.exchange_calendar import calendar_for_venue
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


def add_previous_session_warmup(
    bars: list[Bar],
    bar_times: list[datetime],
) -> tuple[list[Bar], list[datetime]]:
    return (
        [make_bar("100", "101", "99", "100"), *bars],
        [datetime(2026, 1, 2, 20, 55, tzinfo=UTC), *bar_times],
    )


def terminal_session_series(values: list[int]) -> tuple[list[Bar], list[datetime]]:
    calendar = calendar_for_venue("XNYS")
    sessions = calendar.expected_sessions(
        datetime(2026, 1, 2, tzinfo=UTC).date(),
        datetime(2026, 2, 27, tzinfo=UTC).date(),
    )[: len(values)]
    return (
        [
            make_bar(str(value), str(value), str(value), str(value))
            for value in values
            for _ in range(2)
        ],
        [
            timestamp
            for session in sessions
            for timestamp in (
                calendar.session_open(session),
                calendar.session_close(session) - timedelta(minutes=5),
            )
        ],
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
        self.calls: list[dict[str, object]] = []

    def generate_signal(
        self,
        *,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
        bar_times: list[datetime] | None = None,
        history_bars: list[Bar] | None = None,
        history_bar_times: list[datetime] | None = None,
    ) -> None:
        self.calls.append(
            {
                "bars": len(bars),
                "session_open": bars[0].open if bars else None,
                "last_close": bars[-1].close if bars else None,
                "prev_close": prev_close,
                "current_time_utc": current_time_utc,
                "bar_times": tuple(bar_times or []),
                "history_bars": tuple(history_bars or []),
                "history_bar_times": tuple(history_bar_times or []),
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
        bars, bar_times = add_previous_session_warmup(bars, bar_times)
        result = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
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
        bars, bar_times = add_previous_session_warmup(bars, bar_times)
        result = Backtester(
            strategy=OneShotStrategy(quantity=Decimal("100000")),
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.05"),
            max_position_pct=Decimal("10"),
            start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
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
        bars, bar_times = add_previous_session_warmup(bars, bar_times)
        result = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
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
        bars, bar_times = add_previous_session_warmup(bars, bar_times)
        free = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            commission_per_trade=Decimal("0"),
            start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
        ).run(bars, bar_times)
        costly = Backtester(
            strategy=OneShotStrategy(),
            ticker="AAPL",
            risk_per_trade_pct=Decimal("100"),
            max_position_pct=Decimal("100"),
            commission_per_trade=Decimal("1"),
            start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
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
        bars, bar_times = add_previous_session_warmup(bars, bar_times)

        with pytest.raises(ValueError, match="long-only"):
            Backtester(
                strategy=OneShotStrategy(side="sell"),
                ticker="AAPL",
                start_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
            ).run(bars, bar_times)

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
            def __init__(self, strategy, *_args, **kwargs) -> None:
                self.strategy = strategy
                self.start_date = kwargs["start_date"]

            def run(self, bars, times):
                marker = next(
                    int(bar.close)
                    for bar, timestamp in zip(bars, times, strict=True)
                    if timestamp.date() >= self.start_date
                )
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
            in_sample_bars=4,
            out_sample_bars=2,
            step_bars=1,
        )
        base_bars, times = terminal_session_series([100, 1, 2, 3, 4])
        mutated_bars = [
            *base_bars[:8],
            make_bar("400", "400", "400", "400"),
            make_bar("400", "400", "400", "400"),
        ]
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
            in_sample_bars=4,
            out_sample_bars=2,
            step_bars=1,
        )
        bars, times = terminal_session_series([100, 1, 2, 3, 4])

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
            def __init__(self, *_args, **kwargs) -> None:
                self.start_date = kwargs["start_date"]

            def run(self, bars, times):
                calls.append(
                    next(
                        int(bar.close)
                        for bar, timestamp in zip(bars, times, strict=True)
                        if timestamp.date() >= self.start_date
                    )
                )
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
            in_sample_bars=4,
            out_sample_bars=2,
            step_bars=1,
        )
        bars, times = terminal_session_series([100, *range(1, 9)])

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
            datetime(2026, 1, 5, 20, 55, tzinfo=UTC),
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

    def test_backtester_retains_prior_session_as_start_date_warmup(self):
        strategy = CaptureSessionStrategy()
        bars = [
            make_bar("100", "101", "99", "100"),
            make_bar("110", "111", "109", "110"),
            make_bar("110", "112", "109", "111"),
        ]
        bar_times = [
            datetime(2025, 1, 3, 20, 55, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 35, tzinfo=UTC),
        ]

        Backtester(
            strategy=strategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            start_date=datetime(2025, 1, 6, tzinfo=UTC).date(),
        ).run(bars, bar_times)

        assert strategy.calls[0]["session_open"] == Decimal("110")
        assert strategy.calls[0]["prev_close"] == Decimal("100")
        assert len(strategy.calls[0]["history_bars"]) == 2

    def test_backtester_excludes_first_session_when_warmup_is_missing(self):
        strategy = CaptureSessionStrategy()

        result = Backtester(strategy=strategy, ticker="AAPL").run(
            [
                make_bar("110", "111", "109", "110"),
                make_bar("110", "112", "109", "111"),
            ],
            [
                datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
                datetime(2025, 1, 6, 14, 35, tzinfo=UTC),
            ],
        )

        assert strategy.calls == []
        assert result.equity_curve == []

    def test_backtester_rejects_incomplete_prior_session_as_warmup(self):
        strategy = CaptureSessionStrategy()

        Backtester(
            strategy=strategy,
            ticker="AAPL",
            start_date=datetime(2025, 1, 6, tzinfo=UTC).date(),
        ).run(
            [
                make_bar("100", "101", "99", "100"),
                make_bar("110", "111", "109", "110"),
            ],
            [
                datetime(2025, 1, 3, 15, 0, tzinfo=UTC),
                datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
            ],
        )

        assert strategy.calls == []

    def test_backtester_excludes_session_when_opening_bar_is_missing(self):
        strategy = CaptureSessionStrategy()

        result = Backtester(
            strategy=strategy,
            ticker="AAPL",
            start_date=datetime(2025, 1, 6, tzinfo=UTC).date(),
        ).run(
            [
                make_bar("100", "101", "99", "100"),
                make_bar("110", "111", "109", "110"),
                make_bar("110", "112", "109", "111"),
            ],
            [
                datetime(2025, 1, 3, 20, 55, tzinfo=UTC),
                datetime(2025, 1, 6, 15, 0, tzinfo=UTC),
                datetime(2025, 1, 6, 15, 5, tzinfo=UTC),
            ],
        )

        assert strategy.calls == []
        assert result.equity_curve == []

    def test_backtester_passes_dst_invariant_reference_clock_to_strategy(self):
        from app.strategies.opening_fade import OpeningFadeStrategy

        strategy = CaptureSessionStrategy()

        Backtester(
            strategy=strategy,
            ticker="AAPL",
            start_date=datetime(2025, 3, 10, tzinfo=UTC).date(),
        ).run(
            [
                make_bar("100", "101", "99", "100"),
                make_bar("110", "111", "109", "110"),
                make_bar("110", "112", "109", "111"),
            ],
            [
                datetime(2025, 3, 7, 20, 55, tzinfo=UTC),
                datetime(2025, 3, 10, 13, 30, tzinfo=UTC),
                datetime(2025, 3, 10, 13, 35, tzinfo=UTC),
            ],
        )

        assert strategy.calls[-1]["current_time_utc"] == "14:35"
        assert strategy.calls[-1]["bar_times"][-1].strftime("%H:%M") == "14:35"
        assert OpeningFadeStrategy()._time_in_fade_window(
            str(strategy.calls[-1]["current_time_utc"])
        )

    def test_backtester_blocks_entries_on_early_close_sessions(self):
        strategy = CaptureSessionStrategy()

        result = Backtester(
            strategy=strategy,
            ticker="AAPL",
            start_date=datetime(2025, 7, 3, tzinfo=UTC).date(),
        ).run(
            [
                make_bar("100", "101", "99", "100"),
                make_bar("110", "111", "109", "110"),
                make_bar("110", "112", "109", "111"),
            ],
            [
                datetime(2025, 7, 2, 19, 55, tzinfo=UTC),
                datetime(2025, 7, 3, 13, 30, tzinfo=UTC),
                datetime(2025, 7, 3, 16, 55, tzinfo=UTC),
            ],
        )

        assert strategy.calls == []
        assert len(result.equity_curve) == 2

    def test_pending_entry_cannot_fill_across_session_boundary_into_early_close(self):
        class LateSessionEntry(OneShotStrategy):
            def generate_signal(self, **kwargs: object) -> SimpleNamespace | None:
                if kwargs["current_time_utc"] != "20:55":
                    return None
                return super().generate_signal(**kwargs)

        result = Backtester(
            strategy=LateSessionEntry(),
            ticker="AAPL",
            start_date=datetime(2025, 7, 2, tzinfo=UTC).date(),
        ).run(
            [
                make_bar("100", "101", "99", "100"),
                make_bar("101", "102", "100", "101"),
                make_bar("102", "103", "101", "102"),
                make_bar("103", "104", "102", "103"),
                make_bar("104", "105", "103", "104"),
            ],
            [
                datetime(2025, 7, 1, 19, 55, tzinfo=UTC),
                datetime(2025, 7, 2, 13, 30, tzinfo=UTC),
                datetime(2025, 7, 2, 19, 55, tzinfo=UTC),
                datetime(2025, 7, 3, 13, 30, tzinfo=UTC),
                datetime(2025, 7, 3, 16, 55, tzinfo=UTC),
            ],
        )

        assert result.trades == []
        assert result.final_capital == Decimal("10000")

    def test_backtest_and_runtime_runner_share_session_and_previous_close_context(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.services.strategy_runner import StrategyRunner

        strategy = CaptureSessionStrategy()
        bars = [
            make_bar("100", "101", "99", "100"),
            make_bar("105", "106", "104", "105"),
            make_bar("110", "111", "109", "110"),
            make_bar("110", "112", "109", "111"),
        ]
        bar_times = [
            datetime(2025, 1, 3, 20, 55, tzinfo=UTC),
            datetime(2025, 1, 6, 13, 0, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 35, tzinfo=UTC),
        ]
        runner_db = MagicMock()
        runner_db.execute = AsyncMock(side_effect=AssertionError("DB must not be called"))
        runner = StrategyRunner(runner_db)

        runtime_bars, runtime_times, runtime_prev_close = runner._extract_session_context(
            bars,
            bar_times,
            session_open_utc="14:30",
        )
        Backtester(
            strategy=strategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            start_date=datetime(2025, 1, 6, tzinfo=UTC).date(),
        ).run(
            [bars[0], *runtime_bars],
            [bar_times[0], *runtime_times],
        )

        assert runtime_prev_close == Decimal("100")
        assert strategy.calls[0]["prev_close"] == runtime_prev_close
        assert strategy.calls[0]["session_open"] == runtime_bars[0].open
        assert len(strategy.calls[0]["history_bars"]) == 2
        runtime_regular_history = [
            bar
            for bar, bar_time in zip(bars, bar_times, strict=True)
            if calendar_for_venue("XNYS").session_for_timestamp(bar_time) is not None
        ]
        assert len(strategy.calls[-1]["history_bars"]) == len(runtime_regular_history)

    def test_backtester_default_history_horizon_matches_runtime_cap(self):
        strategy = CaptureSessionStrategy()
        calendar = calendar_for_venue("XNYS")
        sessions = calendar.expected_sessions(
            datetime(2025, 1, 2, tzinfo=UTC).date(),
            datetime(2025, 6, 30, tzinfo=UTC).date(),
        )[:92]
        bars = [
            make_bar("100", "101", "99", "100") for _session in sessions for _timestamp in range(2)
        ]
        times = [
            timestamp
            for session in sessions
            for timestamp in (
                calendar.session_open(session),
                calendar.session_close(session) - timedelta(minutes=5),
            )
        ]

        Backtester(
            strategy=strategy,
            ticker="AAPL",
            start_date=sessions[1].local_date,
        ).run(bars, times)

        assert len(strategy.calls[-1]["history_bars"]) == 180

    def test_backtester_rejects_out_of_regular_session_bars_before_strategy(self):
        strategy = CaptureSessionStrategy()
        bars = [
            make_bar("105", "106", "104", "105"),
            make_bar("110", "111", "109", "110"),
        ]
        bar_times = [
            datetime(2025, 1, 6, 13, 0, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        ]

        with pytest.raises(ValueError, match="outside XNYS regular session"):
            Backtester(
                strategy=strategy,
                ticker="AAPL",
                initial_capital=Decimal("10000"),
            ).run(bars, bar_times)

        assert strategy.calls == []
