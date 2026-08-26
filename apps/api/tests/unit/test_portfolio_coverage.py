from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.backtest.portfolio_engine import (
    InsufficientPortfolioEvidence,
    PortfolioBacktester,
    PortfolioCoveragePolicy,
)
from app.market_data.exchange_calendar import calendar_for_venue
from app.strategies.indicators import Bar


def _bar(close: str) -> Bar:
    value = Decimal(close)
    return Bar(
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("1000"),
    )


def _history(*days: int) -> tuple[list[Bar], list[datetime]]:
    return (
        [_bar(str(100 + index)) for index in range(len(days))],
        [datetime(2025, 1, day, 21, 0, tzinfo=UTC) for day in days],
    )


def _history_for_dates(session_dates: list[date]) -> tuple[list[Bar], list[datetime]]:
    return (
        [_bar(str(100 + index)) for index in range(len(session_dates))],
        [
            datetime(session_date.year, session_date.month, session_date.day, 21, tzinfo=UTC)
            for session_date in session_dates
        ],
    )


class CaptureStrategy:
    label = "Capture"
    rebalance_frequency = "monthly"
    min_history_bars = 0

    def __init__(self) -> None:
        self.calls = 0

    def target_weights(
        self,
        history: dict[str, list[Bar]],
        *,
        as_of_index: int,
    ) -> dict[str, Decimal]:
        del history, as_of_index
        self.calls += 1
        return {"AAA": Decimal("0.5"), "BBB": Decimal("0.5")}


def test_missing_symbol_session_is_insufficient_before_strategy_invocation() -> None:
    strategy = CaptureStrategy()
    backtester = PortfolioBacktester(
        strategy=strategy,
        universe=["AAA", "BBB"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 8),
    )

    with pytest.raises(InsufficientPortfolioEvidence, match="insufficient_evidence") as exc_info:
        backtester.run(
            {
                "AAA": _history(6, 7, 8),
                "BBB": _history(6, 8),
            }
        )

    assert strategy.calls == 0
    report = exc_info.value.report
    bbb = next(item for item in report.symbols if item.ticker == "BBB")
    assert bbb.missing_session_ids == ("XNYS:2025-01-07",)
    assert bbb.coverage_pct == Decimal("66.67")
    assert bbb.longest_missing_run == 1
    assert bbb.first_valid_session_id == "XNYS:2025-01-06"
    assert bbb.last_valid_session_id == "XNYS:2025-01-08"
    assert report.retained_session_ids == ("XNYS:2025-01-06", "XNYS:2025-01-08")
    assert report.dropped_session_ids == ("XNYS:2025-01-07",)
    assert report.policy == "strict_complete"
    assert report.policy_id == "portfolio_session_coverage:strict_complete:v1"
    assert report.eligible is False
    assert report.common_start == date(2025, 1, 6)
    assert report.common_end == date(2025, 1, 8)


def test_explicit_lower_threshold_runs_with_intersection_disclosed_and_no_fill() -> None:
    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2025, 1, 6),
        date(2025, 2, 10),
    )[:20]
    session_dates = [session.local_date for session in sessions]
    missing_date = session_dates[10]
    partial_dates = [item for item in session_dates if item != missing_date]
    strategy = CaptureStrategy()
    result = PortfolioBacktester(
        strategy=strategy,
        universe=["AAA", "BBB"],
        initial_capital=Decimal("10000"),
        start_date=session_dates[0],
        end_date=session_dates[-1],
        coverage_policy=PortfolioCoveragePolicy(minimum_coverage_pct=Decimal("95")),
    ).run(
        {
            "AAA": _history_for_dates(session_dates),
            "BBB": _history_for_dates(partial_dates),
        }
    )

    assert [point.date for point in result.equity_curve] == partial_dates
    assert result.coverage_report.complete is False
    assert result.coverage_report.eligible is True
    assert result.coverage_report.policy == "intersection_with_disclosure"
    assert (
        result.coverage_report.policy_id
        == "portfolio_session_coverage:intersection_with_disclosure:v1"
    )
    assert result.coverage_report.common_start == partial_dates[0]
    assert result.coverage_report.common_end == partial_dates[-1]
    assert result.coverage_report.dropped_session_ids == (f"XNYS:{missing_date.isoformat()}",)
    assert result.coverage_report.minimum_coverage_pct == Decimal("95")


def test_incomplete_coverage_cannot_receive_positive_interpretation() -> None:
    from app.api.v1.routes.backtest import _interpret_portfolio_results

    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2025, 1, 6),
        date(2025, 2, 10),
    )[:20]
    session_dates = [session.local_date for session in sessions]
    partial_dates = session_dates[:-1]
    result = PortfolioBacktester(
        strategy=CaptureStrategy(),
        universe=["AAA", "BBB"],
        initial_capital=Decimal("10000"),
        start_date=session_dates[0],
        end_date=session_dates[-1],
        coverage_policy=PortfolioCoveragePolicy(minimum_coverage_pct=Decimal("95")),
    ).run(
        {
            "AAA": _history_for_dates(session_dates),
            "BBB": _history_for_dates(partial_dates),
        }
    )

    interpretation = _interpret_portfolio_results(result)

    assert interpretation["verdict"] == "insufficient_evidence"
    assert "incomplete session coverage" in interpretation["summary"].lower()


def test_complete_histories_report_full_coverage() -> None:
    result = PortfolioBacktester(
        strategy=CaptureStrategy(),
        universe=["AAA", "BBB"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 8),
    ).run({"AAA": _history(6, 7, 8), "BBB": _history(6, 7, 8)})

    report = result.coverage_report
    assert report.complete is True
    assert report.calendar == "XNYS"
    assert report.exchange_timezone == "America/New_York"
    assert report.expected_session_ids == (
        "XNYS:2025-01-06",
        "XNYS:2025-01-07",
        "XNYS:2025-01-08",
    )
    assert report.retained_session_ids == report.expected_session_ids
    assert report.dropped_session_ids == ()
    assert report.policy == "strict_complete"
    assert report.eligible is True
    assert report.common_start == date(2025, 1, 6)
    assert report.common_end == date(2025, 1, 8)
    assert all(
        item.coverage_pct == Decimal("100.00")
        and item.longest_missing_run == 0
        and item.first_valid_session_id == "XNYS:2025-01-06"
        and item.last_valid_session_id == "XNYS:2025-01-08"
        for item in report.symbols
    )


def test_coverage_reports_consecutive_missing_run() -> None:
    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2025, 1, 6),
        date(2025, 1, 10),
    )
    session_dates = [session.local_date for session in sessions]
    bbb_dates = [session_dates[0], *session_dates[3:]]

    with pytest.raises(InsufficientPortfolioEvidence) as exc_info:
        PortfolioBacktester(
            strategy=CaptureStrategy(),
            universe=["AAA", "BBB"],
            initial_capital=Decimal("10000"),
            start_date=session_dates[0],
            end_date=session_dates[-1],
        ).run(
            {
                "AAA": _history_for_dates(session_dates),
                "BBB": _history_for_dates(bbb_dates),
            }
        )

    bbb = next(item for item in exc_info.value.report.symbols if item.ticker == "BBB")
    assert bbb.longest_missing_run == 2
    assert bbb.first_valid_session_id == f"XNYS:{session_dates[0].isoformat()}"
    assert bbb.last_valid_session_id == f"XNYS:{session_dates[-1].isoformat()}"


def test_no_common_period_is_explicitly_ineligible() -> None:
    with pytest.raises(InsufficientPortfolioEvidence) as exc_info:
        PortfolioBacktester(
            strategy=CaptureStrategy(),
            universe=["AAA", "BBB"],
            initial_capital=Decimal("10000"),
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 7),
        ).run({"AAA": _history(6), "BBB": _history(7)})

    report = exc_info.value.report
    assert report.eligible is False
    assert report.common_start is None
    assert report.common_end is None


def test_extra_non_session_date_is_insufficient_before_strategy_invocation() -> None:
    strategy = CaptureStrategy()
    backtester = PortfolioBacktester(
        strategy=strategy,
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 3),
        end_date=date(2025, 1, 6),
    )

    with pytest.raises(InsufficientPortfolioEvidence, match="extra session") as exc_info:
        backtester.run({"AAA": _history(3, 4, 6)})

    assert strategy.calls == 0
    assert exc_info.value.report.symbols[0].extra_session_ids == ("XNYS:2025-01-04",)


def test_daily_timestamp_maps_to_exchange_local_session_date() -> None:
    strategy = CaptureStrategy()
    bar = _bar("100")
    result = PortfolioBacktester(
        strategy=strategy,
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
    ).run(
        {
            "AAA": (
                [bar],
                [datetime(2025, 1, 7, 0, 30, tzinfo=UTC)],
            )
        }
    )

    assert result.coverage_report.symbols[0].observed_session_ids == ("XNYS:2025-01-06",)
    assert result.equity_curve[0].date == date(2025, 1, 6)


def test_requested_universe_must_match_history_symbols() -> None:
    backtester = PortfolioBacktester(
        strategy=CaptureStrategy(),
        universe=["AAA", "BBB"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
    )

    with pytest.raises(ValueError, match="history symbols must exactly match"):
        backtester.run({"AAA": _history(6)})


@pytest.mark.parametrize(
    "minimum",
    [Decimal("0"), Decimal("94.99"), Decimal("100.01")],
)
def test_coverage_threshold_must_be_conservative_percentage(minimum: Decimal) -> None:
    with pytest.raises(ValueError, match="minimum_coverage_pct"):
        PortfolioCoveragePolicy(minimum_coverage_pct=minimum)


def test_unrounded_coverage_must_meet_threshold() -> None:
    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2020, 1, 2),
        date(2025, 1, 2),
    )[:1019]
    session_dates = [session.local_date for session in sessions]
    observed_dates = session_dates[:968]
    strategy = CaptureStrategy()

    with pytest.raises(InsufficientPortfolioEvidence) as exc_info:
        PortfolioBacktester(
            strategy=strategy,
            universe=["AAA"],
            initial_capital=Decimal("10000"),
            start_date=session_dates[0],
            end_date=session_dates[-1],
            coverage_policy=PortfolioCoveragePolicy(minimum_coverage_pct=Decimal("95")),
        ).run({"AAA": _history_for_dates(observed_dates)})

    assert exc_info.value.report.symbols[0].coverage_pct == Decimal("95.00")
    assert strategy.calls == 0


def test_disjoint_symbol_gaps_cannot_reduce_intersection_below_threshold() -> None:
    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2025, 1, 6),
        date(2025, 2, 10),
    )[:20]
    session_dates = [session.local_date for session in sessions]
    aaa_dates = [item for index, item in enumerate(session_dates) if index != 10]
    bbb_dates = [item for index, item in enumerate(session_dates) if index != 11]
    strategy = CaptureStrategy()

    with pytest.raises(InsufficientPortfolioEvidence, match="retained intersection") as exc_info:
        PortfolioBacktester(
            strategy=strategy,
            universe=["AAA", "BBB"],
            initial_capital=Decimal("10000"),
            start_date=session_dates[0],
            end_date=session_dates[-1],
            coverage_policy=PortfolioCoveragePolicy(minimum_coverage_pct=Decimal("95")),
        ).run(
            {
                "AAA": _history_for_dates(aaa_dates),
                "BBB": _history_for_dates(bbb_dates),
            }
        )

    assert exc_info.value.report.retained_coverage_pct == Decimal("90.00")
    assert strategy.calls == 0


def test_portfolio_api_serialization_includes_coverage_report() -> None:
    from app.api.v1.routes.backtest import _serialize_portfolio_backtest_result

    result = PortfolioBacktester(
        strategy=CaptureStrategy(),
        universe=["AAA"],
        initial_capital=Decimal("10000"),
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
    ).run({"AAA": _history(6)})

    payload = _serialize_portfolio_backtest_result(
        result=result,
        strategy_type="equal_weight_rebalance",
        strategy_label="Equal Weight",
        rationale="test",
    )

    assert payload["coverage"] == {
        "calendar": "XNYS",
        "exchange_timezone": "America/New_York",
        "requested_from": "2025-01-06",
        "requested_to": "2025-01-06",
        "minimum_coverage_pct": 100.0,
        "retained_coverage_pct": 100.0,
        "complete": True,
        "policy": "strict_complete",
        "policy_id": "portfolio_session_coverage:strict_complete:v1",
        "eligible": True,
        "common_from": "2025-01-06",
        "common_to": "2025-01-06",
        "expected_session_ids": ["XNYS:2025-01-06"],
        "retained_session_ids": ["XNYS:2025-01-06"],
        "dropped_session_ids": [],
        "symbols": [
            {
                "ticker": "AAA",
                "expected_session_ids": ["XNYS:2025-01-06"],
                "observed_session_ids": ["XNYS:2025-01-06"],
                "missing_session_ids": [],
                "extra_session_ids": [],
                "coverage_pct": 100.0,
                "longest_missing_run": 0,
                "first_valid_session_id": "XNYS:2025-01-06",
                "last_valid_session_id": "XNYS:2025-01-06",
            }
        ],
    }


@pytest.mark.asyncio
async def test_portfolio_job_represents_coverage_failure_as_insufficient_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.routes import backtest as route
    from app.backtest.data_fetcher import BacktestDataFetcher

    sessions = calendar_for_venue("XNYS").expected_sessions(
        date(2025, 1, 6),
        date(2025, 2, 10),
    )[:20]
    session_dates = [session.local_date for session in sessions]

    async def fake_fetch(
        self: BacktestDataFetcher,
        ticker: str,
        **kwargs: object,
    ) -> tuple[list[Bar], list[datetime]]:
        del self, kwargs
        omitted_index = 10 if ticker == "AAA" else 11
        observed_dates = [
            item for index, item in enumerate(session_dates) if index != omitted_index
        ]
        return _history_for_dates(observed_dates)

    monkeypatch.setattr(BacktestDataFetcher, "fetch_bars", fake_fetch)
    job_id = "coverage-insufficient-test"
    body = route.PortfolioBacktestRequest(
        tickers=["AAA", "BBB"],
        strategy_type="equal_weight_rebalance",
        from_date=session_dates[0],
        to_date=session_dates[-1],
    )

    await route._run_portfolio_backtest_job(job_id, body)
    payload = route._portfolio_jobs.pop(job_id)

    assert payload["status"] == "complete"
    assert payload["verdict"] == "insufficient_evidence"
    assert payload["bars_used"] == 18
    assert payload["coverage"]["retained_coverage_pct"] == 90.0
    assert len(payload["coverage"]["dropped_session_ids"]) == 2
    assert "traceback" not in payload
