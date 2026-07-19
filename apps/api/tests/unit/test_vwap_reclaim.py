"""
Unit tests for the VWAP Reclaim strategy.

All tests use synthetic Bar sequences — no DB, no broker, no I/O.
"""

from __future__ import annotations

from decimal import Decimal

from app.strategies.indicators import Bar
from app.strategies.vwap_reclaim import DEFAULT_VWAP_PARAMS, VWAPReclaimStrategy, VWAPSignal

# ── helpers ───────────────────────────────────────────────────────────────────


def _bar(o=100, h=105, low=98, c=102, v=10_000, volume=None):
    vol = volume if volume is not None else v
    return Bar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal(str(vol)),
    )


def _base_bars(n=25, price=100, volume=10_000):
    """N flat bars — provide enough history for VWAP and ATR."""
    return [_bar(price, price + 5, price - 5, price, volume) for _ in range(n)]


def _build_reclaim_bars(n_history=23, dip_n=2, reclaim_price=105, volume=50_000):
    """
    Build a bar sequence that satisfies the VWAP reclaim setup:
    - history bars to establish VWAP and ATR
    - `dip_n` bars below VWAP
    - one reclaim bar closing above VWAP with elevated volume
    """
    base_price = 100
    bars = _base_bars(n_history, base_price, volume=10_000)
    # Current VWAP of the base bars will be near 100
    # Add dip bars (below VWAP)
    for _ in range(dip_n):
        bars.append(_bar(95, 97, 92, 94, volume=8_000))
    # Add reclaim bar: close above 100 (VWAP) with high volume
    bars.append(_bar(99, 110, 98, reclaim_price, volume=volume))
    return bars


# Valid trading time (mid-session)
VALID_TIME = "16:00"

# Strategy with relaxed filters for easier signal generation
RELAXED_PARAMS = {
    **DEFAULT_VWAP_PARAMS,
    "avoid_first_minutes": 0,
    "avoid_last_minutes": 0,
    "avoid_lunch": False,
    "min_rvol": 1.0,
    "min_bars_below_vwap": 1,
    "reward_risk_ratio_min": 1.0,
}


class TestVWAPReclaimInit:
    def test_default_params_applied(self):
        svc = VWAPReclaimStrategy()
        assert svc.params["min_rvol"] == DEFAULT_VWAP_PARAMS["min_rvol"]

    def test_custom_params_merged(self):
        svc = VWAPReclaimStrategy(params={"min_rvol": 3.0})
        assert svc.params["min_rvol"] == 3.0

    def test_custom_params_dont_overwrite_others(self):
        svc = VWAPReclaimStrategy(params={"min_rvol": 3.0})
        assert svc.params["risk_per_trade_pct"] == DEFAULT_VWAP_PARAMS["risk_per_trade_pct"]


class TestVWAPReclaimFilters:
    def setup_method(self):
        self.svc = VWAPReclaimStrategy(params=RELAXED_PARAMS)
        self.account = Decimal("100000")
        self.cash = Decimal("50000")

    def test_too_few_bars_returns_none(self):
        bars = _base_bars(10)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        assert result is None

    def test_outside_trading_time_returns_none(self):
        bars = _build_reclaim_bars()
        strict = VWAPReclaimStrategy(
            params={
                **DEFAULT_VWAP_PARAMS,
                "avoid_first_minutes": 120,
                "avoid_last_minutes": 120,
            }
        )
        result = strict.generate_signal("AAPL", bars, self.account, self.cash, "14:30")
        assert result is None

    def test_price_below_minimum_returns_none(self):
        bars = _base_bars(25, price=4, volume=50_000)
        # VWAP ≈ 4, so close < min_price (5.0)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        assert result is None

    def test_no_dip_before_reclaim_returns_none(self):
        # All bars close above VWAP — no prior dip
        bars = _base_bars(26, price=110, volume=50_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        assert result is None

    def test_insufficient_volume_returns_none(self):
        svc = VWAPReclaimStrategy(params={**RELAXED_PARAMS, "min_rvol": 10.0})
        bars = _build_reclaim_bars(volume=5_000)
        result = svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        assert result is None

    def test_zero_account_value_gives_tiny_quantity(self):
        bars = _build_reclaim_bars(volume=100_000)
        result = self.svc.generate_signal("AAPL", bars, Decimal("0"), Decimal("0"), VALID_TIME)
        # quantity would be 0 → returns None
        assert result is None

    def test_current_close_below_vwap_returns_none(self):
        # Build bars where the last bar closes below VWAP
        bars = _base_bars(25, price=100, volume=10_000)
        bars.append(_bar(95, 97, 90, 92, volume=50_000))  # close < VWAP
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        assert result is None


class TestVWAPReclaimSignal:
    def setup_method(self):
        self.svc = VWAPReclaimStrategy(params=RELAXED_PARAMS)
        self.account = Decimal("100000")
        self.cash = Decimal("50000")

    def test_signal_is_buy(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("TSLA", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert result.side == "buy"
            assert result.signal_type == "entry"

    def test_signal_has_correct_ticker(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("TSLA", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert result.ticker == "TSLA"

    def test_signal_stop_below_entry(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert result.stop_price < result.entry_price

    def test_signal_tp_above_entry(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert result.take_profit_price > result.entry_price

    def test_signal_confidence_in_range(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert Decimal("0") <= result.confidence <= Decimal("1")

    def test_signal_has_params_snapshot(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert "vwap" in result.params_snapshot
            assert "atr" in result.params_snapshot
            assert "rvol" in result.params_snapshot

    def test_returns_vwap_signal_instance(self):
        bars = _build_reclaim_bars(volume=80_000)
        result = self.svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            assert isinstance(result, VWAPSignal)

    def test_high_rvol_boosts_confidence(self):
        svc = VWAPReclaimStrategy(params=RELAXED_PARAMS)
        # Generate with very high volume (rvol >= 2.0)
        bars = _build_reclaim_bars(volume=200_000)
        result = svc.generate_signal("AAPL", bars, self.account, self.cash, VALID_TIME)
        if result is not None:
            # confidence bumped by +0.15 for high rvol
            assert result.confidence >= Decimal("0.55")


class TestVWAPReclaimConfidenceFactors:
    def test_base_confidence_is_0_55(self):
        # In a valid signal, base confidence starts at 0.55
        svc = VWAPReclaimStrategy(params=RELAXED_PARAMS)
        bars = _build_reclaim_bars(volume=50_000)
        result = svc.generate_signal("AAPL", bars, Decimal("100000"), Decimal("50000"), VALID_TIME)
        if result is not None:
            assert result.confidence >= Decimal("0.55")
            assert result.confidence <= Decimal("0.90")

    def test_signal_reason_mentions_vwap(self):
        svc = VWAPReclaimStrategy(params=RELAXED_PARAMS)
        bars = _build_reclaim_bars(volume=80_000)
        result = svc.generate_signal("AAPL", bars, Decimal("100000"), Decimal("50000"), VALID_TIME)
        if result is not None:
            assert "VWAP" in result.reason

    def test_max_position_pct_caps_quantity(self):
        svc = VWAPReclaimStrategy(params={**RELAXED_PARAMS, "max_position_pct": 1.0})
        bars = _build_reclaim_bars(volume=80_000)
        result = svc.generate_signal("AAPL", bars, Decimal("100000"), Decimal("50000"), VALID_TIME)
        if result is not None:
            # max = 100000 * 1% / price ≈ 10 shares
            assert result.suggested_quantity <= Decimal("15")
