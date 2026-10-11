"""How much earlier-session history may be joined to the current session for indicators."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.backtest import engine as backtest_engine
from app.backtest.engine import Backtester, generate_strategy_signal
from app.market_data.exchange_calendar import calendar_for_venue
from app.market_data.indicator_history import continuous_indicator_bars
from app.services.strategy_runner import StrategyRunner
from app.strategies.closing_momentum import ClosingMomentumStrategy
from app.strategies.indicators import Bar, accepts_indicator_bars, indicator_series
from app.strategies.opening_fade import OpeningFadeStrategy
from app.strategies.orb_production import OpeningRangeBreakoutStrategy
from app.strategies.vwap_reclaim import VWAPReclaimStrategy

CALENDAR = calendar_for_venue("XNYS")
INTERVAL = 5


def _times(day: date, count: int | None = None) -> list[datetime]:
    (session,) = CALENDAR.expected_sessions(day, day)
    opened, closed = CALENDAR.session_open(session), CALENDAR.session_close(session)
    times = []
    cursor = opened
    while cursor + timedelta(minutes=INTERVAL) <= closed:
        times.append(cursor)
        cursor += timedelta(minutes=INTERVAL)
    return times if count is None else times[:count]


def _bars(times: list[datetime], *, start: int = 0) -> list[Bar]:
    # Every bar is distinct, so a misplaced bar cannot go unnoticed.
    return [
        Bar(
            Decimal(100 + start + index),
            Decimal(101 + start + index),
            Decimal(99 + start + index),
            Decimal(100 + start + index),
            Decimal(1000 + start + index),
        )
        for index in range(len(times))
    ]


def _two_sessions(
    previous_day: date, session_day: date, session_count: int = 4
) -> tuple[list[Bar], list[datetime], list[Bar], list[datetime]]:
    previous_times = _times(previous_day)
    session_times = _times(session_day, session_count)
    previous_bars = _bars(previous_times)
    session_bars = _bars(session_times, start=1000)
    return (
        previous_bars + session_bars,
        previous_times + session_times,
        session_bars,
        session_times,
    )


def _build(
    history: list[Bar], history_times: list[datetime], session: list[Bar], times: list[datetime]
) -> list[Bar]:
    return continuous_indicator_bars(
        history, history_times, session, times, interval_minutes=INTERVAL
    )


def test_previous_session_and_current_session_form_one_series() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )

    series = _build(history, history_times, session, session_times)

    assert series == history
    assert len(series) == 78 + 4
    assert series[-4:] == session


@pytest.mark.parametrize(
    ("previous_day", "session_day", "previous_bars"),
    [
        (date(2026, 1, 9), date(2026, 1, 12), 78),  # over a weekend
        (date(2026, 7, 2), date(2026, 7, 6), 78),  # over a holiday and a weekend
        (date(2026, 11, 27), date(2026, 11, 30), 42),  # after an early close
        (date(2026, 3, 6), date(2026, 3, 9), 78),  # across the US clock change
        (date(2026, 12, 31), date(2027, 1, 4), 78),  # across the year end
    ],
    ids=["weekend", "holiday", "early-close", "clock-change", "year-end"],
)
def test_adjacent_sessions_join_across_calendar_boundaries(
    previous_day: date, session_day: date, previous_bars: int
) -> None:
    history, history_times, session, session_times = _two_sessions(previous_day, session_day)

    series = _build(history, history_times, session, session_times)

    assert len(series) == previous_bars + 4
    assert series == history


def test_history_is_cut_at_a_missing_bar() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    del history[40], history_times[40]

    series = _build(history, history_times, session, session_times)

    # Only the bars after the hole are trusted: 37 of the prior session, then today's 4.
    assert series == history[40:]
    assert len(series) == 37 + 4


def test_no_history_when_the_prior_session_does_not_reach_its_close() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    del history[77], history_times[77]  # the prior session's last bar is missing

    assert _build(history, history_times, session, session_times) == []


def test_no_history_when_a_whole_session_is_skipped() -> None:
    older_times = _times(date(2026, 1, 5))
    session_times = _times(date(2026, 1, 7), 4)
    session = _bars(session_times, start=1000)

    assert (
        _build(_bars(older_times) + session, older_times + session_times, session, session_times)
        == []
    )


@pytest.mark.parametrize("problem", ["duplicate", "out-of-order", "extended-hours"])
def test_history_is_cut_at_a_bad_timestamp(problem: str) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    if problem == "duplicate":
        history_times[50] = history_times[49]
    elif problem == "out-of-order":
        history_times[50], history_times[51] = history_times[51], history_times[50]
    else:
        history_times[50] = history_times[50].replace(hour=1)

    series = _build(history, history_times, session, session_times)

    assert 4 < len(series) < len(history)
    assert series == history[-len(series) :]
    assert series[-4:] == session


@pytest.mark.parametrize(
    "tamper",
    ["later-bar", "different-last-bar", "different-last-time", "session-only", "shorter", "empty"],
)
def test_no_history_unless_it_ends_with_exactly_the_session(tamper: str) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    if tamper == "later-bar":
        # A bar from after the latest session bar: look-ahead.
        history.append(_bars([session_times[-1]], start=5000)[0])
        history_times.append(session_times[-1] + timedelta(minutes=INTERVAL))
    elif tamper == "different-last-bar":
        history[-1] = history[-1]._replace(close=Decimal("1"))
    elif tamper == "different-last-time":
        history_times[-1] += timedelta(minutes=INTERVAL)
    elif tamper == "session-only":
        history, history_times = list(session), list(session_times)
    elif tamper == "shorter":
        history, history_times = history[-2:], history_times[-2:]
    else:
        history, history_times = [], []

    assert _build(history, history_times, session, session_times) == []


def test_no_history_when_the_session_itself_has_a_hole() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7), session_count=6
    )
    del history[-3], history_times[-3], session[-3], session_times[-3]

    assert _build(history, history_times, session, session_times) == []


@pytest.mark.parametrize("bad", ["no-session", "session-length", "history-length"])
def test_no_history_for_malformed_input(bad: str) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    if bad == "no-session":
        session, session_times = [], []
    elif bad == "session-length":
        session_times = session_times[:-1]
    else:
        history_times = history_times[:-1]

    assert _build(history, history_times, session, session_times) == []


@pytest.mark.parametrize("which", ["history", "session", "both"])
def test_no_history_when_timestamps_carry_no_timezone(which: str) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    if which in {"history", "both"}:
        history_times = [moment.replace(tzinfo=None) for moment in history_times]
    if which in {"session", "both"}:
        session_times = [moment.replace(tzinfo=None) for moment in session_times]

    assert _build(history, history_times, session, session_times) == []


@pytest.mark.parametrize("year", [1980, 2150])
@pytest.mark.parametrize("position", ["first", "middle"])
def test_timestamp_the_calendar_cannot_place_joins_nothing(year: int, position: str) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    index = 0 if position == "first" else len(history_times) - len(session) - 3
    history_times[index] = history_times[index].replace(year=year)

    joined = _build(history, history_times, session, session_times)

    assert joined == history[index + 1 :]


@pytest.mark.parametrize(
    "moment",
    [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, 14, 30, tzinfo=UTC),
        datetime(2100, 12, 31, 14, 30, tzinfo=UTC),
        datetime(9999, 12, 31, 23, 55, tzinfo=UTC),
    ],
)
def test_timestamp_at_the_edge_of_the_calendar_joins_nothing_and_does_not_raise(
    moment: datetime,
) -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    index = len(history_times) - len(session) - 3
    history_times[index] = moment
    history_times[index - 1] = datetime(2100, 12, 30, 20, 55, tzinfo=UTC)

    assert _build(history, history_times, session, session_times) == history[index + 1 :]
    assert _build(session, [moment] * len(session), session[-1:], [moment]) == []


def test_no_history_when_the_session_bars_do_not_start_at_the_open() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7), 6
    )

    assert _build(history, history_times, session[1:], session_times[1:]) == []
    assert _build(history, history_times, session, session_times) == history


def test_a_different_bar_interval_does_not_join() -> None:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )

    assert (
        continuous_indicator_bars(
            history, history_times, session, session_times, interval_minutes=15
        )
        == []
    )


# ── indicator_series ─────────────────────────────────────────────────────────


def test_indicator_series_uses_history_only_when_it_ends_with_the_session() -> None:
    history, _history_times, session, _session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )

    assert indicator_series(session, history, 15) is history
    assert indicator_series(session, None, 15) is session
    assert indicator_series(session, [], 15) is session
    assert indicator_series(session, session, 15) is session
    assert indicator_series(session, history[:-1], 15) is session
    assert indicator_series(session, [*history, history[0]], 15) is session
    assert indicator_series([], history, 15) == []
    assert indicator_series(session, history, len(session) + 1) is history
    assert indicator_series(session, history, len(session)) is session


# ── Wiring ───────────────────────────────────────────────────────────────────


def _kwargs(engine: Any, indicator_bars: list[Bar] | None) -> dict[str, Any]:
    history, history_times, session, session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    return StrategyRunner._build_signal_kwargs(
        object.__new__(StrategyRunner),
        engine,
        ticker="NVDA",
        bars=session,
        bar_times=session_times,
        history_bars=history,
        history_bar_times=history_times,
        account_value=Decimal("1"),
        available_cash=Decimal("1"),
        current_time_utc="14:50",
        prev_close=Decimal("1"),
        indicator_bars=indicator_bars,
    )


@pytest.mark.parametrize("engine_type", [OpeningRangeBreakoutStrategy, OpeningFadeStrategy])
def test_runner_passes_indicator_history_to_strategies_that_accept_it(engine_type: Any) -> None:
    history, _history_times, session, _session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )

    assert _kwargs(engine_type({}), history)["indicator_bars"] == history
    assert _kwargs(engine_type({}), [])["indicator_bars"] is None
    assert _kwargs(engine_type({}), history)["bars"] == session


@pytest.mark.parametrize("engine_type", [VWAPReclaimStrategy, ClosingMomentumStrategy])
def test_runner_does_not_pass_indicator_history_to_other_strategies(engine_type: Any) -> None:
    history, _history_times, _session, _session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )

    assert "indicator_bars" not in _kwargs(engine_type({}), history)


def test_backtest_passes_indicator_history_to_strategies_that_accept_it() -> None:
    history, _history_times, session, _session_times = _two_sessions(
        date(2026, 1, 6), date(2026, 1, 7)
    )
    seen: dict[str, Any] = {}

    class Recording:
        def generate_signal(
            self,
            ticker: str,
            bars: list[Bar],
            account_value: Decimal,
            available_cash: Decimal,
            current_time_utc: str,
            indicator_bars: list[Bar] | None = None,
        ) -> None:
            seen["bars"], seen["indicator_bars"] = bars, indicator_bars

    for supplied, expected in ((history, history), ([], None)):
        generate_strategy_signal(
            Recording(),
            ticker="NVDA",
            bars=session,
            indicator_bars=supplied,
            account_value=Decimal("1"),
            available_cash=Decimal("1"),
            current_time_utc="14:50",
            prev_close=None,
        )
        assert seen == {"bars": session, "indicator_bars": expected}


class _PlainStrategy:
    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
    ) -> None:
        return None


class _SeriesStrategy:
    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        indicator_bars: list[Bar] | None = None,
    ) -> None:
        self.seen.append((len(bars), len(indicator_bars or [])))


def test_only_strategies_with_an_indicator_bars_parameter_accept_a_series() -> None:
    assert accepts_indicator_bars(OpeningRangeBreakoutStrategy({}))
    assert accepts_indicator_bars(OpeningFadeStrategy({}))
    assert accepts_indicator_bars(_SeriesStrategy())
    assert not accepts_indicator_bars(VWAPReclaimStrategy({}))
    assert not accepts_indicator_bars(ClosingMomentumStrategy({}))
    assert not accepts_indicator_bars(_PlainStrategy())
    assert not accepts_indicator_bars(object())


def test_backtest_builds_the_series_only_for_a_strategy_that_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_day = date(2026, 1, 7)
    bars, times, _session, _session_times = _two_sessions(date(2026, 1, 6), session_day, 12)
    previous_count = len(bars) - 12
    built: list[int] = []
    build = backtest_engine.continuous_indicator_bars

    def counting(*args: Any, **kwargs: Any) -> list[Bar]:
        built.append(1)
        return build(*args, **kwargs)

    monkeypatch.setattr(backtest_engine, "continuous_indicator_bars", counting)

    Backtester(strategy=_PlainStrategy(), ticker="NVDA", start_date=session_day).run(bars, times)
    assert built == []

    recording = _SeriesStrategy()
    Backtester(strategy=recording, ticker="NVDA", start_date=session_day).run(bars, times)
    assert recording.seen
    assert len(built) == len(recording.seen)
    assert all(joined == previous_count + count for count, joined in recording.seen)


def test_backtest_stops_building_the_series_once_the_session_has_twenty_one_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_day = date(2026, 1, 7)
    bars, times, _session, _session_times = _two_sessions(date(2026, 1, 6), session_day, 30)
    previous_count = len(bars) - 30
    built: list[int] = []
    build = backtest_engine.continuous_indicator_bars

    def counting(*args: Any, **kwargs: Any) -> list[Bar]:
        built.append(1)
        return build(*args, **kwargs)

    monkeypatch.setattr(backtest_engine, "continuous_indicator_bars", counting)
    recording = _SeriesStrategy()

    Backtester(strategy=recording, ticker="NVDA", start_date=session_day).run(bars, times)

    early = [(count, joined) for count, joined in recording.seen if count < 21]
    late = [(count, joined) for count, joined in recording.seen if count >= 21]
    assert early
    assert late
    assert all(joined == previous_count + count for count, joined in early)
    assert all(joined == 0 for _count, joined in late)
    assert len(built) == len(early)


def test_timestamps_used_here_are_utc() -> None:
    assert _times(date(2026, 1, 7), 1)[0] == datetime(2026, 1, 7, 14, 30, tzinfo=UTC)
    assert _times(date(2026, 7, 6), 1)[0] == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
