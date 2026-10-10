"""Choppiness Index (E. W. Dreiss): reference values, properties and unavailable cases.

    TR_i   = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)
    CHOP_t = 100 * log10(sum(TR over the last n bars) / (max H - min L over them)) / log10(n)

A value needs n true ranges, so n + 1 bars. When it cannot be computed the function
returns None; it never substitutes a neutral number.
"""

from __future__ import annotations

import math
import random
from decimal import ROUND_HALF_EVEN, ROUND_UP, Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from app.strategies.indicators import Bar, choppiness_index

TOLERANCE = Decimal("0.01")


def _bar(high: object, low: object, close: object, open_: object | None = None) -> Bar:
    return Bar(
        open=Decimal(str(close if open_ is None else open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
    )


def _reference(bars: list[Bar], period: int) -> float:
    """Independent float implementation of the published formula."""
    window = bars[-period:]
    total_true_range = 0.0
    for index in range(len(bars) - period, len(bars)):
        bar, previous_close = bars[index], float(bars[index - 1].close)
        total_true_range += max(
            float(bar.high) - float(bar.low),
            abs(float(bar.high) - previous_close),
            abs(float(bar.low) - previous_close),
        )
    price_range = max(float(b.high) for b in window) - min(float(b.low) for b in window)
    return 100 * math.log10(total_true_range / price_range) / math.log10(period)


def _steady_trend(count: int) -> list[Bar]:
    # Each bar opens at the previous close and extends the range by exactly its own height.
    return [_bar(high=101 + i, low=100 + i, close=101 + i, open_=100 + i) for i in range(count)]


def _flat_back_and_forth(count: int) -> list[Bar]:
    # Every bar spans the same [100, 101]; the range never grows.
    return [_bar(high=101, low=100, close=101 if i % 2 == 0 else 100) for i in range(count)]


def _random_walk(seed: int, count: int) -> list[Bar]:
    rng = random.Random(seed)
    price, bars = 100.0, []
    for _ in range(count):
        open_ = price
        close = price + rng.gauss(0, 1)
        high = max(open_, close) + abs(rng.gauss(0, 0.3))
        low = min(open_, close) - abs(rng.gauss(0, 0.3))
        bars.append(_bar(round(high, 4), round(low, 4), round(close, 4), round(open_, 4)))
        price = close
    return bars


# ── Hand-computed reference values ───────────────────────────────────────────


def test_steady_trend_is_zero() -> None:
    # 14 true ranges of 1 sum to 14; the 14 bars span 14; log10(14 / 14) = 0.
    assert choppiness_index(_steady_trend(30), period=14) == Decimal("0.00")


def test_flat_back_and_forth_is_one_hundred() -> None:
    # 14 true ranges of 1 sum to 14; the range is 1; log10(14) / log10(14) = 1.
    assert choppiness_index(_flat_back_and_forth(30), period=14) == Decimal("100.00")


def test_worked_four_bar_example_is_fifty() -> None:
    # Previous close 10. True ranges 2, 2, 2, 2 sum to 8; highs reach 13, lows 9, range 4.
    # 100 * log10(8 / 4) / log10(4) = 100 * log10(2) / (2 * log10(2)) = 50.
    bars = [
        _bar(high=10, low=10, close=10),
        _bar(high=12, low=10, close=11),
        _bar(high=13, low=11, close=12),
        _bar(high=12, low=10, close=10),
        _bar(high=11, low=9, close=10),
    ]

    assert choppiness_index(bars, period=4) == Decimal("50.00")


def test_only_the_last_period_bars_and_the_close_before_them_matter() -> None:
    noisy_history = _random_walk(seed=3, count=40)
    tail = _steady_trend(15)

    assert choppiness_index(noisy_history + tail, period=14) == choppiness_index(tail, period=14)


def test_gap_into_the_window_can_exceed_one_hundred() -> None:
    # Previous close 50, then four bars inside [100, 101] closing at 100.5.
    # True ranges 51, 1, 1, 1 sum to 54; range 1; 100 * log10(54) / log10(4) = 287.74.
    bars = [_bar(high=50, low=50, close=50)] + [_bar(high=101, low=100, close=100.5)] * 4
    expected = Decimal(str(round(100 * math.log10(54) / math.log10(4), 2)))

    value = choppiness_index(bars, period=4)

    assert value == expected == Decimal("287.74")


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("period", [2, 5, 14, 30])
def test_matches_an_independent_float_implementation(seed: int, period: int) -> None:
    bars = _random_walk(seed, 120)

    value = choppiness_index(bars, period=period)

    assert value is not None
    assert abs(value - Decimal(str(_reference(bars, period)))) <= TOLERANCE


def _exact_reference(bars: list[Bar], period: int) -> Decimal:
    """Independent Decimal implementation at 60 digits, rounded half-even to two places."""
    with localcontext() as context:
        context.prec = 60
        window = bars[-period:]
        total = Decimal(0)
        for index in range(len(bars) - period, len(bars)):
            bar, previous_close = bars[index], bars[index - 1].close
            total += max(
                bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)
            )
        price_range = max(b.high for b in window) - min(b.low for b in window)
        value = Decimal(100) * (total / price_range).log10() / Decimal(period).log10()
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


@pytest.mark.parametrize("seed", range(40))
def test_matches_a_high_precision_decimal_reference_exactly(seed: int) -> None:
    bars = _random_walk(seed, 80)
    period = 2 + seed % 30

    assert choppiness_index(bars, period=period) == _exact_reference(bars, period)


def test_result_does_not_depend_on_the_ambient_decimal_context() -> None:
    bars = _random_walk(seed=2, count=60)
    expected = choppiness_index(bars, period=14)

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP

        assert choppiness_index(bars, period=14) == expected


def test_result_is_rounded_half_even_to_two_places() -> None:
    value = choppiness_index(_random_walk(seed=1, count=60), period=14)

    assert value is not None
    assert value == value.quantize(Decimal("0.01"))


# ── Unavailable, never a made-up neutral value ───────────────────────────────


@pytest.mark.parametrize("count", [0, 1, 5, 14])
def test_fewer_than_period_plus_one_bars_is_unavailable(count: int) -> None:
    assert choppiness_index(_steady_trend(count), period=14) is None


def test_exactly_period_plus_one_bars_is_enough() -> None:
    assert choppiness_index(_steady_trend(15), period=14) == Decimal("0.00")


@pytest.mark.parametrize("period", [1, 0, -3])
def test_period_below_two_is_unavailable(period: int) -> None:
    assert choppiness_index(_steady_trend(30), period=period) is None


def test_zero_range_is_unavailable() -> None:
    bars = [_bar(high=100, low=100, close=100) for _ in range(30)]

    assert choppiness_index(bars, period=14) is None


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["high", "low", "close"])
@pytest.mark.parametrize("position", [-1, -8, -14])
def test_non_finite_price_in_the_window_is_unavailable(bad: str, field: str, position: int) -> None:
    bars = _random_walk(seed=5, count=40)
    bars[position] = bars[position]._replace(**{field: Decimal(bad)})

    assert choppiness_index(bars, period=14) is None


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_close_just_before_the_window_is_unavailable(bad: str) -> None:
    # The first true range is measured from that close.
    bars = _random_walk(seed=5, count=40)
    bars[-15] = bars[-15]._replace(close=Decimal(bad))

    assert choppiness_index(bars, period=14) is None


def test_high_and_low_of_the_bar_before_the_window_do_not_matter() -> None:
    bars = _random_walk(seed=5, count=40)
    clean = choppiness_index(bars, period=14)
    bars[-15] = bars[-15]._replace(high=Decimal("NaN"), low=Decimal("NaN"))

    assert choppiness_index(bars, period=14) == clean


def test_non_finite_price_before_the_window_does_not_matter() -> None:
    bars = _random_walk(seed=5, count=40)
    clean = choppiness_index(bars, period=14)
    bars[-16] = bars[-16]._replace(high=Decimal("NaN"), low=Decimal("NaN"), close=Decimal("NaN"))

    assert choppiness_index(bars, period=14) == clean


def test_bar_with_high_below_low_is_unavailable() -> None:
    bars = _random_walk(seed=6, count=40)
    bars[-3] = bars[-3]._replace(high=bars[-3].low - 1)

    assert choppiness_index(bars, period=14) is None


def test_bar_closing_outside_its_own_range_is_unavailable_not_negative() -> None:
    # Prior close 10; the first bar spans 10-11 but "closes" at 100. Scoring it would give
    # true ranges of 1 and 1 against a range of 91: 100 * log10(2 / 91) / log10(2) = -550.78,
    # which every consumer would read as a strong trend.
    bars = [
        _bar(high=10, low=10, close=10),
        _bar(high=11, low=10, close=100),
        _bar(high=101, low=100, close=100),
    ]

    assert choppiness_index(bars, period=2) is None


@pytest.mark.parametrize("position", [-1, -7, -14])
@pytest.mark.parametrize("side", ["above", "below"])
def test_close_outside_the_bar_range_anywhere_in_the_window_is_unavailable(
    position: int, side: str
) -> None:
    bars = _random_walk(seed=9, count=40)
    bar = bars[position]
    bars[position] = bar._replace(close=bar.high + 1 if side == "above" else bar.low - 1)

    assert choppiness_index(bars, period=14) is None


def test_unavailable_is_never_reported_as_fifty() -> None:
    for bars, period in (
        (_steady_trend(5), 14),
        ([_bar(high=100, low=100, close=100)] * 30, 14),
        (_steady_trend(30), 1),
    ):
        assert choppiness_index(bars, period=period) is None


# ── Properties ───────────────────────────────────────────────────────────────

_price_paths = st.lists(
    st.tuples(
        st.integers(min_value=-300, max_value=300),  # close change, in cents
        st.integers(min_value=0, max_value=200),  # wick above, in cents
        st.integers(min_value=0, max_value=200),  # wick below, in cents
    ),
    min_size=20,
    max_size=60,
)


def _bars_from_path(path: list[tuple[int, int, int]], start_cents: int = 100_000) -> list[Bar]:
    bars, price = [], start_cents
    for change, wick_up, wick_down in path:
        open_, close = price, price + change
        high, low = max(open_, close) + wick_up, min(open_, close) - wick_down
        bars.append(_bar(Decimal(high) / 100, Decimal(low) / 100, Decimal(close) / 100))
        price = close
    return bars


@given(path=_price_paths)
@h_settings(max_examples=150, deadline=None)
def test_value_is_never_negative(path: list[tuple[int, int, int]]) -> None:
    value = choppiness_index(_bars_from_path(path), period=14)

    assert value is None or value >= 0


@given(path=_price_paths)
@h_settings(max_examples=150, deadline=None)
def test_value_is_at_most_one_hundred_when_the_prior_close_is_inside_the_window_range(
    path: list[tuple[int, int, int]],
) -> None:
    bars = _bars_from_path(path)
    window = bars[-14:]
    prior_close = bars[-15].close
    inside = min(b.low for b in window) <= prior_close <= max(b.high for b in window)
    value = choppiness_index(bars, period=14)

    if value is not None and inside:
        assert value <= Decimal("100.00")


@given(path=_price_paths, shift_cents=st.integers(min_value=-50_000, max_value=5_000_000))
@h_settings(max_examples=150, deadline=None)
def test_shifting_every_price_by_a_constant_changes_nothing(
    path: list[tuple[int, int, int]], shift_cents: int
) -> None:
    bars = _bars_from_path(path)
    shifted = _bars_from_path(path, start_cents=100_000 + shift_cents)

    assert choppiness_index(shifted, period=14) == choppiness_index(bars, period=14)


@given(path=_price_paths, factor=st.sampled_from(["0.01", "0.5", "3", "7.25", "1000"]))
@h_settings(max_examples=150, deadline=None)
def test_scaling_every_price_by_a_positive_factor_changes_nothing(
    path: list[tuple[int, int, int]], factor: str
) -> None:
    bars = _bars_from_path(path)
    scale = Decimal(factor)
    scaled = [
        Bar(b.open * scale, b.high * scale, b.low * scale, b.close * scale, b.volume) for b in bars
    ]
    original, rescaled = choppiness_index(bars, period=14), choppiness_index(scaled, period=14)

    assert (original is None) == (rescaled is None)
    if original is not None and rescaled is not None:
        assert abs(original - rescaled) <= TOLERANCE


@given(step_cents=st.integers(min_value=1, max_value=5_000), count=st.integers(15, 60))
@h_settings(max_examples=100, deadline=None)
def test_any_gapless_monotone_trend_is_zero(step_cents: int, count: int) -> None:
    step = Decimal(step_cents) / 100
    rising = [
        _bar(high=100 + step * (i + 1), low=100 + step * i, close=100 + step * (i + 1))
        for i in range(count)
    ]
    top = 100 + step * (count + 1)
    falling = [
        _bar(high=top - step * i, low=top - step * (i + 1), close=top - step * (i + 1))
        for i in range(count)
    ]

    assert choppiness_index(rising, period=14) == Decimal("0.00")
    assert choppiness_index(falling, period=14) == Decimal("0.00")
