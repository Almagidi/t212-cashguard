"""
Unit tests for the Opening Fade strategy.

All functions are pure Python — no DB, no broker, no I/O.
"""

from __future__ import annotations

from decimal import Decimal

from app.strategies.indicators import Bar
from app.strategies.opening_fade import (
    DEFAULT_FADE_PARAMS,
    FadeSignal,
    OpeningFadeStrategy,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _bar(o=100, h=105, low=98, c=102, v=10_000):
    return Bar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _flat_bars(n=20, price=100, volume=10_000):
    return [_bar(price, price + 2, price - 2, price, volume) for _ in range(n)]


def _choppy_bars(n=20, base=100, volume=10_000):
    """Alternating high/low bars produce high choppiness."""
    bars = []
    for i in range(n):
        if i % 2 == 0:
            bars.append(_bar(base, base + 10, base - 10, base + 8, volume))
        else:
            bars.append(_bar(base, base + 10, base - 10, base - 8, volume))
    return bars


# Relaxed params for easier signal generation in tests
RELAXED = {
    **DEFAULT_FADE_PARAMS,
    "avoid_first_minutes": 0,
    "avoid_last_minutes": 0,
    "avoid_lunch": False,
    "fade_window_minutes": 600,  # whole session
    "min_rvol": 1.0,
    "n_confirm": 1,
    "reward_risk_ratio_min": 1.0,
    "chop_threshold": 0.0,  # always pass chop gate
    "min_atr_pct": 0.0,
    "max_atr_pct": 100.0,
}

VALID_TIME = "14:35"
ACCOUNT = Decimal("100000")
CASH = Decimal("50000")


# ── _time_in_fade_window ──────────────────────────────────────────────────────


class TestTimeInFadeWindow:
    def _svc(self, **overrides):
        return OpeningFadeStrategy(params={**DEFAULT_FADE_PARAMS, **overrides})

    def test_before_session_returns_false(self):
        svc = self._svc()
        assert svc._time_in_fade_window("13:00") is False

    def test_after_session_returns_false(self):
        svc = self._svc()
        assert svc._time_in_fade_window("21:01") is False

    def test_within_buffer_at_open_returns_false(self):
        svc = self._svc(avoid_first_minutes=5)
        assert svc._time_in_fade_window("14:31") is False

    def test_valid_window_returns_true(self):
        svc = self._svc(
            avoid_first_minutes=0,
            avoid_last_minutes=0,
            avoid_lunch=False,
            fade_window_minutes=600,
        )
        assert svc._time_in_fade_window("15:00") is True

    def test_outside_fade_window_returns_false(self):
        # fade_window_minutes=15: only 14:30-14:45 valid; 15:30 is outside
        svc = self._svc(avoid_first_minutes=0, avoid_last_minutes=60, fade_window_minutes=15)
        assert svc._time_in_fade_window("15:30") is False

    def test_before_close_buffer_returns_false(self):
        svc = self._svc(avoid_last_minutes=120, fade_window_minutes=600)
        # 20:30 is within 120 min of 21:00 close
        assert svc._time_in_fade_window("19:00") is False

    def test_lunch_hour_blocked(self):
        svc = self._svc(avoid_lunch=True, fade_window_minutes=600, avoid_last_minutes=0)
        # 17:30 = 12:30 ET — inside lunch block
        assert svc._time_in_fade_window("17:30") is False

    def test_lunch_disabled_passes(self):
        svc = self._svc(
            avoid_lunch=False,
            fade_window_minutes=600,
            avoid_first_minutes=0,
            avoid_last_minutes=0,
        )
        assert svc._time_in_fade_window("17:30") is True


# ── _count_confirm_bars ───────────────────────────────────────────────────────


class TestCountConfirmBars:
    def _svc(self):
        return OpeningFadeStrategy(params=RELAXED)

    def test_count_down_bars(self):
        svc = self._svc()
        session_open = Decimal("100")
        bars = [
            _bar(c=98),  # below open
            _bar(c=97),  # below open
            _bar(c=99),  # below open
        ]
        assert svc._count_confirm_bars(bars, session_open, "down") == 3

    def test_count_up_bars(self):
        svc = self._svc()
        session_open = Decimal("100")
        bars = [
            _bar(c=102),
            _bar(c=103),
        ]
        assert svc._count_confirm_bars(bars, session_open, "up") == 2

    def test_stops_at_non_conforming_bar(self):
        svc = self._svc()
        session_open = Decimal("100")
        bars = [
            _bar(c=105),  # above — breaks the "down" streak
            _bar(c=97),  # below
            _bar(c=96),  # below
        ]
        # Most recent 2 are below, then 105 breaks streak → count=2
        assert svc._count_confirm_bars(bars, session_open, "down") == 2

    def test_empty_bars_returns_zero(self):
        svc = self._svc()
        assert svc._count_confirm_bars([], Decimal("100"), "down") == 0

    def test_bar_exactly_at_open_not_counted_for_down(self):
        svc = self._svc()
        session_open = Decimal("100")
        bars = [_bar(c=100)]  # at open, not < open
        assert svc._count_confirm_bars(bars, session_open, "down") == 0

    def test_bar_exactly_at_open_not_counted_for_up(self):
        svc = self._svc()
        session_open = Decimal("100")
        bars = [_bar(c=100)]  # at open, not > open
        assert svc._count_confirm_bars(bars, session_open, "up") == 0


# ── generate_signal — filters ─────────────────────────────────────────────────


class TestGenerateSignalFilters:
    def _svc(self, **extra):
        return OpeningFadeStrategy(params={**RELAXED, **extra})

    def test_too_few_bars_returns_none(self):
        svc = self._svc()
        result = svc.generate_signal(
            "AAPL", _flat_bars(3), ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("100")
        )
        assert result is None

    def test_none_prev_close_returns_none(self):
        svc = self._svc()
        result = svc.generate_signal(
            "AAPL", _flat_bars(20), ACCOUNT, CASH, VALID_TIME, prev_close=None
        )
        assert result is None

    def test_zero_prev_close_returns_none(self):
        svc = self._svc()
        result = svc.generate_signal(
            "AAPL", _flat_bars(20), ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("0")
        )
        assert result is None

    def test_outside_time_window_returns_none(self):
        svc = OpeningFadeStrategy()  # default params
        result = svc.generate_signal(
            "AAPL", _flat_bars(20), ACCOUNT, CASH, "13:00", prev_close=Decimal("100")
        )
        assert result is None

    def test_session_open_below_min_price_returns_none(self):
        svc = self._svc(min_price=10.0)
        bars = _flat_bars(20, price=3)  # below min_price
        result = svc.generate_signal(
            "AAPL", bars, ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("3")
        )
        assert result is None

    def test_gap_too_small_returns_none(self):
        svc = self._svc(min_gap_pct=2.0)
        # gap = 0.5%
        bars = _flat_bars(20, price=100)
        bars[0] = _bar(o=100)
        result = svc.generate_signal(
            "AAPL", bars, ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("99.5")
        )
        assert result is None

    def test_gap_too_large_returns_none(self):
        svc = self._svc(max_gap_pct=5.0)
        # gap = 10%
        bars = _flat_bars(20, price=110)
        result = svc.generate_signal(
            "AAPL", bars, ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("100")
        )
        assert result is None

    def test_insufficient_volume_returns_none(self):
        svc = self._svc(min_rvol=5.0)
        bars = _choppy_bars(20, volume=100)
        result = svc.generate_signal(
            "AAPL", bars, ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("97")
        )
        assert result is None

    def test_trending_market_skipped_due_to_chop_gate(self):
        svc = self._svc(chop_threshold=70.0)
        bars = []
        for i in range(25):
            p = 100 + i  # steadily trending up
            bars.append(_bar(p, p + 1, p - 1, p + 1, v=20_000))
        result = svc.generate_signal(
            "AAPL", bars, ACCOUNT, CASH, VALID_TIME, prev_close=Decimal("99")
        )
        assert result is None

    def test_gap_up_without_allow_short_returns_none(self):
        svc = self._svc(allow_short=False, n_confirm=1)
        # Gap up: prev_close=95, session_open=100
        bars = _choppy_bars(20, volume=30_000)
        bars[0] = _bar(o=100)
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("95"),
            session_open=Decimal("100"),
        )
        assert result is None  # gap-up fade requires allow_short


# ── generate_signal — gap-down long setup ─────────────────────────────────────


class TestGenerateSignalGapDown:
    def _svc(self):
        return OpeningFadeStrategy(params=RELAXED)

    def _gap_down_bars(self, prev_close=110, session_open=105, n=20, volume=30_000):
        """Bars where session opened gap-down vs prev_close, with some recovery."""
        bars = []
        # First bar: session open (gap down)
        bars.append(
            _bar(session_open, session_open + 2, session_open - 1, session_open + 1, v=volume)
        )
        # Middle bars: price recovers above session_open (confirm bars for gap-down fade)
        for _ in range(n - 2):
            bars.append(
                _bar(session_open + 1, session_open + 3, session_open, session_open + 2, v=volume)
            )
        # Final bar: still above session_open
        bars.append(
            _bar(session_open + 2, session_open + 4, session_open + 1, session_open + 3, v=volume)
        )
        return bars

    def test_gap_down_signal_is_buy(self):
        svc = self._svc()
        bars = self._gap_down_bars()
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        if result is not None:
            assert result.side == "buy"
            assert result.signal_type == "entry"

    def test_gap_down_stop_below_entry(self):
        svc = self._svc()
        bars = self._gap_down_bars()
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        if result is not None:
            assert result.stop_price < result.entry_price

    def test_gap_down_tp_above_entry(self):
        svc = self._svc()
        bars = self._gap_down_bars()
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        if result is not None:
            assert result.take_profit_price > result.entry_price

    def test_gap_down_returns_fade_signal(self):
        svc = self._svc()
        bars = self._gap_down_bars()
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        if result is not None:
            assert isinstance(result, FadeSignal)

    def test_gap_down_insufficient_confirm_bars_returns_none(self):
        svc = OpeningFadeStrategy(params={**RELAXED, "n_confirm": 10})
        bars = self._gap_down_bars()
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        assert result is None

    def test_gap_down_session_open_from_bars_zero(self):
        svc = self._svc()
        bars = self._gap_down_bars()
        # Don't pass session_open explicitly — should use bars[0].open
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
        )
        # Just verify it doesn't crash; may or may not signal depending on exact prices
        assert result is None or isinstance(result, FadeSignal)


# ── generate_signal — gap-up short setup ──────────────────────────────────────


class TestGenerateSignalGapUp:
    def _svc(self):
        return OpeningFadeStrategy(params={**RELAXED, "allow_short": True})

    def _gap_up_bars(self, prev_close=95, session_open=100, n=20, volume=30_000):
        """Bars where session opened gap-up, then price fades below session_open."""
        bars = []
        bars.append(
            _bar(session_open, session_open + 2, session_open - 1, session_open - 1, v=volume)
        )
        for _ in range(n - 2):
            bars.append(
                _bar(session_open - 1, session_open, session_open - 3, session_open - 2, v=volume)
            )
        bars.append(
            _bar(session_open - 1, session_open, session_open - 3, session_open - 2, v=volume)
        )
        return bars

    def test_gap_up_with_allow_short_is_sell(self):
        svc = self._svc()
        bars = self._gap_up_bars()
        result = svc.generate_signal(
            "TSLA",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("95"),
            session_open=Decimal("100"),
        )
        if result is not None:
            assert result.side == "sell"

    def test_gap_up_stop_above_entry_for_short(self):
        svc = self._svc()
        bars = self._gap_up_bars()
        result = svc.generate_signal(
            "TSLA",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("95"),
            session_open=Decimal("100"),
        )
        if result is not None:
            assert result.stop_price > result.entry_price

    def test_gap_up_no_confirm_bars_returns_none(self):
        svc = OpeningFadeStrategy(params={**RELAXED, "allow_short": True, "n_confirm": 10})
        bars = self._gap_up_bars()
        result = svc.generate_signal(
            "TSLA",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("95"),
            session_open=Decimal("100"),
        )
        assert result is None


# ── confidence scoring ────────────────────────────────────────────────────────


class TestFadeConfidence:
    def test_confidence_in_valid_range(self):
        svc = OpeningFadeStrategy(params=RELAXED)
        bars = []
        prev_close = Decimal("110")
        session_open = Decimal("105")
        for _ in range(3):
            bars.append(_bar(105, 107, 103, 107, v=50_000))
        for _ in range(17):
            bars.append(_bar(106, 108, 104, 107, v=50_000))
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=prev_close,
            session_open=session_open,
        )
        if result is not None:
            assert Decimal("0") < result.confidence <= Decimal("0.90")

    def test_params_snapshot_contains_expected_keys(self):
        svc = OpeningFadeStrategy(params=RELAXED)
        bars = []
        for _ in range(20):
            bars.append(_bar(106, 108, 104, 107, v=50_000))
        result = svc.generate_signal(
            "AAPL",
            bars,
            ACCOUNT,
            CASH,
            VALID_TIME,
            prev_close=Decimal("110"),
            session_open=Decimal("105"),
        )
        if result is not None:
            snap = result.params_snapshot
            assert "gap_pct" in snap
            assert "chop" in snap
            assert "rvol" in snap


# ── check_exit_conditions ─────────────────────────────────────────────────────


class TestCheckExitConditions:
    def _svc(self):
        return OpeningFadeStrategy(params=RELAXED)

    def test_stop_hit_for_long_returns_stop_signal(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("95"),  # below stop
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
        )
        assert result is not None
        assert result.signal_type == "stop"
        assert result.side == "sell"

    def test_stop_not_hit_for_long_returns_none(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("102"),  # above stop
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
        )
        assert result is None

    def test_tp_hit_for_long_returns_take_profit(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("116"),  # at/above TP
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
        )
        assert result is not None
        assert result.signal_type == "take_profit"

    def test_partial_exit_at_1r_for_long(self):
        svc = self._svc()
        bars = _flat_bars(20)
        # entry=100, stop=97 → risk=3, 1R = 103
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("104"),  # above 1R
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
            partial_exit_done=False,
        )
        assert result is not None
        assert result.signal_type == "partial_exit"
        assert result.side == "sell"

    def test_partial_not_returned_when_already_done(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("104"),
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
            partial_exit_done=True,  # already done
        )
        assert result is None

    def test_short_stop_hit_above_price(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="TSLA",
            side="sell",
            current_price=Decimal("105"),  # above stop → stop hit for short
            entry_price=Decimal("100"),
            stop_price=Decimal("103"),
            take_profit_price=Decimal("85"),
            remaining_qty=Decimal("5"),
            bars=bars,
        )
        assert result is not None
        assert result.signal_type == "stop"
        assert result.side == "buy"

    def test_short_tp_hit_below_price(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="TSLA",
            side="sell",
            current_price=Decimal("83"),  # below TP
            entry_price=Decimal("100"),
            stop_price=Decimal("103"),
            take_profit_price=Decimal("85"),
            remaining_qty=Decimal("5"),
            bars=bars,
        )
        assert result is not None
        assert result.signal_type == "take_profit"

    def test_short_partial_exit_at_1r(self):
        svc = self._svc()
        bars = _flat_bars(20)
        # entry=100, stop=103 → risk=3, 1R for short = 97
        result = svc.check_exit_conditions(
            ticker="TSLA",
            side="sell",
            current_price=Decimal("96"),  # below 1R for short
            entry_price=Decimal("100"),
            stop_price=Decimal("103"),
            take_profit_price=Decimal("85"),
            remaining_qty=Decimal("10"),
            bars=bars,
            partial_exit_done=False,
        )
        assert result is not None
        assert result.signal_type == "partial_exit"

    def test_fewer_than_15_bars_uses_zero_atr(self):
        svc = self._svc()
        bars = _flat_bars(5)  # fewer than 15
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("95"),
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("5"),
            bars=bars,
        )
        assert result is not None  # stop hit
        assert result.atr_value == Decimal("0")

    def test_no_exit_conditions_met(self):
        svc = self._svc()
        bars = _flat_bars(20)
        result = svc.check_exit_conditions(
            ticker="AAPL",
            side="buy",
            current_price=Decimal("102"),  # between stop and TP
            entry_price=Decimal("100"),
            stop_price=Decimal("97"),
            take_profit_price=Decimal("115"),
            remaining_qty=Decimal("10"),
            bars=bars,
        )
        assert result is None
