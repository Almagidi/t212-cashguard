from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.backtest.data_fetcher import BacktestDataFetcher
from app.backtest.engine import Backtester, WalkForwardValidator
from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_strategies import EqualWeightRebalanceStrategy
from app.strategies.indicators import Bar


def make_bar(
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100.5",
    volume: str = "1000",
) -> Bar:
    return Bar(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


class CaptureStrategy:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Decimal, ...], tuple[Decimal, ...]]] = []

    def generate_signal(
        self,
        *,
        ticker: str,
        bars: list[Bar],
        history_bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> None:
        del ticker, account_value, available_cash, current_time_utc, prev_close
        self.calls.append(
            (
                tuple(bar.close for bar in bars),
                tuple(bar.close for bar in history_bars),
            )
        )
        return None


@pytest.mark.parametrize(
    ("bars", "bar_times", "message"),
    [
        (
            [make_bar(), make_bar()],
            [
                datetime(2026, 1, 5, 14, 35, tzinfo=UTC),
                datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            ],
            "strictly increasing",
        ),
        (
            [make_bar(), make_bar()],
            [
                datetime(2026, 10, 25, 1, 15, tzinfo=ZoneInfo("Europe/London"), fold=1),
                datetime(2026, 10, 25, 1, 45, tzinfo=ZoneInfo("Europe/London"), fold=0),
            ],
            "strictly increasing",
        ),
        (
            [make_bar(), make_bar()],
            [
                datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
                datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            ],
            "duplicate timestamp",
        ),
        (
            [make_bar()],
            [datetime(2026, 1, 5, 14, 30)],
            "timezone-aware",
        ),
        (
            [make_bar(high="99")],
            [datetime(2026, 1, 5, 14, 30, tzinfo=UTC)],
            "high",
        ),
        (
            [make_bar(low="101")],
            [datetime(2026, 1, 5, 14, 30, tzinfo=UTC)],
            "low",
        ),
        (
            [make_bar(open_="0")],
            [datetime(2026, 1, 5, 14, 30, tzinfo=UTC)],
            "positive",
        ),
        (
            [make_bar(volume="-1")],
            [datetime(2026, 1, 5, 14, 30, tzinfo=UTC)],
            "volume",
        ),
        (
            [make_bar(close="NaN")],
            [datetime(2026, 1, 5, 14, 30, tzinfo=UTC)],
            "finite",
        ),
    ],
)
def test_backtester_rejects_invalid_input_before_strategy_invocation(
    bars: list[Bar],
    bar_times: list[datetime],
    message: str,
) -> None:
    strategy = CaptureStrategy()

    with pytest.raises(ValueError, match=message):
        Backtester(strategy=strategy, ticker="AAPL").run(bars, bar_times)

    assert strategy.calls == []


def test_backtester_rejects_mismatched_bar_and_timestamp_counts() -> None:
    strategy = CaptureStrategy()

    with pytest.raises(ValueError, match="same length"):
        Backtester(strategy=strategy, ticker="AAPL").run(
            [make_bar()],
            [],
        )

    assert strategy.calls == []


def test_future_mutation_cannot_change_prior_strategy_inputs() -> None:
    def bar_at(close: Decimal) -> Bar:
        return make_bar(
            open_=str(close),
            high=str(close + 1),
            low=str(close - 1),
            close=str(close),
        )

    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bar_times = [start + timedelta(minutes=5 * index) for index in range(8)]
    bars = [bar_at(Decimal("100") + index) for index in range(8)]
    mutated = [*bars[:5], *(bar_at(Decimal("900") + index) for index in range(3))]
    baseline_strategy = CaptureStrategy()
    mutated_strategy = CaptureStrategy()

    Backtester(strategy=baseline_strategy, ticker="AAPL").run(bars, bar_times)
    Backtester(strategy=mutated_strategy, ticker="AAPL").run(mutated, bar_times)

    assert mutated_strategy.calls[:5] == baseline_strategy.calls[:5]


def test_portfolio_backtester_rejects_duplicate_dates_before_strategy_invocation() -> None:
    class CapturePortfolioStrategy(EqualWeightRebalanceStrategy):
        def __init__(self) -> None:
            super().__init__({"rebalance_frequency": "monthly"})
            self.calls = 0

        def target_weights(
            self,
            history: dict[str, list[Bar]],
            as_of_index: int,
        ) -> dict[str, Decimal]:
            self.calls += 1
            return super().target_weights(history, as_of_index=as_of_index)

    strategy = CapturePortfolioStrategy()
    first = datetime(2026, 1, 5, tzinfo=UTC)
    same_date = datetime(2026, 1, 5, 16, tzinfo=UTC)
    history = ([make_bar(), make_bar()], [first, same_date])
    backtester = PortfolioBacktester(
        strategy=strategy,
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=first.date(),
        end_date=first.date(),
    )

    with pytest.raises(ValueError, match="duplicate date"):
        backtester.run({"AAA": history})

    assert strategy.calls == 0


def test_portfolio_strategy_cannot_observe_future_bars() -> None:
    class CapturePortfolioHistoryStrategy:
        label = "Capture portfolio history"
        rebalance_frequency = "monthly"
        min_history_bars = 1

        def __init__(self) -> None:
            self.calls: list[tuple[Decimal, ...]] = []

        def target_weights(
            self,
            history: dict[str, list[Bar]],
            *,
            as_of_index: int,
        ) -> dict[str, Decimal]:
            del as_of_index
            self.calls.append(tuple(bar.close for bar in history["AAA"]))
            return {"AAA": Decimal("1")}

    def daily_bar(close: Decimal) -> Bar:
        return make_bar(
            open_=str(close),
            high=str(close + 1),
            low=str(close - 1),
            close=str(close),
        )

    start = datetime(2026, 1, 1, tzinfo=UTC)
    bar_times = [start + timedelta(days=index) for index in range(65)]
    bars = [daily_bar(Decimal("100") + index) for index in range(65)]
    mutated_bars = [
        *bars[:40],
        *(daily_bar(Decimal("900") + index) for index in range(25)),
    ]
    baseline_strategy = CapturePortfolioHistoryStrategy()
    mutated_strategy = CapturePortfolioHistoryStrategy()

    for strategy, candidate_bars in (
        (baseline_strategy, bars),
        (mutated_strategy, mutated_bars),
    ):
        PortfolioBacktester(
            strategy=strategy,
            universe=["AAA"],
            initial_capital=Decimal("10000"),
            start_date=bar_times[0].date(),
            end_date=bar_times[-1].date(),
        ).run({"AAA": (candidate_bars, bar_times)})

    assert baseline_strategy.calls[0] == (bars[0].close,)
    assert baseline_strategy.calls[:2] == mutated_strategy.calls[:2]


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("in_sample_bars", 0),
        ("out_sample_bars", 0),
        ("step_bars", 0),
        ("step_bars", -1),
    ],
)
def test_walk_forward_rejects_non_positive_window_configuration(
    parameter: str,
    value: int,
) -> None:
    kwargs = {parameter: value}

    with pytest.raises(ValueError, match=parameter):
        WalkForwardValidator(
            strategy_class=CaptureStrategy,
            ticker="AAPL",
            initial_capital=Decimal("10000"),
            **kwargs,
        )


def test_walk_forward_rejects_timestamp_regression_across_window_boundary() -> None:
    validator = WalkForwardValidator(
        strategy_class=CaptureStrategy,
        ticker="AAPL",
        initial_capital=Decimal("10000"),
        in_sample_bars=2,
        out_sample_bars=1,
        step_bars=1,
    )
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

    with pytest.raises(ValueError, match="strictly increasing"):
        validator.run(
            [make_bar(), make_bar(), make_bar()],
            [start, start + timedelta(minutes=10), start + timedelta(minutes=5)],
            [{}],
        )


def test_data_fetcher_rejects_invalid_provider_rows() -> None:
    fetcher = BacktestDataFetcher("test-key")

    with pytest.raises(ValueError, match="high"):
        fetcher._parse_raw(
            [
                {
                    "o": 100,
                    "h": 99,
                    "l": 98,
                    "c": 100,
                    "v": 1000,
                    "t": 1767623400000,
                }
            ]
        )


@pytest.mark.asyncio
async def test_data_fetcher_fails_closed_on_provider_chunk_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    import httpx

    class SuccessfulResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "status": "OK",
                "results": [
                    {
                        "o": 100,
                        "h": 101,
                        "l": 99,
                        "c": 100,
                        "v": 1000,
                        "t": 1767259800000,
                    }
                ],
            }

    class FailedResponse:
        status_code = 503

        def json(self) -> dict[str, Any]:
            return {"status": "ERROR"}

    class FailedClient:
        calls = 0

        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def __aenter__(self) -> FailedClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def get(self, *args: Any, **kwargs: Any) -> SuccessfulResponse | FailedResponse:
            del args, kwargs
            self.__class__.calls += 1
            if self.calls == 1:
                return SuccessfulResponse()
            return FailedResponse()

    monkeypatch.setattr("app.backtest.data_fetcher.CACHE_DIR", tmp_path)
    monkeypatch.setattr(httpx, "AsyncClient", FailedClient)

    with pytest.raises(RuntimeError, match="503"):
        await BacktestDataFetcher("test-key").fetch_bars(
            "AAPL",
            datetime(2026, 1, 1).date(),
            datetime(2026, 2, 2).date(),
        )

    assert FailedClient.calls == 2
    assert list(tmp_path.iterdir()) == []
