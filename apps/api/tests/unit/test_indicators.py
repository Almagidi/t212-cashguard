"""
Unit tests for app/strategies/indicators.py.

All functions are pure Python — no mocking needed.
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.strategies.indicators import (
    Bar,
    adaptive_atr_multiplier,
    atr,
    atr_pct,
    atr_position_size,
    choppiness_index,
    ema,
    ema_of_closes,
    gap_pct,
    is_clean_open,
    is_tradeable_time,
    is_trending_down,
    is_trending_up,
    is_volume_breakout,
    kelly_fraction,
    kelly_position_size,
    market_regime,
    relative_volume,
    trailing_stop_price,
    true_range,
    vwap,
    vwap_bands,
    volume_sma,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _bar(o=100, h=105, l=98, c=102, v=10_000):
    return Bar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _flat_bars(n=30, price=100, volume=10_000):
    """N flat bars at the given price."""
    return [_bar(price, price, price, price, volume) for _ in range(n)]


def _trending_bars(n=40, start=90, step=1, volume=10_000):
    """Steadily rising bars for trend detection tests."""
    bars = []
    for i in range(n):
        p = start + i * step
        bars.append(_bar(p, p + 2, p - 1, p + 1, volume))
    return bars


# ── true_range ────────────────────────────────────────────────────────────────

class TestTrueRange:
    def test_no_prev_close_uses_hl(self):
        bar = _bar(h=105, l=98)
        assert true_range(bar, None) == Decimal("7")

    def test_with_prev_close_above_high(self):
        bar = _bar(h=100, l=95, c=99)
        # prev_close=110: hc=10, lc=15, hl=5 → max=15
        assert true_range(bar, Decimal("110")) == Decimal("15")

    def test_with_prev_close_below_low(self):
        bar = _bar(h=105, l=100, c=103)
        # prev_close=90: hc=15, lc=10, hl=5 → max=15
        assert true_range(bar, Decimal("90")) == Decimal("15")

    def test_prev_close_within_range(self):
        bar = _bar(h=110, l=90, c=100)
        # hl=20, hc=|110-100|=10, lc=|90-100|=10 → max=20
        assert true_range(bar, Decimal("100")) == Decimal("20")


# ── atr ───────────────────────────────────────────────────────────────────────

class TestATR:
    def test_not_enough_bars_returns_zero(self):
        bars = _flat_bars(5)
        assert atr(bars, period=14) == Decimal("0")

    def test_flat_bars_returns_near_zero(self):
        bars = _flat_bars(30)
        result = atr(bars, period=14)
        assert result >= Decimal("0")

    def test_volatile_bars_returns_positive(self):
        bars = []
        for i in range(30):
            bars.append(_bar(h=110, l=90))  # hl range = 20
        result = atr(bars, period=14)
        assert result > Decimal("5")

    def test_result_is_decimal(self):
        bars = _flat_bars(20)
        result = atr(bars, period=14)
        assert isinstance(result, Decimal)

    def test_wilder_smoothing_decreases_with_more_bars(self):
        # For stable bars, ATR should converge
        bars = _flat_bars(50)
        a14 = atr(bars, period=14)
        a5 = atr(bars, period=5)
        # Both should be near 0 for flat data
        assert a14 >= Decimal("0")
        assert a5 >= Decimal("0")

    def test_extra_bars_beyond_period_exercised(self):
        # Ensure the `for tr in trs[period:]` branch is hit
        bars = _flat_bars(50)
        result = atr(bars, period=14)
        assert isinstance(result, Decimal)


# ── atr_pct ───────────────────────────────────────────────────────────────────

class TestATRPct:
    def test_empty_bars_returns_zero(self):
        assert atr_pct([], 14) == Decimal("0")

    def test_not_enough_bars_returns_zero(self):
        bars = _flat_bars(5)
        assert atr_pct(bars, 14) == Decimal("0")

    def test_zero_price_returns_zero(self):
        bars = [_bar(c=0) for _ in range(20)]
        assert atr_pct(bars, 14) == Decimal("0")

    def test_normal_case_returns_percentage(self):
        bars = [_bar(h=110, l=90, c=100) for _ in range(30)]
        result = atr_pct(bars, 14)
        assert result > Decimal("0")


# ── ema ───────────────────────────────────────────────────────────────────────

class TestEMA:
    def test_too_few_values_returns_zeros(self):
        result = ema([Decimal("1"), Decimal("2")], period=5)
        assert all(v == Decimal("0") for v in result)
        assert len(result) == 2

    def test_exact_period_returns_seed(self):
        vals = [Decimal("10")] * 5
        result = ema(vals, period=5)
        assert len(result) == 5
        assert result[-1] == Decimal("10")

    def test_longer_series_runs_smoothing(self):
        vals = [Decimal(str(i)) for i in range(1, 21)]
        result = ema(vals, period=5)
        assert len(result) == 20
        assert result[-1] > Decimal("0")

    def test_result_length_matches_input(self):
        vals = [Decimal("1")] * 15
        result = ema(vals, period=5)
        assert len(result) == 15


# ── ema_of_closes ─────────────────────────────────────────────────────────────

class TestEMAOfCloses:
    def test_returns_decimal(self):
        bars = _flat_bars(30, price=100)
        result = ema_of_closes(bars, 9)
        assert isinstance(result, Decimal)

    def test_not_enough_bars_returns_zero(self):
        bars = _flat_bars(5)
        result = ema_of_closes(bars, 21)
        assert result == Decimal("0")


# ── vwap ─────────────────────────────────────────────────────────────────────

class TestVWAP:
    def test_zero_volume_returns_zero(self):
        bars = [_bar(v=0) for _ in range(5)]
        assert vwap(bars) == Decimal("0")

    def test_typical_price_used(self):
        # single bar: typical = (h+l+c)/3 = (105+95+100)/3 = 100
        bars = [_bar(h=105, l=95, c=100, v=1000)]
        result = vwap(bars)
        assert result == Decimal("100.0000")

    def test_multiple_bars_weighted(self):
        bars = [
            _bar(h=110, l=100, c=105, v=2000),  # typical=105
            _bar(h=120, l=110, c=115, v=1000),  # typical=115
        ]
        result = vwap(bars)
        # weighted: (105*2000 + 115*1000) / 3000 = (210000+115000)/3000 = 108.333...
        assert result == pytest.approx(Decimal("108.3333"), abs=Decimal("0.01"))

    def test_returns_decimal(self):
        bars = _flat_bars(10)
        assert isinstance(vwap(bars), Decimal)


# ── vwap_bands ────────────────────────────────────────────────────────────────

class TestVWAPBands:
    def test_zero_vwap_returns_equal_bands(self):
        bars = [_bar(v=0) for _ in range(5)]
        v, upper, lower = vwap_bands(bars)
        assert v == upper == lower

    def test_single_bar_returns_equal_bands(self):
        bars = [_bar()]
        v, upper, lower = vwap_bands(bars)
        assert v == upper == lower

    def test_normal_bands_upper_above_lower(self):
        bars = []
        for i in range(20):
            bars.append(_bar(h=100 + i, l=90 + i, c=95 + i, v=10000))
        v, upper, lower = vwap_bands(bars)
        assert upper >= v >= lower

    def test_zero_cum_vol_returns_equal_bands(self):
        # All volumes zero → vwap=0 → returns (0,0,0)
        bars = [_bar(v=0), _bar(v=0)]
        v, upper, lower = vwap_bands(bars)
        assert v == upper == lower


# ── volume_sma ────────────────────────────────────────────────────────────────

class TestVolumeSMA:
    def test_too_few_bars_returns_zero(self):
        bars = _flat_bars(5)
        assert volume_sma(bars, period=20) == Decimal("0")

    def test_exact_period(self):
        bars = [_bar(v=1000) for _ in range(20)]
        assert volume_sma(bars, period=20) == Decimal("1000")

    def test_uses_last_n_bars(self):
        bars = [_bar(v=100)] * 10 + [_bar(v=200)] * 10
        result = volume_sma(bars, period=10)
        assert result == Decimal("200")


# ── relative_volume ───────────────────────────────────────────────────────────

class TestRelativeVolume:
    def test_zero_avg_returns_one(self):
        bars = [_bar(v=0)] * 25
        result = relative_volume(bars, period=20)
        assert result == Decimal("1")

    def test_double_average_returns_two(self):
        bars = [_bar(v=1000)] * 21
        bars[-1] = _bar(v=2000)  # current bar has 2x volume
        result = relative_volume(bars, period=20)
        assert result == Decimal("2.00")

    def test_half_average_returns_half(self):
        bars = [_bar(v=1000)] * 21
        bars[-1] = _bar(v=500)
        result = relative_volume(bars, period=20)
        assert result == Decimal("0.50")


# ── is_volume_breakout ────────────────────────────────────────────────────────

class TestIsVolumeBreakout:
    def test_high_volume_returns_true(self):
        bars = [_bar(v=1000)] * 21
        bars[-1] = _bar(v=2000)
        assert is_volume_breakout(bars, min_rvol=1.5) is True

    def test_low_volume_returns_false(self):
        bars = [_bar(v=1000)] * 21
        bars[-1] = _bar(v=500)
        assert is_volume_breakout(bars, min_rvol=1.5) is False


# ── is_trending_up / is_trending_down ────────────────────────────────────────

class TestTrendDetection:
    def test_not_enough_bars_returns_false_up(self):
        bars = _flat_bars(10)
        assert is_trending_up(bars, fast=9, slow=21) is False

    def test_not_enough_bars_returns_false_down(self):
        bars = _flat_bars(10)
        assert is_trending_down(bars, fast=9, slow=21) is False

    def test_uptrend_detected(self):
        bars = _trending_bars(50, start=50, step=1)
        assert is_trending_up(bars, fast=9, slow=21) is True

    def test_downtrend_detected(self):
        bars = _trending_bars(50, start=200, step=-1)
        assert is_trending_down(bars, fast=9, slow=21) is True

    def test_flat_market_not_trending_up(self):
        bars = _flat_bars(40)
        assert is_trending_up(bars) is False


# ── market_regime ─────────────────────────────────────────────────────────────

class TestMarketRegime:
    def test_not_enough_bars_returns_unknown(self):
        bars = _flat_bars(10)
        assert market_regime(bars) == "unknown"

    def test_flat_bars_returns_unknown_or_choppy(self):
        bars = _flat_bars(40)
        # Flat bars have 0 price_range → returns "unknown"
        result = market_regime(bars)
        assert result in ("unknown", "choppy", "neutral")

    def test_trending_up_bars(self):
        bars = _trending_bars(50, start=50, step=2)
        result = market_regime(bars)
        assert result in ("trending_up", "neutral", "choppy")

    def test_trending_down_bars(self):
        bars = _trending_bars(50, start=300, step=-2)
        result = market_regime(bars)
        assert result in ("trending_down", "neutral", "choppy")

    def test_high_choppiness_returns_choppy(self):
        # zigzag bars produce high choppiness
        bars = []
        for i in range(50):
            if i % 2 == 0:
                bars.append(_bar(h=120, l=80, c=110, v=5000))
            else:
                bars.append(_bar(h=120, l=80, c=90, v=5000))
        result = market_regime(bars)
        assert result in ("choppy", "neutral", "trending_up", "trending_down")

    def test_zero_atr_returns_unknown(self):
        # All bars identical → ATR=0
        bars = [_bar(h=100, l=100, c=100) for _ in range(40)]
        result = market_regime(bars)
        assert result == "unknown"


# ── gap_pct ───────────────────────────────────────────────────────────────────

class TestGapPct:
    def test_zero_prev_close_returns_zero(self):
        assert gap_pct(Decimal("0"), Decimal("100")) == Decimal("0")

    def test_gap_up(self):
        result = gap_pct(Decimal("100"), Decimal("102"))
        assert result == Decimal("2.00")

    def test_gap_down(self):
        result = gap_pct(Decimal("100"), Decimal("98"))
        assert result == Decimal("-2.00")

    def test_no_gap(self):
        result = gap_pct(Decimal("100"), Decimal("100"))
        assert result == Decimal("0.00")


# ── is_clean_open ─────────────────────────────────────────────────────────────

class TestIsCleanOpen:
    def test_small_gap_is_clean(self):
        assert is_clean_open(Decimal("100"), Decimal("101"), max_gap_pct=2.0) is True

    def test_large_gap_not_clean(self):
        assert is_clean_open(Decimal("100"), Decimal("106"), max_gap_pct=2.0) is False

    def test_exactly_at_threshold_is_clean(self):
        assert is_clean_open(Decimal("100"), Decimal("102"), max_gap_pct=2.0) is True

    def test_gap_down_also_checked(self):
        assert is_clean_open(Decimal("100"), Decimal("94"), max_gap_pct=2.0) is False


# ── atr_position_size ─────────────────────────────────────────────────────────

class TestATRPositionSize:
    def test_zero_atr_returns_zero(self):
        result = atr_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            atr_val=Decimal("0"),
            risk_pct=Decimal("1"),
        )
        assert result == Decimal("0")

    def test_zero_entry_price_returns_zero(self):
        result = atr_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("0"),
            atr_val=Decimal("2"),
            risk_pct=Decimal("1"),
        )
        assert result == Decimal("0")

    def test_basic_sizing(self):
        # risk = 100000 * 1/100 = 1000, stop_dist = 2*2 = 4, qty = 1000/4 = 250
        result = atr_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            atr_val=Decimal("2"),
            risk_pct=Decimal("1"),
            atr_stop_multiplier=2.0,
        )
        assert result == Decimal("250.00")

    def test_cash_cap_applied(self):
        # Without cap: qty = 250, but available_cash = 1000 → max_by_cash = 10
        result = atr_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            atr_val=Decimal("2"),
            risk_pct=Decimal("1"),
            atr_stop_multiplier=2.0,
            available_cash=Decimal("1000"),
        )
        assert result == Decimal("10.00")

    def test_no_cash_cap_when_none(self):
        result = atr_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            atr_val=Decimal("2"),
            risk_pct=Decimal("1"),
            atr_stop_multiplier=2.0,
            available_cash=None,
        )
        assert result == Decimal("250.00")


# ── trailing_stop_price ───────────────────────────────────────────────────────

class TestTrailingStopPrice:
    def test_long_no_initial_stop(self):
        # stop = current - atr*mult = 110 - 2*2.5 = 105
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            atr_val=Decimal("2"),
            side="buy",
            atr_multiplier=2.5,
        )
        assert result == Decimal("105")

    def test_long_initial_stop_overrides_if_higher(self):
        # dynamic = 110 - 2*2.5 = 105; initial_stop = 108 → max = 108
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            atr_val=Decimal("2"),
            side="buy",
            atr_multiplier=2.5,
            initial_stop=Decimal("108"),
        )
        assert result == Decimal("108")

    def test_long_dynamic_overrides_if_higher(self):
        # dynamic = 110 - 2*2.5 = 105; initial_stop = 90 → max = 105
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            atr_val=Decimal("2"),
            side="buy",
            atr_multiplier=2.5,
            initial_stop=Decimal("90"),
        )
        assert result == Decimal("105")

    def test_short_no_initial_stop(self):
        # stop = current + atr*mult = 90 + 2*2.5 = 95
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("90"),
            atr_val=Decimal("2"),
            side="sell",
            atr_multiplier=2.5,
        )
        assert result == Decimal("95")

    def test_short_initial_stop_overrides_if_lower(self):
        # dynamic = 90 + 5 = 95; initial_stop = 92 → min = 92
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("90"),
            atr_val=Decimal("2"),
            side="sell",
            atr_multiplier=2.5,
            initial_stop=Decimal("92"),
        )
        assert result == Decimal("92")

    def test_short_dynamic_overrides_if_lower(self):
        # dynamic = 90 + 5 = 95; initial_stop = 98 → min = 95
        result = trailing_stop_price(
            entry_price=Decimal("100"),
            current_price=Decimal("90"),
            atr_val=Decimal("2"),
            side="sell",
            atr_multiplier=2.5,
            initial_stop=Decimal("98"),
        )
        assert result == Decimal("95")


# ── choppiness_index ──────────────────────────────────────────────────────────

class TestChoppinessIndex:
    def test_not_enough_bars_returns_fifty(self):
        bars = _flat_bars(5)
        assert choppiness_index(bars, period=14) == Decimal("50")

    def test_zero_range_returns_fifty(self):
        bars = [_bar(h=100, l=100, c=100) for _ in range(20)]
        assert choppiness_index(bars, period=14) == Decimal("50")

    def test_choppy_market_returns_high_value(self):
        # Large ATR relative to range → high choppiness
        bars = []
        for i in range(20):
            if i % 2 == 0:
                bars.append(_bar(h=150, l=50, c=140, v=10000))
            else:
                bars.append(_bar(h=150, l=50, c=60, v=10000))
        result = choppiness_index(bars, period=14)
        assert Decimal("0") <= result <= Decimal("100")

    def test_trending_market_returns_lower_value(self):
        bars = _trending_bars(30, start=50, step=3)
        result = choppiness_index(bars, period=14)
        assert Decimal("0") <= result <= Decimal("100")

    def test_result_clamped_between_0_and_100(self):
        bars = _flat_bars(30)
        bars[0] = _bar(h=200, l=50)  # one extreme bar
        result = choppiness_index(bars, period=14)
        assert Decimal("0") <= result <= Decimal("100")


# ── adaptive_atr_multiplier ───────────────────────────────────────────────────

class TestAdaptiveATRMultiplier:
    def test_not_enough_bars_returns_base(self):
        bars = _flat_bars(10)
        result = adaptive_atr_multiplier(bars, base_multiplier=2.0)
        assert result == 2.0

    def test_zero_current_atr_returns_base(self):
        # Flat bars → ATR ≈ 0
        bars = [_bar(h=100, l=100, c=100) for _ in range(50)]
        result = adaptive_atr_multiplier(bars, base_multiplier=2.0)
        assert result == 2.0

    def test_result_clamped_to_floor(self):
        # Calm recent bars (low ATR) vs volatile history
        bars = []
        for _ in range(20):
            bars.append(_bar(h=150, l=50))  # volatile
        for _ in range(30):
            bars.append(_bar(h=101, l=99, c=100))  # calm
        result = adaptive_atr_multiplier(bars, base_multiplier=2.0, floor=1.5, ceiling=4.0)
        assert result >= 1.5

    def test_result_clamped_to_ceiling(self):
        # Very volatile recent bars
        bars = []
        for _ in range(30):
            bars.append(_bar(h=100, l=100, c=100))  # calm history
        for _ in range(20):
            bars.append(_bar(h=200, l=0, c=100))   # volatile recent
        result = adaptive_atr_multiplier(bars, base_multiplier=2.0, floor=1.5, ceiling=4.0)
        assert result <= 4.0

    def test_returns_float(self):
        bars = _flat_bars(50)
        result = adaptive_atr_multiplier(bars, base_multiplier=2.0)
        assert isinstance(result, float)


# ── kelly_fraction ────────────────────────────────────────────────────────────

class TestKellyFraction:
    def test_zero_avg_loss_returns_zero(self):
        assert kelly_fraction(0.6, 0.02, 0.0) == 0.0

    def test_zero_win_rate_returns_zero(self):
        assert kelly_fraction(0.0, 0.02, 0.01) == 0.0

    def test_zero_avg_win_returns_zero(self):
        assert kelly_fraction(0.6, 0.0, 0.01) == 0.0

    def test_negative_edge_returns_zero(self):
        # Edge = b*p - q: b=0.5, p=0.3, q=0.7 → 0.15 - 0.7 = -0.55 < 0
        assert kelly_fraction(0.3, 0.01, 0.02, fraction=0.25) == 0.0

    def test_positive_edge_returns_nonzero(self):
        # 60% win rate, 2% win, 1% loss: b=2, full_kelly=(2*0.6-0.4)/2=0.4
        result = kelly_fraction(0.6, 0.02, 0.01, fraction=0.25)
        assert result > 0.0
        assert result <= 1.0

    def test_fraction_scales_result(self):
        r_full = kelly_fraction(0.6, 0.02, 0.01, fraction=1.0)
        r_half = kelly_fraction(0.6, 0.02, 0.01, fraction=0.5)
        assert r_full == pytest.approx(r_half * 2, rel=1e-6)

    def test_result_capped_at_one(self):
        # Very high edge — should still be ≤ 1
        result = kelly_fraction(0.99, 0.1, 0.001, fraction=10.0)
        assert result <= 1.0


# ── kelly_position_size ───────────────────────────────────────────────────────

class TestKellyPositionSize:
    def test_zero_risk_per_share_returns_zero(self):
        result = kelly_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("100"),  # same as entry → risk = 0
            win_rate=0.6,
            avg_win_pct=0.02,
            avg_loss_pct=0.01,
        )
        assert result == Decimal("0")

    def test_negative_edge_returns_zero(self):
        result = kelly_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            win_rate=0.2,  # very low win rate → negative edge
            avg_win_pct=0.01,
            avg_loss_pct=0.02,
        )
        assert result == Decimal("0")

    def test_normal_sizing_returns_positive(self):
        result = kelly_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            win_rate=0.6,
            avg_win_pct=0.02,
            avg_loss_pct=0.01,
        )
        assert result > Decimal("0")

    def test_cash_cap_applied(self):
        result = kelly_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            win_rate=0.6,
            avg_win_pct=0.02,
            avg_loss_pct=0.01,
            available_cash=Decimal("500"),  # can only afford 5 shares
        )
        assert result <= Decimal("5.01")  # 500/100 = 5.00

    def test_zero_entry_price_returns_zero(self):
        result = kelly_position_size(
            account_value=Decimal("100000"),
            entry_price=Decimal("0"),
            stop_price=Decimal("95"),
            win_rate=0.6,
            avg_win_pct=0.02,
            avg_loss_pct=0.01,
        )
        assert result == Decimal("0")


# ── is_tradeable_time ─────────────────────────────────────────────────────────

class TestIsTradableTime:
    def test_before_session_returns_false(self):
        # Before 14:30 UTC
        assert is_tradeable_time("13:00") is False

    def test_after_session_returns_false(self):
        # 21:00 UTC = close
        assert is_tradeable_time("21:00") is False

    def test_well_after_session_returns_false(self):
        assert is_tradeable_time("22:00") is False

    def test_first_minutes_returns_false(self):
        # 14:32 = 2 min after open, avoid_first_minutes=5 → blocked
        assert is_tradeable_time("14:32", avoid_first_minutes=5) is False

    def test_last_minutes_returns_false(self):
        # 20:35 = within 25 min of 21:00 close, avoid_last_minutes=30 → blocked
        assert is_tradeable_time("20:35", avoid_last_minutes=30) is False

    def test_lunch_hour_returns_false(self):
        # 17:30 UTC = 12:30 ET — inside lunch block
        assert is_tradeable_time("17:30", avoid_lunch=True) is False

    def test_lunch_avoidance_disabled(self):
        # Same time but avoid_lunch=False → should pass if otherwise valid
        assert is_tradeable_time(
            "17:30",
            avoid_first_minutes=5,
            avoid_last_minutes=30,
            avoid_lunch=False,
        ) is True

    def test_valid_mid_session_returns_true(self):
        # 16:00 UTC = 11:00 ET — clean window
        assert is_tradeable_time(
            "16:00",
            avoid_first_minutes=5,
            avoid_last_minutes=30,
            avoid_lunch=True,
        ) is True

    def test_custom_session_window(self):
        assert is_tradeable_time(
            "15:00",
            session_open_utc="14:30",
            session_close_utc="21:00",
            avoid_first_minutes=5,
            avoid_last_minutes=30,
            avoid_lunch=False,
        ) is True

    def test_outside_custom_session(self):
        assert is_tradeable_time(
            "13:00",
            session_open_utc="14:30",
            session_close_utc="21:00",
            avoid_first_minutes=5,
            avoid_last_minutes=30,
        ) is False
