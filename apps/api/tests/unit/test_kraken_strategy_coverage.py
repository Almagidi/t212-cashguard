from __future__ import annotations

from decimal import Decimal

import pytest

import app.strategies.kraken_breakout_retest as br
import app.strategies.kraken_mean_reversion as mr
import app.strategies.kraken_momentum as mom
import app.strategies.kraken_trend_follow as tf
from app.strategies.indicators import Bar
from app.strategies.kraken_breakout_retest import KrakenBreakoutRetestStrategy
from app.strategies.kraken_mean_reversion import KrakenMeanReversionStrategy
from app.strategies.kraken_momentum import KrakenMomentumStrategy
from app.strategies.kraken_trend_follow import KrakenHTFBreakoutStrategy


ACCOUNT = Decimal("100000")
CASH = Decimal("50000")
NOW = "2026-05-11T14:00:00Z"


def _bar(o=100, h=105, l=95, c=100, v=10_000) -> Bar:
    return Bar(
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _bars(n: int, price=100, high=None, low=None, volume=10_000) -> list[Bar]:
    high = price + 5 if high is None else high
    low = price - 5 if low is None else low
    return [_bar(price, high, low, price, volume) for _ in range(n)]


# ── Kraken mean reversion ─────────────────────────────────────────────────────


def _patch_mean_success(monkeypatch, *, rvol="2.5", rsi_value="30", atr_value="2", qty="1"):
    monkeypatch.setattr(
        mr,
        "bollinger_bands",
        lambda bars, period, multiplier: (Decimal("120"), Decimal("110"), Decimal("100")),
    )
    monkeypatch.setattr(mr, "relative_volume", lambda bars, period: Decimal(rvol))
    monkeypatch.setattr(mr, "rsi", lambda bars, period: Decimal(rsi_value))
    monkeypatch.setattr(mr, "atr", lambda bars, period: Decimal(atr_value))
    monkeypatch.setattr(mr, "atr_position_size", lambda **kwargs: Decimal(qty))


def test_kraken_mean_reversion_metadata_and_param_merge():
    strategy = KrakenMeanReversionStrategy(params={"min_rvol": 1.1})

    assert strategy.VENUE == "kraken"
    assert strategy.PAPER_ONLY is True
    assert strategy.APPROVED is False
    assert strategy.params["min_rvol"] == 1.1
    assert strategy.params["bb_period"] == mr.DEFAULT_PARAMS["bb_period"]


def test_kraken_mean_reversion_returns_none_with_too_few_bars():
    strategy = KrakenMeanReversionStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(5), ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_returns_none_when_price_below_minimum():
    strategy = KrakenMeanReversionStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(22, price=5), ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_requires_previous_bar_at_lower_band(monkeypatch):
    _patch_mean_success(monkeypatch)
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=101), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_requires_current_reclaim_above_lower_band(monkeypatch):
    _patch_mean_success(monkeypatch)
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=100)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_requires_volume_confirmation(monkeypatch):
    _patch_mean_success(monkeypatch, rvol="1.0")
    strategy = KrakenMeanReversionStrategy(params={"min_rvol": 1.3})
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_requires_recovery_rsi(monkeypatch):
    _patch_mean_success(monkeypatch, rsi_value="50")
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_emits_buy_signal(monkeypatch):
    _patch_mean_success(monkeypatch)
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    signal = strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW)

    assert signal is not None
    assert signal.ticker == "BTC/USD"
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == Decimal("105")
    assert signal.stop_price < signal.entry_price
    assert signal.take_profit_price > signal.entry_price
    assert signal.suggested_quantity == Decimal("1")
    assert signal.bb_lower == Decimal("100")
    assert signal.bb_middle == Decimal("110")
    assert signal.params_snapshot["venue"] == "kraken"
    assert signal.params_snapshot["paper_only"] is True


# ── Kraken momentum ───────────────────────────────────────────────────────────


def _patch_momentum_success(monkeypatch, *, fast="125", slow="100", chop="30", upper="110", rvol="2.5", atr_value="5", qty="1"):
    def fake_ema(bars, period):
        return Decimal(fast) if period == 9 else Decimal(slow)

    monkeypatch.setattr(mom, "ema_of_closes", fake_ema)
    monkeypatch.setattr(mom, "choppiness_index", lambda bars, period: Decimal(chop))
    monkeypatch.setattr(mom, "donchian_channel", lambda bars, period: (Decimal(upper), Decimal("90")))
    monkeypatch.setattr(mom, "relative_volume", lambda bars, period: Decimal(rvol))
    monkeypatch.setattr(mom, "atr", lambda bars, period: Decimal(atr_value))
    monkeypatch.setattr(mom, "atr_position_size", lambda **kwargs: Decimal(qty))


def test_kraken_momentum_metadata_and_param_merge():
    strategy = KrakenMomentumStrategy(params={"min_rvol": 1.2})

    assert strategy.VENUE == "kraken"
    assert strategy.PAPER_ONLY is True
    assert strategy.APPROVED is False
    assert strategy.params["min_rvol"] == 1.2
    assert strategy.params["donchian_period"] == mom.DEFAULT_PARAMS["donchian_period"]


def test_kraken_momentum_returns_none_with_too_few_bars():
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(5, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_returns_none_when_price_below_minimum():
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=50), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_requires_fast_ema_above_slow(monkeypatch):
    _patch_momentum_success(monkeypatch, fast="100", slow="100")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_requires_non_choppy_market(monkeypatch):
    _patch_momentum_success(monkeypatch, chop="80")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_requires_donchian_breakout(monkeypatch):
    _patch_momentum_success(monkeypatch, upper="130")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_requires_relative_volume(monkeypatch):
    _patch_momentum_success(monkeypatch, rvol="1.0")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_emits_buy_signal(monkeypatch):
    _patch_momentum_success(monkeypatch)
    strategy = KrakenMomentumStrategy()
    bars = _bars(25, price=125)

    signal = strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW)

    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == Decimal("125")
    assert signal.stop_price < signal.entry_price
    assert signal.take_profit_price > signal.entry_price
    assert signal.atr_value == Decimal("5")
    assert signal.rvol == Decimal("2.5")
    assert signal.params_snapshot["donchian_upper"] == 110.0
    assert signal.params_snapshot["paper_only"] is True


# ── Kraken breakout-retest ────────────────────────────────────────────────────


def _breakout_retest_bars(current_close=106, current_volume=50_000) -> list[Bar]:
    # First 20 bars define resistance at 100.
    resistance = [_bar(h=100, c=95, v=10_000) for _ in range(20)]
    # 8 setup bars: breakout first, then retest/hold, then continuation setup.
    setup = [
        _bar(h=108, l=101, c=105, v=20_000),  # breakout above R
        _bar(h=104, l=101, c=99, v=20_000),   # retest within 2% zone, holds >= 98
        _bar(h=106, l=102, c=104, v=20_000),
        _bar(h=105, l=101, c=103, v=20_000),
        _bar(h=106, l=102, c=104, v=20_000),
        _bar(h=107, l=103, c=105, v=20_000),
        _bar(h=108, l=104, c=106, v=20_000),
        _bar(h=107, l=103, c=105, v=20_000),
    ]
    current = [_bar(h=max(108, current_close), l=102, c=current_close, v=current_volume)]
    return resistance + setup + current


def _patch_breakout_success(monkeypatch, *, rvol="2.0", atr_value="3", qty="1"):
    monkeypatch.setattr(br, "relative_volume", lambda bars, period: Decimal(rvol))
    monkeypatch.setattr(br, "atr", lambda bars, period: Decimal(atr_value))
    monkeypatch.setattr(br, "atr_position_size", lambda **kwargs: Decimal(qty))


def test_kraken_breakout_find_resistance_excludes_setup_window():
    strategy = KrakenBreakoutRetestStrategy()
    bars = _breakout_retest_bars()

    assert strategy._find_resistance(bars, 20, 8) == Decimal("100")


def test_kraken_breakout_check_setup_requires_breakout_then_retest():
    strategy = KrakenBreakoutRetestStrategy()
    R = Decimal("100")
    zone = Decimal("0.02")

    valid = [_bar(c=105, l=101), _bar(c=99, l=101)]
    no_breakout = [_bar(c=99, l=99), _bar(c=100, l=99)]
    no_retest_after_breakout = [_bar(c=105, l=101), _bar(c=106, l=105)]

    assert strategy._check_setup(valid, R, zone) is True
    assert strategy._check_setup(no_breakout, R, zone) is False
    assert strategy._check_setup(no_retest_after_breakout, R, zone) is False


def test_kraken_breakout_returns_none_with_too_few_bars():
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(5, price=105), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_returns_none_when_price_below_minimum():
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(29, price=50), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_requires_valid_setup(monkeypatch):
    _patch_breakout_success(monkeypatch)
    strategy = KrakenBreakoutRetestStrategy()
    bars = (
        [_bar(h=100, c=95, v=10_000) for _ in range(20)]
        + [_bar(h=101, l=98, c=99, v=20_000) for _ in range(8)]
        + [_bar(h=108, l=102, c=106, v=50_000)]
    )

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_requires_current_close_above_resistance(monkeypatch):
    _patch_breakout_success(monkeypatch)
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _breakout_retest_bars(current_close=100), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_requires_relative_volume(monkeypatch):
    _patch_breakout_success(monkeypatch, rvol="1.0")
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _breakout_retest_bars(), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_emits_buy_signal(monkeypatch):
    _patch_breakout_success(monkeypatch)
    strategy = KrakenBreakoutRetestStrategy()

    signal = strategy.generate_signal("BTC/USD", _breakout_retest_bars(), ACCOUNT, CASH, NOW)

    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == Decimal("106")
    assert signal.resistance_level == Decimal("100")
    assert signal.stop_price < signal.entry_price
    assert signal.take_profit_price > signal.entry_price
    assert signal.atr_value == Decimal("3")
    assert signal.rvol == Decimal("2.0")
    assert signal.params_snapshot["resistance_level"] == 100.0


# ── Kraken HTF trend-follow breakout ──────────────────────────────────────────


def _patch_trend_success(monkeypatch, *, ema="120", upper="140", chop="30", rvol="2.0", atr_value="5", qty="1"):
    monkeypatch.setattr(tf, "ema_of_closes", lambda bars, period: Decimal(ema))
    monkeypatch.setattr(tf, "donchian_channel", lambda bars, period: (Decimal(upper), Decimal("90")))
    monkeypatch.setattr(tf, "choppiness_index", lambda bars, period: Decimal(chop))
    monkeypatch.setattr(tf, "relative_volume", lambda bars, period: Decimal(rvol))
    monkeypatch.setattr(tf, "atr", lambda bars, period: Decimal(atr_value))
    monkeypatch.setattr(tf, "atr_position_size", lambda **kwargs: Decimal(qty))


def test_kraken_trend_follow_metadata_and_param_merge():
    strategy = KrakenHTFBreakoutStrategy(params={"min_rvol": 1.1})

    assert strategy.VENUE == "kraken"
    assert strategy.PAPER_ONLY is True
    assert strategy.params["min_rvol"] == 1.1
    assert strategy.params["breakout_period"] == tf.DEFAULT_PARAMS["breakout_period"]


def test_kraken_trend_follow_returns_none_with_too_few_bars():
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(5, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_returns_none_when_price_below_minimum():
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=5), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_requires_price_above_ema(monkeypatch):
    _patch_trend_success(monkeypatch, ema="160")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_requires_donchian_breakout(monkeypatch):
    _patch_trend_success(monkeypatch, upper="160")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_requires_non_choppy_market(monkeypatch):
    _patch_trend_success(monkeypatch, chop="70")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_requires_relative_volume(monkeypatch):
    _patch_trend_success(monkeypatch, rvol="1.0")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_emits_buy_signal(monkeypatch):
    _patch_trend_success(monkeypatch)
    strategy = KrakenHTFBreakoutStrategy()

    signal = strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW)

    assert signal is not None
    assert signal.side == "buy"
    assert signal.signal_type == "entry"
    assert signal.entry_price == Decimal("150")
    assert signal.breakout_level == Decimal("140")
    assert signal.stop_price < signal.entry_price
    assert signal.take_profit_price > signal.entry_price
    assert signal.atr_value == Decimal("5")
    assert signal.rvol == Decimal("2.0")
    assert signal.params_snapshot["breakout_level"] == 140.0
    assert signal.params_snapshot["paper_only"] is True


# ── Extra defensive branches to push total coverage over 80% ──────────────────

def test_kraken_mean_reversion_rejects_invalid_bollinger_bands(monkeypatch):
    monkeypatch.setattr(
        mr,
        "bollinger_bands",
        lambda bars, period, multiplier: (Decimal("120"), Decimal("0"), Decimal("0")),
    )
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_rejects_zero_atr(monkeypatch):
    _patch_mean_success(monkeypatch, atr_value="0")
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_mean_reversion_rejects_tiny_quantity(monkeypatch):
    _patch_mean_success(monkeypatch, qty="0.00001")
    strategy = KrakenMeanReversionStrategy()
    bars = _bars(20, price=105) + [_bar(c=99), _bar(c=105)]

    assert strategy.generate_signal("BTC/USD", bars, ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_rejects_zero_atr(monkeypatch):
    _patch_momentum_success(monkeypatch, atr_value="0")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_momentum_rejects_tiny_quantity(monkeypatch):
    _patch_momentum_success(monkeypatch, qty="0.00001")
    strategy = KrakenMomentumStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(25, price=125), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_find_resistance_returns_zero_without_window():
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy._find_resistance([], 20, 8) == Decimal("0")


def test_kraken_breakout_rejects_zero_atr(monkeypatch):
    _patch_breakout_success(monkeypatch, atr_value="0")
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _breakout_retest_bars(), ACCOUNT, CASH, NOW) is None


def test_kraken_breakout_rejects_tiny_quantity(monkeypatch):
    _patch_breakout_success(monkeypatch, qty="0.00001")
    strategy = KrakenBreakoutRetestStrategy()

    assert strategy.generate_signal("BTC/USD", _breakout_retest_bars(), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_rejects_zero_atr(monkeypatch):
    _patch_trend_success(monkeypatch, atr_value="0")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None


def test_kraken_trend_follow_rejects_tiny_quantity(monkeypatch):
    _patch_trend_success(monkeypatch, qty="0.00001")
    strategy = KrakenHTFBreakoutStrategy()

    assert strategy.generate_signal("BTC/USD", _bars(55, price=150), ACCOUNT, CASH, NOW) is None
