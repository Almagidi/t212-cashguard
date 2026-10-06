from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.backtest.data_fetcher import BacktestDataFetcher
from app.backtest.engine import Backtester, WalkForwardValidator
from app.backtest.portfolio_engine import PortfolioBacktester
from app.backtest.portfolio_strategies import EqualWeightRebalanceStrategy
from app.market_data.exchange_calendar import calendar_for_venue
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


@pytest.mark.asyncio
async def test_single_symbol_job_requests_previous_xnys_session_as_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.routes import backtest as route
    from app.backtest.data_fetcher import BacktestDataFetcher

    requested_from: list[date] = []

    async def capture_fetch(
        self: BacktestDataFetcher,
        ticker: str,
        from_date: date,
        to_date: date,
        **kwargs: object,
    ) -> tuple[list[Bar], list[datetime]]:
        del self, ticker, to_date, kwargs
        requested_from.append(from_date)
        raise RuntimeError("stop after request capture")

    monkeypatch.setattr(BacktestDataFetcher, "fetch_bars", capture_fetch)
    job_id = "warmup-request-test"
    body = route.BacktestRequest(
        ticker="AAPL",
        strategy_type="orb",
        from_date=datetime(2025, 1, 6, tzinfo=UTC).date(),
        to_date=datetime(2025, 1, 10, tzinfo=UTC).date(),
    )

    await route._run_backtest_job(job_id, body)
    route._jobs.pop(job_id)

    assert requested_from == [datetime(2025, 1, 3, tzinfo=UTC).date()]


@pytest.mark.asyncio
async def test_single_symbol_job_attaches_immutable_dataset_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.routes import backtest as route
    from app.backtest.data_fetcher import BacktestDataFetcher

    async def fake_fetch(
        self: BacktestDataFetcher,
        ticker: str,
        **kwargs: object,
    ) -> tuple[list[Bar], list[datetime]]:
        del kwargs
        self._manifests[ticker] = {
            "manifest_id": "manifest-AAPL",
            "canonical_sha256": "content-AAPL",
        }
        start = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
        times = [start + timedelta(minutes=5 * index) for index in range(60)]
        return [make_bar() for _ in times], times

    monkeypatch.setattr(BacktestDataFetcher, "fetch_bars", fake_fetch)
    job_id = "dataset-manifest-test"
    body = route.BacktestRequest(
        ticker="AAPL",
        strategy_type="orb",
        from_date=date(2025, 1, 6),
        to_date=date(2025, 1, 6),
    )

    await route._run_backtest_job(job_id, body)
    payload = route._jobs.pop(job_id)

    assert payload["status"] == "complete"
    assert payload["datasets"] == [
        {"manifest_id": "manifest-AAPL", "canonical_sha256": "content-AAPL"}
    ]


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
    first = datetime(2026, 1, 5, 15, tzinfo=UTC)
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

    sessions = calendar_for_venue("XNYS").expected_sessions(
        datetime(2026, 1, 2, tzinfo=UTC).date(),
        datetime(2026, 5, 1, tzinfo=UTC).date(),
    )
    bar_times = [session.close_at.astimezone(UTC) for session in sessions[:65]]
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


def test_walk_forward_partitions_are_session_aligned_with_prior_warmup(monkeypatch) -> None:
    calls: list[tuple[date | None, list[datetime]]] = []

    class FakeStrategy:
        def __init__(self, params: dict[str, object]) -> None:
            self.params = params

    class FakeBacktester:
        def __init__(self, _strategy: object, _ticker: str, _capital: Decimal, **kwargs: object):
            self.start_date = kwargs.get("start_date")

        def run(self, _bars: list[Bar], times: list[datetime]) -> Any:
            calls.append((self.start_date, times))
            return type(
                "Result",
                (),
                {
                    "total_trades": 10,
                    "completed_positions": 10,
                    "total_return_pct": Decimal("1"),
                    "sharpe_ratio": Decimal("1"),
                    "max_drawdown_pct": Decimal("1"),
                    "win_rate": Decimal("0.5"),
                    "profit_factor": Decimal("1.2"),
                },
            )()

    monkeypatch.setattr("app.backtest.engine.Backtester", FakeBacktester)
    session_dates = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]
    times = [
        timestamp
        for session_date in session_dates
        for timestamp in (
            datetime.combine(session_date, datetime.min.time(), UTC).replace(hour=14, minute=30),
            datetime.combine(session_date, datetime.min.time(), UTC).replace(hour=20, minute=55),
        )
    ]
    bars = [
        make_bar(
            open_=str(100 + index),
            high=str(101 + index),
            low=str(99 + index),
            close=str(100 + index),
        )
        for index in range(len(times))
    ]
    validator = WalkForwardValidator(
        strategy_class=FakeStrategy,
        ticker="AAPL",
        initial_capital=Decimal("10000"),
        in_sample_bars=2,
        out_sample_bars=2,
        step_bars=1,
    )

    result = validator.run(bars, times, [{}])

    assert len(result) == 1
    assert [start for start, _ in calls] == session_dates[1:]
    assert [partition[0].date() for _, partition in calls] == session_dates[:3]
    assert [partition[-1].date() for _, partition in calls] == session_dates[1:]


def test_walk_forward_rejects_session_that_starts_after_exchange_open() -> None:
    validator = WalkForwardValidator(
        strategy_class=CaptureStrategy,
        ticker="AAPL",
        initial_capital=Decimal("10000"),
        in_sample_bars=2,
        out_sample_bars=2,
        step_bars=1,
    )
    times = [
        datetime(2025, 1, 3, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 3, 20, 55, tzinfo=UTC),
        datetime(2025, 1, 6, 15, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 20, 55, tzinfo=UTC),
    ]

    with pytest.raises(ValueError, match="does not begin at session open"):
        validator.run([make_bar() for _ in times], times, [{}])


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
async def test_data_fetcher_publishes_and_reuses_verified_secret_free_dataset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    import httpx

    class SuccessfulResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "status": "OK",
                "resultsCount": 2,
                "results": [
                    {
                        "o": 100,
                        "h": 101,
                        "l": 99,
                        "c": 100,
                        "v": 1000,
                        "t": 1767623400000,
                    },
                    {
                        "o": 101,
                        "h": 102,
                        "l": 100,
                        "c": 101,
                        "v": 1100,
                        "t": 1767623700000,
                    },
                ],
            }

    class SuccessfulClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def __aenter__(self) -> SuccessfulClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def get(self, *args: Any, **kwargs: Any) -> SuccessfulResponse:
            del args, kwargs
            return SuccessfulResponse()

    monkeypatch.setattr("app.backtest.data_fetcher.CACHE_DIR", tmp_path)
    monkeypatch.setattr(httpx, "AsyncClient", SuccessfulClient)
    monkeypatch.setenv("T212_CODE_SHA", "f" * 40)
    first_fetcher = BacktestDataFetcher("secret-test-key")
    first = await first_fetcher.fetch_bars(
        "AAPL",
        date(2026, 1, 5),
        date(2026, 1, 5),
    )

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("cache hit must not contact provider")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    second_fetcher = BacktestDataFetcher("different-secret-key")
    second = await second_fetcher.fetch_bars(
        "AAPL",
        date(2026, 1, 5),
        date(2026, 1, 5),
    )

    assert second == first
    assert second_fetcher.manifest_for("AAPL") == first_fetcher.manifest_for("AAPL")
    persisted = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert "secret-test-key" not in persisted
    assert "different-secret-key" not in persisted


@pytest.mark.asyncio
async def test_data_fetcher_fails_closed_on_dangling_cache_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    import httpx

    from app.backtest.dataset_cache import ImmutableDatasetCache

    class SuccessfulResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "status": "OK",
                "resultsCount": 1,
                "results": [
                    {
                        "o": 100,
                        "h": 101,
                        "l": 99,
                        "c": 100,
                        "v": 1000,
                        "t": 1767623400000,
                    }
                ],
            }

    class SuccessfulClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def __aenter__(self) -> SuccessfulClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def get(self, *args: Any, **kwargs: Any) -> SuccessfulResponse:
            del args, kwargs
            return SuccessfulResponse()

    monkeypatch.setattr("app.backtest.data_fetcher.CACHE_DIR", tmp_path)
    monkeypatch.setattr(httpx, "AsyncClient", SuccessfulClient)
    monkeypatch.setenv("T212_CODE_SHA", "f" * 40)
    initial_fetcher = BacktestDataFetcher("test-key")
    await initial_fetcher.fetch_bars(
        "AAPL",
        date(2026, 1, 5),
        date(2026, 1, 5),
    )

    request = initial_fetcher._request(
        "AAPL",
        date(2026, 1, 5),
        date(2026, 1, 5),
        5,
        "minute",
        "single_symbol_request",
        None,
    )
    cache = ImmutableDatasetCache(tmp_path)
    cache.object_path(initial_fetcher.manifest_for("AAPL")["canonical_sha256"]).unlink()
    assert cache.reference_path(request).exists()

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("dangling cache reference must not contact provider")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)

    with pytest.raises(FileNotFoundError):
        await BacktestDataFetcher("test-key").fetch_bars(
            "AAPL",
            date(2026, 1, 5),
            date(2026, 1, 5),
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


@pytest.mark.asyncio
async def test_data_fetcher_rejects_partial_provider_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    import httpx

    class PartialResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "status": "OK",
                "resultsCount": 1,
                "next_url": "https://api.polygon.io/v2/aggs/next-page",
                "results": [
                    {
                        "o": 100,
                        "h": 101,
                        "l": 99,
                        "c": 100,
                        "v": 1000,
                        "t": 1767623400000,
                    }
                ],
            }

    class PartialClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def __aenter__(self) -> PartialClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def get(self, *args: Any, **kwargs: Any) -> PartialResponse:
            del args, kwargs
            return PartialResponse()

    monkeypatch.setattr("app.backtest.data_fetcher.CACHE_DIR", tmp_path)
    monkeypatch.setattr(httpx, "AsyncClient", PartialClient)

    with pytest.raises(RuntimeError, match="partial provider page"):
        await BacktestDataFetcher("test-key").fetch_bars(
            "AAPL",
            date(2026, 1, 5),
            date(2026, 1, 5),
        )

    assert list(tmp_path.rglob("*.json")) == []
