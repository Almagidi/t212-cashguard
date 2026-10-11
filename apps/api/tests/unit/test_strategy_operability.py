"""Can ORB and Opening Fade actually produce an entry with their production parameters?

Every scenario here uses the default parameters or the shipped preset unchanged, five-minute
bars, real session timestamps and the real indicators, and is evaluated at each five-minute
tick as the strategy runner evaluates it. No window, threshold or gate is relaxed.

Findings on issue #258: N-13 (Opening Fade is unreachable), N-14 (ORB could not enter in
the first 105 minutes of a session), N-15 (made-up neutral values for unavailable inputs).
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.market_data.exchange_calendar import calendar_for_venue
from app.market_data.indicator_history import continuous_indicator_bars
from app.services.strategy_presets import PRESET_CONFIGS
from app.strategies import indicators
from app.strategies.indicators import (
    ATR_MIN_BARS,
    INDICATOR_HISTORY_CUTOFF_BARS,
    REGIME_MIN_BARS,
    RELATIVE_VOLUME_MIN_BARS,
    Bar,
    indicator_series,
)
from app.strategies.opening_fade import DEFAULT_FADE_PARAMS, OpeningFadeStrategy
from app.strategies.orb_production import (
    DEFAULT_PARAMS,
    OpeningRangeBreakoutStrategy,
    ORBState,
)

CALENDAR = calendar_for_venue("XNYS")
INTERVAL = 5
PREVIOUS_DAY = date(2026, 1, 6)
SESSION_DAY = date(2026, 1, 7)
ACCOUNT = Decimal("100000")
CASH = Decimal("100000")
ORB_PARAMETER_SETS = {
    "default": {},
    "preset": dict(PRESET_CONFIGS["orb"].strategy_params),
}
FADE_PARAMETER_SETS = {
    "default": {},
    "preset": dict(PRESET_CONFIGS["opening_fade"].strategy_params),
}


def _bar(open_: object, high: object, low: object, close: object, volume: int) -> Bar:
    return Bar(*(Decimal(str(value)) for value in (open_, high, low, close, volume)))


def _session_times(day: date, count: int | None = None) -> list[datetime]:
    (session,) = CALENDAR.expected_sessions(day, day)
    opened, closed = CALENDAR.session_open(session), CALENDAR.session_close(session)
    times = []
    cursor = opened
    while cursor + timedelta(minutes=INTERVAL) <= closed:
        times.append(cursor)
        cursor += timedelta(minutes=INTERVAL)
    return times if count is None else times[:count]


def _clock(tick_bars: int) -> str:
    """The strategy clock the runner passes: the open time (UTC) of the latest bar."""
    opened = datetime.combine(SESSION_DAY, time(14, 30), UTC)
    return (opened + timedelta(minutes=INTERVAL * (tick_bars - 1))).strftime("%H:%M")


def _previous_session(*, end_close: Decimal, bar_range: Decimal) -> list[Bar]:
    """A quiet, gently rising prior session whose last close is exactly `end_close`."""
    times = _session_times(PREVIOUS_DAY)
    drift = Decimal("0.012")
    bars = []
    for index in range(len(times)):
        close = end_close - drift * (len(times) - 1 - index)
        open_ = close - drift
        bars.append(_bar(open_, close + bar_range / 2, open_ - bar_range / 2, close, 100_000))
    return bars


def _breakout_session(count: int) -> list[Bar]:
    """A 15-minute opening range just above 100, then a breakout bar on heavy volume."""
    bars = [
        _bar("100.10", "100.45", "99.95", "100.30", 150_000),
        _bar("100.30", "100.50", "100.05", "100.20", 130_000),
        _bar("100.20", "100.48", "100.00", "100.35", 120_000),
        _bar("100.35", "101.50", "100.30", "101.40", 420_000),
    ]
    price = Decimal("101.40")
    while len(bars) < count:
        bars.append(_bar(price, price + Decimal("0.35"), price - Decimal("0.10"), price, 180_000))
    return bars[:count]


def _orb_context(count: int) -> dict[str, Any]:
    previous = _previous_session(end_close=Decimal("100.00"), bar_range=Decimal("0.40"))
    session = _breakout_session(count)
    session_times = _session_times(SESSION_DAY, count)
    history = previous + session
    history_times = _session_times(PREVIOUS_DAY) + session_times
    return {
        "session": session,
        "prev_close": previous[-1].close,
        "indicator_bars": continuous_indicator_bars(
            history, history_times, session, session_times, interval_minutes=INTERVAL
        ),
    }


def _orb_signal(parameters: dict[str, Any], count: int, *, with_history: bool) -> Any:
    context = _orb_context(count)
    return OpeningRangeBreakoutStrategy(parameters).generate_signal(
        ticker="NVDA",
        bars=context["session"],
        account_value=ACCOUNT,
        available_cash=CASH,
        current_time_utc=_clock(count),
        prev_close=context["prev_close"],
        indicator_bars=context["indicator_bars"] if with_history else None,
    )


# ── ORB (N-14) ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ORB_PARAMETER_SETS))
def test_orb_enters_on_the_first_breakout_bar_after_its_opening_range(name: str) -> None:
    parameters = ORB_PARAMETER_SETS[name]

    signal = _orb_signal(parameters, 4, with_history=True)

    # Bar 4 completes at 14:50 UTC (09:50 ET): a 15-minute range plus one breakout bar.
    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == Decimal("101.40")
    assert signal.stop_price < signal.entry_price < signal.take_profit_price
    # The opening range is today's first 15 minutes, not anything from the earlier session.
    assert signal.params_snapshot["orb_high"] == 100.50
    assert signal.params_snapshot["orb_low"] == 99.95
    # The reported inputs are the ones the gates used.
    context = _orb_context(4)
    series = indicator_series(context["session"], context["indicator_bars"], ATR_MIN_BARS)
    assert signal.rvol == indicators.relative_volume(series, 20)
    assert signal.atr_value == indicators.atr(series, 14)
    assert signal.regime == indicators.market_regime(series)


@pytest.mark.parametrize("name", sorted(ORB_PARAMETER_SETS))
def test_orb_does_not_enter_before_its_opening_range_is_complete(name: str) -> None:
    for count in (1, 2, 3):
        assert _orb_signal(ORB_PARAMETER_SETS[name], count, with_history=True) is None


@pytest.mark.parametrize("name", sorted(ORB_PARAMETER_SETS))
def test_orb_without_validated_history_still_waits_for_twenty_one_session_bars(name: str) -> None:
    # With session bars alone the 20-bar volume average does not exist until bar 21
    # (105 minutes after the open). The same market that signals at bar 4 with history
    # must stay silent, not trade on a made-up relative volume.
    for count in range(1, 21):
        assert _orb_signal(ORB_PARAMETER_SETS[name], count, with_history=False) is None


def test_orb_gate_values_at_the_breakout_bar_are_real_and_within_the_unchanged_limits() -> None:
    context = _orb_context(4)
    series = indicator_series(context["session"], context["indicator_bars"], ATR_MIN_BARS)
    price = context["session"][-1].close

    assert len(series) == 78 + 4
    assert indicators.volume_sma(series[:-1], 20) > 0
    assert indicators.relative_volume(series, 20) >= Decimal(str(DEFAULT_PARAMS["min_rvol"]))
    atr_pct = indicators.atr(series, 14) / price * 100
    assert Decimal(str(DEFAULT_PARAMS["min_atr_pct"])) <= atr_pct <= Decimal("6.0")
    assert indicators.market_regime(series) in {"trending_up", "neutral"}
    assert DEFAULT_PARAMS["min_rvol"] == 1.5
    assert PRESET_CONFIGS["orb"].strategy_params["min_rvol"] == 1.6


def test_orb_quiet_breakout_without_volume_is_still_rejected_with_history() -> None:
    context = _orb_context(4)
    quiet = [*context["session"][:-1], context["session"][-1]._replace(volume=Decimal("110000"))]
    history = [*context["indicator_bars"][:-1], quiet[-1]]

    signal = OpeningRangeBreakoutStrategy({}).generate_signal(
        ticker="NVDA",
        bars=quiet,
        account_value=ACCOUNT,
        available_cash=CASH,
        current_time_utc=_clock(4),
        prev_close=context["prev_close"],
        indicator_bars=history,
    )

    assert indicators.relative_volume(history, 20) < Decimal("1.5")
    assert signal is None


def test_orb_ignores_history_that_does_not_end_with_the_session_bars() -> None:
    context = _orb_context(4)
    tampered = [*context["indicator_bars"][:-1], context["session"][-1]._replace(volume=Decimal(1))]
    shifted = context["indicator_bars"][:-1]
    later = [*context["indicator_bars"], _bar(102, 103, 101, 102, 900_000)]

    for bad_history in (tampered, shifted, later, context["session"], []):
        assert indicator_series(context["session"], bad_history, ATR_MIN_BARS) == context["session"]
        signal = OpeningRangeBreakoutStrategy({}).generate_signal(
            ticker="NVDA",
            bars=context["session"],
            account_value=ACCOUNT,
            available_cash=CASH,
            current_time_utc=_clock(4),
            prev_close=context["prev_close"],
            indicator_bars=bad_history,
        )
        assert signal is None


# ── Opening Fade (N-13) ──────────────────────────────────────────────────────

FADE_PREV_CLOSE = Decimal("100.00")
FADE_GAP_PCT = Decimal("4.5")
FADE_SIGNAL_TICK = 8  # the eighth bar opens 15:05 UTC (10:05 ET), inside both windows


def _fade_session(
    count: int,
    *,
    gap_pct: Decimal = FADE_GAP_PCT,
    amplitude: Decimal = Decimal("1.6"),
    lift: Decimal = Decimal("0.10"),
    last_volume: int = 300_000,
) -> list[Bar]:
    """A gap-down open that holds just above its open in wide two-way bars.

    `amplitude` is how far each bar reaches above the open and `lift` how far above the
    open the session holds; the last bar carries `last_volume`.
    """
    session_open = FADE_PREV_CLOSE * (1 - gap_pct / 100)
    bars = [
        _bar(
            session_open,
            session_open + amplitude,
            session_open - amplitude / 4,
            session_open + lift,
            150_000,
        )
    ]
    for index in range(1, count):
        odd = index % 2 == 1
        close = session_open + (lift if odd else lift + amplitude / 2)
        open_ = session_open + (lift + amplitude / 2 if odd else lift)
        bars.append(
            _bar(
                open_, session_open + lift + amplitude, session_open - amplitude / 8, close, 100_000
            )
        )
    bars[-1] = bars[-1]._replace(volume=Decimal(last_volume))
    return bars


def _fade_context(count: int, **session_options: Any) -> dict[str, Any]:
    previous = _previous_session(end_close=FADE_PREV_CLOSE, bar_range=Decimal("0.40"))
    session = _fade_session(count, **session_options)
    session_times = _session_times(SESSION_DAY, count)
    return {
        "session": session,
        "indicator_bars": continuous_indicator_bars(
            previous + session,
            _session_times(PREVIOUS_DAY) + session_times,
            session,
            session_times,
            interval_minutes=INTERVAL,
        ),
    }


def _fade_signal(
    parameters: dict[str, Any], count: int, *, with_history: bool, **session_options: Any
) -> Any:
    context = _fade_context(count, **session_options)
    return OpeningFadeStrategy(params=parameters).generate_signal(
        "AAPL",
        context["session"],
        ACCOUNT,
        CASH,
        _clock(count),
        prev_close=FADE_PREV_CLOSE,
        session_open=context["session"][0].open,
        indicator_bars=context["indicator_bars"] if with_history else None,
    )


def _ticks_in_fade_window(strategy: OpeningFadeStrategy) -> list[int]:
    return [count for count in range(1, 40) if strategy._time_in_fade_window(_clock(count))]


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_window_holds_fewer_session_bars_than_its_indicators_need(name: str) -> None:
    strategy = OpeningFadeStrategy(params=FADE_PARAMETER_SETS[name])
    ticks = _ticks_in_fade_window(strategy)
    long_session = _fade_session(40)
    first_atr = next(k for k in range(1, 41) if indicators.atr(long_session[:k], 14) > 0)
    first_rvol = next(
        k for k in range(1, 41) if indicators.volume_sma(long_session[: k - 1], 20) > 0
    )
    first_chop = next(
        k for k in range(1, 41) if indicators.choppiness_index(long_session[:k], 14) is not None
    )

    assert max(ticks) == {"default": 10, "preset": 9}[name]
    assert (first_atr, first_rvol, first_chop) == (15, 21, 15)
    assert max(ticks) < min(first_atr, first_rvol, first_chop)


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_gives_no_signal_at_any_tick_with_session_bars_alone(name: str) -> None:
    """N-13 as the runner called the strategy before: session bars only."""
    for count in range(1, 40):
        assert _fade_signal(FADE_PARAMETER_SETS[name], count, with_history=False) is None


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_enters_inside_its_window_when_given_validated_history(name: str) -> None:
    parameters = {**DEFAULT_FADE_PARAMS, **FADE_PARAMETER_SETS[name]}
    strategy = OpeningFadeStrategy(params=FADE_PARAMETER_SETS[name])
    context = _fade_context(FADE_SIGNAL_TICK)
    series = indicator_series(context["session"], context["indicator_bars"], ATR_MIN_BARS)

    signal = _fade_signal(FADE_PARAMETER_SETS[name], FADE_SIGNAL_TICK, with_history=True)

    assert FADE_SIGNAL_TICK in _ticks_in_fade_window(strategy)
    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == context["session"][-1].close
    assert signal.take_profit_price == FADE_PREV_CLOSE
    assert signal.stop_price < min(bar.low for bar in context["session"])
    assert signal.reward_risk_ratio >= Decimal(str(parameters["reward_risk_ratio_min"]))
    # Every gate was met with the unchanged thresholds.
    assert indicators.relative_volume(series, 20) >= Decimal(str(parameters["min_rvol"]))
    chop = indicators.choppiness_index(series, 14)
    assert chop is not None
    assert chop >= Decimal(str(parameters["chop_threshold"]))


def test_opening_fade_stop_uses_todays_low_not_a_lower_low_in_the_history() -> None:
    previous = _previous_session(end_close=FADE_PREV_CLOSE, bar_range=Decimal("0.40"))
    previous[5] = previous[5]._replace(low=Decimal("90.00"))  # a spike low early yesterday
    session = _fade_session(FADE_SIGNAL_TICK)
    session_times = _session_times(SESSION_DAY, FADE_SIGNAL_TICK)
    history = continuous_indicator_bars(
        previous + session,
        _session_times(PREVIOUS_DAY) + session_times,
        session,
        session_times,
        interval_minutes=INTERVAL,
    )

    signal = OpeningFadeStrategy(params={}).generate_signal(
        "AAPL",
        session,
        ACCOUNT,
        CASH,
        _clock(FADE_SIGNAL_TICK),
        prev_close=FADE_PREV_CLOSE,
        session_open=session[0].open,
        indicator_bars=history,
    )

    session_low = min(bar.low for bar in session)
    assert min(bar.low for bar in history) == Decimal("90.00") < session_low
    assert signal is not None
    assert signal.stop_price == session_low - signal.atr_value * Decimal("1.5")
    assert signal.stop_price > Decimal("90.00")


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_rejects_a_one_way_gap_even_with_history(name: str) -> None:
    # Tight bars after the gap: a trending open. The regime gate must reject it.
    parameters = {**DEFAULT_FADE_PARAMS, **FADE_PARAMETER_SETS[name]}
    context = _fade_context(FADE_SIGNAL_TICK, amplitude=Decimal("0.3"))
    chop = indicators.choppiness_index(
        indicator_series(context["session"], context["indicator_bars"], ATR_MIN_BARS), 14
    )

    assert chop is not None
    assert chop < Decimal(str(parameters["chop_threshold"]))
    for count in _ticks_in_fade_window(OpeningFadeStrategy(params=FADE_PARAMETER_SETS[name])):
        assert (
            _fade_signal(
                FADE_PARAMETER_SETS[name], count, with_history=True, amplitude=Decimal("0.3")
            )
            is None
        )


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_rejects_a_reversal_bar_without_volume(name: str) -> None:
    assert (
        _fade_signal(
            FADE_PARAMETER_SETS[name], FADE_SIGNAL_TICK, with_history=True, last_volume=120_000
        )
        is None
    )


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
@pytest.mark.parametrize("gap_pct", ["1.0", "7.0"])
def test_opening_fade_rejects_gaps_outside_its_limits(name: str, gap_pct: str) -> None:
    assert (
        _fade_signal(
            FADE_PARAMETER_SETS[name], FADE_SIGNAL_TICK, with_history=True, gap_pct=Decimal(gap_pct)
        )
        is None
    )


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
@pytest.mark.parametrize("count", [6, 8])
def test_opening_fade_enters_on_a_moderate_gap_with_validated_history(
    name: str, count: int
) -> None:
    """A 2% gap in 0.6-point bars also qualifies; the main scenario is the wider one.

    Whether a session qualifies is sensitive to both: with the same history a 2% gap in
    1-point bars, or a 3% gap in 0.6-point bars, gives no signal at any tick.
    """
    moderate = {"gap_pct": Decimal("2.0"), "amplitude": Decimal("0.6")}

    signal = _fade_signal(FADE_PARAMETER_SETS[name], count, with_history=True, **moderate)

    assert signal is not None
    assert (signal.side, signal.signal_type) == ("buy", "entry")
    assert _fade_signal(FADE_PARAMETER_SETS[name], count, with_history=False, **moderate) is None
    for options in (
        {"gap_pct": Decimal("2.0"), "amplitude": Decimal("1.0")},
        {"gap_pct": Decimal("3.0"), "amplitude": Decimal("0.6")},
    ):
        for tick in range(5, 11):
            assert (
                _fade_signal(FADE_PARAMETER_SETS[name], tick, with_history=True, **options) is None
            )


@pytest.mark.parametrize("name", sorted(FADE_PARAMETER_SETS))
def test_opening_fade_never_signals_after_its_window_closes(name: str) -> None:
    strategy = OpeningFadeStrategy(params=FADE_PARAMETER_SETS[name])
    last_tick = max(_ticks_in_fade_window(strategy))

    for count in range(last_tick + 1, 40):
        assert _fade_signal(FADE_PARAMETER_SETS[name], count, with_history=True) is None


def test_opening_fade_ignores_history_that_does_not_end_with_the_session_bars() -> None:
    context = _fade_context(FADE_SIGNAL_TICK)
    session = context["session"]
    tampered = [*context["indicator_bars"][:-1], session[-1]._replace(volume=Decimal(1))]
    later = [*context["indicator_bars"], _bar(96, 97, 95, 96, 900_000)]

    for bad_history in (tampered, context["indicator_bars"][:-1], later, session, []):
        signal = OpeningFadeStrategy(params={}).generate_signal(
            "AAPL",
            session,
            ACCOUNT,
            CASH,
            _clock(FADE_SIGNAL_TICK),
            prev_close=FADE_PREV_CLOSE,
            session_open=session[0].open,
            indicator_bars=bad_history,
        )
        assert signal is None


# ── Managing a position before the session is long enough for an ATR ─────────

ENTRY, STOP, TARGET_1R, TARGET_2R = (Decimal(v) for v in ("101.40", "99.90", "102.90", "104.40"))


def _held_position() -> ORBState:
    return ORBState(
        ticker="NVDA",
        strategy_id="strategy",
        side="buy",
        entry_price=ENTRY,
        quantity=Decimal("10"),
        remaining_quantity=Decimal("10"),
        initial_stop=STOP,
        current_stop=STOP,
        take_profit_1r=TARGET_1R,
        take_profit_2r=TARGET_2R,
    )


def _exit_check(count: int, price: Decimal) -> Any:
    session = _breakout_session(count)
    return OpeningRangeBreakoutStrategy({}).check_exit_conditions(
        "NVDA", _held_position(), price, session
    )


@pytest.mark.parametrize("count", range(4, 15))
@pytest.mark.parametrize("price", ["99.91", "101.40", "101.50", "102.89"])
def test_position_between_its_stop_and_target_is_held_while_no_atr_is_available(
    count: int, price: str
) -> None:
    assert indicators.atr(_breakout_session(count), 14) == 0

    assert _exit_check(count, Decimal(price)) is None


@pytest.mark.parametrize("count", [4, 9, 14])
@pytest.mark.parametrize("price", ["99.90", "98.00"])
def test_entry_stop_is_still_enforced_while_no_atr_is_available(count: int, price: str) -> None:
    exit_signal = _exit_check(count, Decimal(price))

    assert (exit_signal.side, exit_signal.signal_type) == ("sell", "stop")
    assert exit_signal.stop_price == STOP
    assert exit_signal.suggested_quantity == Decimal("-10")


@pytest.mark.parametrize("count", [4, 9, 14])
def test_targets_are_still_taken_while_no_atr_is_available(count: int) -> None:
    assert _exit_check(count, TARGET_1R).signal_type == "partial_exit"
    assert _exit_check(count, TARGET_2R).signal_type == "take_profit"


def test_stop_is_never_placed_at_the_current_price_by_a_missing_atr() -> None:
    """A stop exit must come from the stored stop, whatever the price above it."""
    for count in range(4, 15):
        for cents in range(9991, 10289, 7):
            assert _exit_check(count, Decimal(cents) / 100) is None


# ── Entries the session can already produce are not changed by the history ───

ORB_LAST_TICK = 30  # the latest bar ORB may enter on opens 16:55 UTC (11:55 ET)


def _late_breakout_session(count: int) -> list[Bar]:
    """The same opening range, a dip, a steady climb back, and a breakout on the last bar."""
    dip, top, wiggle = Decimal("99.30"), Decimal("100.40"), Decimal("0.10")
    bars = _breakout_session(3)
    bars.append(_bar("100.35", "100.40", dip - Decimal("0.05"), dip, 110_000))
    climb = count - 5
    for step in range(1, climb + 1):
        open_ = dip + (top - dip) * (step - 1) / climb
        close = dip + (top - dip) * step / climb
        bars.append(_bar(open_, close + wiggle, open_ - wiggle, close, 100_000))
    bars.append(_bar(top, "101.50", top - Decimal("0.05"), "101.40", 420_000))
    return bars


def _with_history(session: list[Bar]) -> list[Bar]:
    previous = _previous_session(end_close=Decimal("100.00"), bar_range=Decimal("0.40"))
    session_times = _session_times(SESSION_DAY, len(session))
    return continuous_indicator_bars(
        previous + session,
        _session_times(PREVIOUS_DAY) + session_times,
        session,
        session_times,
        interval_minutes=INTERVAL,
    )


@pytest.mark.parametrize("name", sorted(ORB_PARAMETER_SETS))
@pytest.mark.parametrize("count", range(RELATIVE_VOLUME_MIN_BARS, ORB_LAST_TICK + 1))
def test_orb_entry_from_bar_twenty_one_is_identical_with_and_without_history(
    name: str, count: int
) -> None:
    session = _late_breakout_session(count)
    history = _with_history(session)
    strategy = OpeningRangeBreakoutStrategy(ORB_PARAMETER_SETS[name])
    arguments = ("NVDA", session, ACCOUNT, CASH, _clock(count))

    alone = strategy.generate_signal(*arguments, prev_close=Decimal("100.00"))
    joined = strategy.generate_signal(
        *arguments, prev_close=Decimal("100.00"), indicator_bars=history
    )

    assert len(history) > count
    assert alone is not None
    assert alone.signal_type == "entry"
    assert joined == alone
    assert alone.atr_value == indicators.atr(session, 14)
    assert indicators.atr(history, 14) != indicators.atr(session, 14)


def _random_session(seed: int, count: int, *, drift: int = 0) -> list[Bar]:
    """A seeded random walk from 100.00; ``drift`` (in cents per bar) tilts it upwards."""
    generator = random.Random(seed)
    price = Decimal("100.00")
    session = []
    for _ in range(count):
        close = price + Decimal(generator.randint(-45, 50) + drift) / 100
        high = max(price, close) + Decimal(generator.randint(0, 30)) / 100
        low = min(price, close) - Decimal(generator.randint(0, 30)) / 100
        session.append(_bar(price, high, low, close, generator.randint(60_000, 400_000)))
        price = close
    return session


def _orb_outcomes(parameters: dict[str, Any], session: list[Bar], history: list[Bar] | None) -> Any:
    strategy = OpeningRangeBreakoutStrategy(parameters)
    count = len(session)
    filter_arguments = (session, _clock(count), Decimal("100.00"), history)
    return (
        strategy._check_filters(*filter_arguments),
        strategy._check_filters_short(*filter_arguments),
        strategy.generate_signal(
            "NVDA",
            session,
            ACCOUNT,
            CASH,
            _clock(count),
            prev_close=Decimal("100.00"),
            indicator_bars=history,
        ),
    )


@pytest.mark.parametrize("seed", range(40))
def test_orb_filters_from_bar_twenty_one_ignore_the_history_on_any_session(seed: int) -> None:
    count = random.Random(seed).randint(INDICATOR_HISTORY_CUTOFF_BARS, 78)
    session = _random_session(seed, count)
    history = _with_history(session)
    assert len(history) > count

    for parameters in ({}, {"allow_short": True}):
        assert _orb_outcomes(parameters, session, history) == (
            _orb_outcomes(parameters, session, None)
        )


# The volume gate hides the later gates on most random sessions, so these parity checks
# switch it off. That is a property of the comparison, not a claim about a tradeable setup.
PARITY_PARAMETER_SETS = (
    {"min_rvol": 0},
    {"min_rvol": 0, "allow_short": True},
    {"min_rvol": 0, "trend_ema_slow": 30},
    {"min_rvol": 0, "trend_ema_slow": 50, "allow_short": True},
)


def test_orb_from_bar_twenty_one_is_identical_with_history_for_any_parameters() -> None:
    reached_trend = entered = 0
    labels: set[str] = set()
    for seed in range(120):
        count = INDICATOR_HISTORY_CUTOFF_BARS + seed % 10  # bars 21-30: inside the window
        session = _random_session(seed, count, drift=seed % 3 * 4)
        history = _with_history(session)
        assert len(history) > count
        for parameters in PARITY_PARAMETER_SETS:
            alone = _orb_outcomes(parameters, session, None)
            assert _orb_outcomes(parameters, session, history) == alone
            long_filter, _short_filter, signal = alone
            reached_trend += long_filter[0] or long_filter[1] == "No uptrend confirmation"
            entered += signal is not None
            if signal is not None:
                labels.add(signal.regime)

    assert reached_trend >= 100
    assert entered >= 40
    assert {"trending_up", "trending_down"} <= labels


@pytest.mark.parametrize("seed", range(60))
def test_below_bar_twenty_one_history_can_only_remove_entries_never_add_them(seed: int) -> None:
    """With a volume gate of 1.0 or less a session-only entry exists from bar 15, because
    relative volume is then a made-up 1.00. With history a measured value replaces it."""
    count = ATR_MIN_BARS + seed % 6  # bars 15-20
    session = _random_session(seed, count, drift=seed % 3 * 4)
    history = _with_history(session)

    for parameters in ({"min_rvol": 1.0}, {"min_rvol": 0}):
        strategy = OpeningRangeBreakoutStrategy(parameters)
        arguments = ("NVDA", session, ACCOUNT, CASH, _clock(count))
        alone = strategy.generate_signal(*arguments, prev_close=Decimal("100.00"))
        joined = strategy.generate_signal(
            *arguments, prev_close=Decimal("100.00"), indicator_bars=history
        )
        if joined is not None:
            assert alone is not None
            assert (joined.entry_price, joined.stop_price, joined.suggested_quantity) == (
                alone.entry_price,
                alone.stop_price,
                alone.suggested_quantity,
            )


@pytest.mark.parametrize(
    "minimum",
    [ATR_MIN_BARS, REGIME_MIN_BARS, RELATIVE_VOLUME_MIN_BARS, DEFAULT_PARAMS["trend_ema_slow"]],
)
def test_history_is_used_for_an_indicator_only_while_the_session_cannot_produce_it(
    minimum: int,
) -> None:
    short_session = _late_breakout_session(minimum - 1)
    long_enough = _late_breakout_session(minimum)

    assert indicator_series(short_session, _with_history(short_session), minimum) == (
        _with_history(short_session)
    )
    assert indicator_series(long_enough, _with_history(long_enough), minimum) is long_enough


@pytest.mark.parametrize("minimum", [22, 30, 50, 200])
def test_history_is_never_used_from_bar_twenty_one_whatever_minimum_is_asked(
    minimum: int,
) -> None:
    before = _late_breakout_session(INDICATOR_HISTORY_CUTOFF_BARS - 1)
    at_cutoff = _late_breakout_session(INDICATOR_HISTORY_CUTOFF_BARS)

    assert indicator_series(before, _with_history(before), minimum) == _with_history(before)
    assert indicator_series(at_cutoff, _with_history(at_cutoff), minimum) is at_cutoff


@pytest.mark.parametrize("name", sorted(ORB_PARAMETER_SETS))
@pytest.mark.parametrize("count", range(ATR_MIN_BARS, INDICATOR_HISTORY_CUTOFF_BARS))
def test_orb_between_bars_fifteen_and_twenty_takes_each_indicator_from_the_right_bars(
    name: str, count: int
) -> None:
    """ATR and regime from the session, which can produce them; relative volume from history."""
    session = _late_breakout_session(count)
    history = _with_history(session)
    strategy = OpeningRangeBreakoutStrategy(ORB_PARAMETER_SETS[name])
    arguments = ("NVDA", session, ACCOUNT, CASH, _clock(count))

    signal = strategy.generate_signal(
        *arguments, prev_close=Decimal("100.00"), indicator_bars=history
    )

    assert strategy.generate_signal(*arguments, prev_close=Decimal("100.00")) is None
    assert signal is not None
    assert signal.atr_value == indicators.atr(session, 14) != indicators.atr(history, 14)
    assert signal.regime == indicators.market_regime(session) == "neutral"
    assert indicators.market_regime(history) == "trending_up"
    assert signal.rvol == indicators.relative_volume(history, 20)
    assert indicators.relative_volume(session, 20) == 1  # the made-up value, not used
    filters_pass, _reason = strategy._check_filters(
        session, _clock(count), Decimal("100.00"), history
    )
    assert filters_pass


def test_the_minimums_are_the_first_bar_counts_at_which_each_indicator_exists() -> None:
    session = _late_breakout_session(30)

    assert indicators.atr(session[: ATR_MIN_BARS - 1], 14) == 0
    assert indicators.atr(session[:ATR_MIN_BARS], 14) > 0
    assert indicators.choppiness_index(session[: REGIME_MIN_BARS - 1], 14) is None
    assert indicators.choppiness_index(session[:REGIME_MIN_BARS], 14) is not None
    assert indicators.market_regime(session[: REGIME_MIN_BARS - 1]) == "unknown"
    assert indicators.market_regime(session[:REGIME_MIN_BARS]) != "unknown"
    assert indicators.volume_sma(session[: RELATIVE_VOLUME_MIN_BARS - 2], 20) == 0
    assert indicators.volume_sma(session[: RELATIVE_VOLUME_MIN_BARS - 1], 20) > 0


def test_joined_choppiness_index_falls_as_the_overnight_gap_grows() -> None:
    """The index window spans the gap, so Opening Fade's regime gate partly measures it."""
    values = [
        indicators.choppiness_index(
            _fade_context(FADE_SIGNAL_TICK, gap_pct=Decimal(gap))["indicator_bars"], 14
        )
        for gap in ("0", "2", "3", "4.5", "6")
    ]

    assert all(value is not None for value in values)
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]


def test_joined_atr_includes_the_overnight_gap() -> None:
    """Stated plainly because it widens stops and eases the ATR floor on a gap day."""
    gapped = _fade_context(FADE_SIGNAL_TICK)
    level = _fade_context(FADE_SIGNAL_TICK, gap_pct=Decimal("0"))

    assert indicators.atr(gapped["indicator_bars"], 14) > indicators.atr(
        level["indicator_bars"], 14
    )


# ── Exit check: short side and a session with no range ───────────────────────


def _held_short() -> ORBState:
    return ORBState(
        ticker="NVDA",
        strategy_id="strategy",
        side="sell",
        entry_price=ENTRY,
        quantity=Decimal("10"),
        remaining_quantity=Decimal("10"),
        initial_stop=Decimal("102.90"),
        current_stop=Decimal("102.90"),
        take_profit_1r=Decimal("99.90"),
        take_profit_2r=Decimal("98.40"),
    )


@pytest.mark.parametrize("count", [4, 9, 14])
def test_short_position_is_managed_by_its_stop_and_targets_while_no_atr_is_available(
    count: int,
) -> None:
    strategy = OpeningRangeBreakoutStrategy({})
    session = _breakout_session(count)

    def check(price: str) -> Any:
        return strategy.check_exit_conditions("NVDA", _held_short(), Decimal(price), session)

    assert check("101.40") is None
    assert check("102.89") is None
    assert check("99.91") is None
    stop = check("102.90")
    assert (stop.side, stop.signal_type, stop.stop_price) == ("buy", "stop", Decimal("102.90"))
    assert check("99.90").signal_type == "partial_exit"
    assert check("98.40").signal_type == "take_profit"


def test_position_in_a_session_with_no_range_is_held_at_any_bar_count() -> None:
    """Flat bars give an ATR of exactly zero even with fifteen or more of them."""
    flat = [_bar("101.40", "101.40", "101.40", "101.40", 100_000)] * 30
    strategy = OpeningRangeBreakoutStrategy({})

    for count in (15, 21, 30):
        assert indicators.atr(flat[:count], 14) == 0
        assert strategy.check_exit_conditions("NVDA", _held_position(), ENTRY, flat[:count]) is None
        stopped = strategy.check_exit_conditions("NVDA", _held_position(), STOP, flat[:count])
        assert stopped.signal_type == "stop"
