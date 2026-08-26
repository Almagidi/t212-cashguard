from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.backtest.portfolio_engine import (
    InsufficientPortfolioEvidence,
    PortfolioCoveragePolicy,
    _align_operational_histories,
    _align_research_histories,
)
from app.core.config import settings
from app.market_data.exchange_calendar import calendar_for_venue
from app.services import portfolio_execution_service as portfolio_service_module
from app.services.portfolio_execution_service import MarketSnapshot, PortfolioExecutionService
from app.strategies.indicators import Bar

NY = ZoneInfo("America/New_York")


def _bar(close: str) -> Bar:
    price = Decimal(close)
    return Bar(
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000000"),
    )


def _daily_time(local_date: date) -> datetime:
    return datetime.combine(local_date, time(0), NY).astimezone(UTC)


def _history(*local_dates: date) -> tuple[list[Bar], list[datetime]]:
    return (
        [_bar(str(100 + index)) for index, _ in enumerate(local_dates)],
        [_daily_time(item) for item in local_dates],
    )


def test_operational_alignment_excludes_current_partial_session() -> None:
    dates = tuple(date(2026, 1, day) for day in range(5, 10))
    aligned_dates, aligned, report = _align_operational_histories(
        {"SPY": _history(*dates), "QQQ": _history(*dates)},
        universe=["SPY", "QQQ"],
        as_of=datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
    )

    assert aligned_dates == list(dates[:-1])
    assert {ticker: len(bars) for ticker, bars in aligned.items()} == {"QQQ": 4, "SPY": 4}
    assert report.complete is True
    assert report.requested_end == date(2026, 1, 8)


def test_operational_alignment_maps_daily_labels_across_dst() -> None:
    dates = (date(2026, 3, 6), date(2026, 3, 9), date(2026, 3, 10))
    aligned_dates, _, _ = _align_operational_histories(
        {"SPY": _history(*dates)},
        universe=["SPY"],
        as_of=datetime(2026, 3, 10, 14, 0, tzinfo=UTC),
    )

    assert _daily_time(date(2026, 3, 6)).hour == 5
    assert _daily_time(date(2026, 3, 9)).hour == 4
    assert aligned_dates == [date(2026, 3, 6), date(2026, 3, 9)]


def test_operational_history_matches_research_prior_close_horizon() -> None:
    dates = tuple(date(2026, 1, day) for day in range(5, 10))
    histories = {"SPY": _history(*dates), "QQQ": _history(*dates)}

    research_dates, research_history, _ = _align_research_histories(
        histories,
        universe=["SPY", "QQQ"],
        start_date=dates[0],
        end_date=dates[-1],
        coverage_policy=PortfolioCoveragePolicy(),
    )
    operational_dates, operational_history, _ = _align_operational_histories(
        histories,
        universe=["SPY", "QQQ"],
        as_of=datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
    )

    assert operational_dates == research_dates[:-1]
    assert operational_history == {ticker: bars[:-1] for ticker, bars in research_history.items()}


def test_current_partial_mutation_cannot_change_operational_history() -> None:
    dates = tuple(date(2026, 1, day) for day in range(5, 10))
    baseline = _history(*dates)
    mutated_bars = list(baseline[0])
    mutated_bars[-1] = _bar("9999")

    _, baseline_history, _ = _align_operational_histories(
        {"SPY": baseline},
        universe=["SPY"],
        as_of=datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
    )
    _, mutated_history, _ = _align_operational_histories(
        {"SPY": (mutated_bars, baseline[1])},
        universe=["SPY"],
        as_of=datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
    )

    assert mutated_history == baseline_history


def test_operational_alignment_uses_friday_during_weekend() -> None:
    dates = (date(2026, 1, 8), date(2026, 1, 9))
    aligned_dates, _, report = _align_operational_histories(
        {"SPY": _history(*dates)},
        universe=["SPY"],
        as_of=datetime(2026, 1, 10, 17, 0, tzinfo=UTC),
    )

    assert aligned_dates[-1] == date(2026, 1, 9)
    assert report.requested_end == date(2026, 1, 9)


def test_operational_alignment_uses_prior_session_during_exchange_holiday() -> None:
    dates = (date(2026, 1, 15), date(2026, 1, 16))
    aligned_dates, _, report = _align_operational_histories(
        {"SPY": _history(*dates)},
        universe=["SPY"],
        as_of=datetime(2026, 1, 19, 17, 0, tzinfo=UTC),
    )

    assert aligned_dates[-1] == date(2026, 1, 16)
    assert report.requested_end == date(2026, 1, 16)


def test_operational_decision_after_close_targets_next_execution_session() -> None:
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    decision_date, history, report = service._aligned_decision_history(
        {"SPY": _history(date(2026, 1, 8), date(2026, 1, 9))},
        universe=["SPY"],
        as_of=datetime(2026, 1, 9, 22, 0, tzinfo=UTC),
    )

    assert report.common_end == date(2026, 1, 9)
    assert len(history["SPY"]) == 2
    assert decision_date == date(2026, 1, 12)


@pytest.mark.parametrize(
    "histories",
    [
        {
            "SPY": _history(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)),
            "QQQ": _history(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 8)),
        },
        {
            "SPY": _history(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)),
            "QQQ": _history(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)),
        },
        {
            "SPY": _history(date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 10)),
            "QQQ": _history(date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 10)),
        },
    ],
    ids=["missing-middle-session", "stale-through-latest-close", "weekend-bar"],
)
def test_operational_alignment_rejects_incomplete_or_out_of_calendar_history(
    histories: dict[str, tuple[list[Bar], list[datetime]]],
) -> None:
    with pytest.raises(InsufficientPortfolioEvidence, match="insufficient_evidence"):
        _align_operational_histories(
            histories,
            universe=["SPY", "QQQ"],
            as_of=datetime(2026, 1, 9, 22, 0, tzinfo=UTC),
        )


def test_operational_alignment_requires_exact_universe() -> None:
    with pytest.raises(InsufficientPortfolioEvidence, match="requested universe"):
        _align_operational_histories(
            {"SPY": _history(date(2026, 1, 8))},
            universe=["SPY", "QQQ"],
            as_of=datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
        )


class _FakeDb:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


@dataclass
class _ProviderBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@pytest.mark.asyncio
async def test_provider_request_covers_required_completed_sessions() -> None:
    calls: list[dict[str, Any]] = []

    class RecordingProvider:
        async def get_bars(self, ticker: str, **kwargs: Any) -> list[_ProviderBar]:
            calls.append({"ticker": ticker, **kwargs})
            sessions = calendar_for_venue("XNYS").expected_sessions(
                kwargs["from_date"], kwargs["to_date"]
            )
            return [
                _ProviderBar(
                    timestamp=_daily_time(session.local_date),
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("1000"),
                )
                for session in sessions
            ]

        async def get_quote(self, _ticker: str) -> None:
            return None

        async def is_market_open(self) -> bool:
            return True

    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]
    as_of = datetime(2026, 1, 9, 15, 0, tzinfo=UTC)
    snapshot = await service._snapshot_from_provider(RecordingProvider(), ["SPY"], 148, as_of)

    assert len(snapshot.histories["SPY"][0]) == 148
    assert calls == [
        {
            "ticker": "SPY",
            "multiplier": 1,
            "timespan": "day",
            "from_date": date(2025, 6, 9),
            "to_date": date(2026, 1, 8),
            "limit": 148,
        }
    ]


@pytest.mark.asyncio
async def test_runtime_skips_before_strategy_invocation_when_history_is_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict[str, list[Bar]], int]] = []

    class CapturingStrategy:
        rebalance_frequency = "monthly"

        def __init__(self, _params: dict[str, Any]) -> None:
            self.min_history_bars = 3

        def target_weights(
            self, history: dict[str, list[Bar]], *, as_of_index: int
        ) -> dict[str, Decimal]:
            calls.append((history, as_of_index))
            return {}

    snapshot = MarketSnapshot(
        histories={
            "SPY": _history(date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)),
        },
        latest_prices={"SPY": Decimal("102")},
        latest_quote_times={"SPY": datetime(2026, 1, 9, 15, 0, tzinfo=UTC)},
        market_open=True,
        provider_name="fake_provider",
    )
    strategy = SimpleNamespace(
        id="portfolio-session-test",
        name="Portfolio session test",
        type="buy_hold_core",
        is_live=False,
        params={},
        allowed_tickers=["SPY"],
        risk_profile=None,
    )
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    async def load_snapshot(*_args: Any) -> MarketSnapshot:
        return snapshot

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(service, "_load_market_snapshot", load_snapshot)
    monkeypatch.setattr(
        service,
        "_decision_now",
        lambda: datetime(2026, 1, 9, 15, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        portfolio_service_module,
        "get_portfolio_backtest_strategy",
        lambda _strategy_type: {
            "strategy_class": CapturingStrategy,
            "min_history_bars": 1,
        },
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=object(),
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-session-test",
    )

    assert summary == {"status": "skipped", "reason": "insufficient_history"}
    assert calls == []
    state = strategy.params["portfolio_execution"]
    assert state["last_reason"] == "insufficient_history"
    assert state["last_history_policy"] == "strict_complete"
    assert state["last_history_policy_id"] == "portfolio_session_coverage:strict_complete:v1"
    assert state["last_history_eligible"] is True
    assert state["last_history_common_start"] == "2026-01-07"
    assert state["last_history_common_end"] == "2026-01-08"
    assert state["last_history_expected_sessions"] == 2
    assert state["last_history_retained_sessions"] == 2
    assert state["last_history_dropped_sessions"] == 0
    audit = service.db.added[-1]
    assert audit.payload["history_policy"] == "strict_complete"
    assert audit.payload["history_eligible"] is True
    assert audit.payload["history_common_start"] == "2026-01-07"
    assert audit.payload["history_common_end"] == "2026-01-08"


@pytest.mark.asyncio
async def test_runtime_exact_declared_history_minimum_can_invoke_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class CapturingStrategy:
        rebalance_frequency = "monthly"

        def __init__(self, _params: dict[str, Any]) -> None:
            self.min_history_bars = 3

        def target_weights(
            self, history: dict[str, list[Bar]], *, as_of_index: int
        ) -> dict[str, Decimal]:
            calls.append(len(history["SPY"]))
            assert as_of_index == 2
            return {}

    snapshot = MarketSnapshot(
        histories={"SPY": _history(date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8))},
        latest_prices={"SPY": Decimal("102")},
        latest_quote_times={"SPY": datetime(2026, 1, 8, 21, 0, tzinfo=UTC)},
        market_open=True,
        provider_name="fake_provider",
    )
    strategy = SimpleNamespace(
        id="portfolio-exact-minimum-test",
        name="Portfolio exact minimum test",
        type="buy_hold_core",
        is_live=False,
        params={},
        allowed_tickers=["SPY"],
        risk_profile=None,
    )
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    async def load_snapshot(*_args: Any) -> MarketSnapshot:
        return snapshot

    async def load_instruments(*_args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(service, "_load_market_snapshot", load_snapshot)
    monkeypatch.setattr(service, "_decision_now", lambda: datetime(2026, 1, 9, 15, 0, tzinfo=UTC))
    monkeypatch.setattr(service, "_load_instruments", load_instruments)
    monkeypatch.setattr(
        portfolio_service_module,
        "get_portfolio_backtest_strategy",
        lambda _strategy_type: {
            "strategy_class": CapturingStrategy,
            "min_history_bars": 1,
        },
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=object(),
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-exact-minimum-test",
    )

    assert summary["status"] == "rebalanced"
    assert calls == [3]


@pytest.mark.asyncio
async def test_runtime_persists_and_audits_ineligible_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCalledStrategy:
        rebalance_frequency = "monthly"
        min_history_bars = 0

        def __init__(self, _params: dict[str, Any]) -> None:
            return None

        def target_weights(
            self, _history: dict[str, list[Bar]], *, as_of_index: int
        ) -> dict[str, Decimal]:
            raise AssertionError(f"target_weights must not be called at {as_of_index}")

    snapshot = MarketSnapshot(
        histories={
            "SPY": _history(
                date(2026, 1, 5),
                date(2026, 1, 6),
                date(2026, 1, 7),
                date(2026, 1, 8),
            ),
            "QQQ": _history(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 8)),
        },
        latest_prices={"SPY": Decimal("103"), "QQQ": Decimal("102")},
        latest_quote_times={
            "SPY": datetime(2026, 1, 8, 21, 0, tzinfo=UTC),
            "QQQ": datetime(2026, 1, 8, 21, 0, tzinfo=UTC),
        },
        market_open=True,
        provider_name="fake_provider",
    )
    strategy = SimpleNamespace(
        id="portfolio-ineligible-coverage-test",
        name="Portfolio ineligible coverage test",
        type="buy_hold_core",
        is_live=False,
        params={},
        allowed_tickers=["SPY", "QQQ"],
        risk_profile=None,
    )
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    async def load_snapshot(*_args: Any) -> MarketSnapshot:
        return snapshot

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(service, "_load_market_snapshot", load_snapshot)
    monkeypatch.setattr(service, "_decision_now", lambda: datetime(2026, 1, 9, 15, 0, tzinfo=UTC))
    monkeypatch.setattr(
        portfolio_service_module,
        "get_portfolio_backtest_strategy",
        lambda _strategy_type: {
            "strategy_class": NeverCalledStrategy,
            "min_history_bars": 0,
        },
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=object(),
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-ineligible-coverage-test",
    )

    assert summary == {"status": "skipped", "reason": "insufficient_session_coverage"}
    state = strategy.params["portfolio_execution"]
    assert state["last_history_eligible"] is False
    assert state["last_history_dropped_sessions"] == 1
    assert state["last_history_dropped_session_ids"] == ["XNYS:2026-01-07"]
    assert state["last_history_reasons"]
    audit = service.db.added[-1]
    assert audit.payload["history_eligible"] is False
    assert audit.payload["history_dropped_sessions"] == 1
    assert audit.payload["history_dropped_session_ids"] == ["XNYS:2026-01-07"]
    assert audit.payload["history_reasons"]


@pytest.mark.asyncio
async def test_runtime_rebalances_on_first_session_of_new_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class AnnualStrategy:
        rebalance_frequency = "annual"
        min_history_bars = 0

        def __init__(self, _params: dict[str, Any]) -> None:
            return None

        def target_weights(
            self, history: dict[str, list[Bar]], *, as_of_index: int
        ) -> dict[str, Decimal]:
            calls.append(as_of_index)
            return {}

    snapshot = MarketSnapshot(
        histories={"SPY": _history(date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31))},
        latest_prices={"SPY": Decimal("102")},
        latest_quote_times={"SPY": datetime(2025, 12, 31, 21, 0, tzinfo=UTC)},
        market_open=True,
        provider_name="fake_provider",
    )
    strategy = SimpleNamespace(
        id="portfolio-period-boundary-test",
        name="Portfolio period boundary test",
        type="buy_hold_core",
        is_live=False,
        params={"portfolio_execution": {"last_rebalance_signal_at": "2025-12-01T00:00:00+00:00"}},
        allowed_tickers=["SPY"],
        risk_profile=None,
    )
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    async def load_snapshot(*_args: Any) -> MarketSnapshot:
        return snapshot

    async def load_instruments(*_args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(service, "_load_market_snapshot", load_snapshot)
    monkeypatch.setattr(service, "_decision_now", lambda: datetime(2026, 1, 2, 15, 0, tzinfo=UTC))
    monkeypatch.setattr(service, "_load_instruments", load_instruments)
    monkeypatch.setattr(
        portfolio_service_module,
        "get_portfolio_backtest_strategy",
        lambda _strategy_type: {
            "strategy_class": AnnualStrategy,
            "min_history_bars": 0,
        },
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=object(),
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=False,
        actor="portfolio-period-boundary-test",
    )

    assert summary["status"] == "rebalanced"
    assert calls == [2]
    assert strategy.params["portfolio_execution"]["last_rebalance_signal_at"].startswith(
        "2026-01-02"
    )


@pytest.mark.asyncio
async def test_runtime_pins_decision_clock_before_snapshot_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    observed_lengths: list[int] = []
    after_fetch = False

    class CapturingStrategy:
        rebalance_frequency = "monthly"
        min_history_bars = 0

        def __init__(self, _params: dict[str, Any]) -> None:
            return None

        def target_weights(
            self, history: dict[str, list[Bar]], *, as_of_index: int
        ) -> dict[str, Decimal]:
            events.append("target_weights")
            observed_lengths.append(len(history["SPY"]))
            assert as_of_index == len(history["SPY"]) - 1
            return {}

    snapshot = MarketSnapshot(
        histories={"SPY": _history(date(2026, 1, 8), date(2026, 1, 9))},
        latest_prices={"SPY": Decimal("101")},
        latest_quote_times={"SPY": datetime(2026, 1, 9, 20, 59, tzinfo=UTC)},
        market_open=True,
        provider_name="fake_provider",
    )
    strategy = SimpleNamespace(
        id="portfolio-clock-test",
        name="Portfolio clock test",
        type="buy_hold_core",
        is_live=False,
        params={},
        allowed_tickers=["SPY"],
        risk_profile=None,
    )
    service = PortfolioExecutionService(_FakeDb())  # type: ignore[arg-type]

    def decision_now() -> datetime:
        events.append("decision_now")
        if after_fetch:
            return datetime(2026, 1, 9, 21, 1, tzinfo=UTC)
        return datetime(2026, 1, 9, 20, 59, tzinfo=UTC)

    async def load_snapshot(*_args: Any) -> MarketSnapshot:
        nonlocal after_fetch
        events.append("load_snapshot")
        after_fetch = True
        return snapshot

    async def load_instruments(*_args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(service, "_decision_now", decision_now)
    monkeypatch.setattr(service, "_load_market_snapshot", load_snapshot)
    monkeypatch.setattr(service, "_load_instruments", load_instruments)
    monkeypatch.setattr(
        portfolio_service_module,
        "get_portfolio_backtest_strategy",
        lambda _strategy_type: {
            "strategy_class": CapturingStrategy,
            "min_history_bars": 0,
        },
    )

    summary = await service.run_strategy_once(
        strategy,
        broker=object(),
        account_value=Decimal("1000"),
        available_cash=Decimal("1000"),
        broker_positions=[],
        force=True,
        actor="portfolio-clock-test",
    )

    assert summary["status"] == "rebalanced"
    assert events[:2] == ["decision_now", "load_snapshot"]
    assert observed_lengths == [1]
