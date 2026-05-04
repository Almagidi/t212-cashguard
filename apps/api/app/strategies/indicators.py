"""
Technical indicator calculations.
All functions are pure — no side effects, fully testable.

Used by all strategy implementations.
Inputs are lists of dicts with OHLCV keys.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import NamedTuple


class Bar(NamedTuple):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


# ── ATR (Average True Range) ──────────────────────────────────────────────────

def true_range(bar: Bar, prev_close: Decimal | None) -> Decimal:
    """Single true range value."""
    hl = bar.high - bar.low
    if prev_close is None:
        return hl
    hc = abs(bar.high - prev_close)
    lc = abs(bar.low - prev_close)
    return max(hl, hc, lc)


def atr(bars: list[Bar], period: int = 14) -> Decimal:
    """
    Wilder's ATR (exponential smoothing).
    Returns ATR of the most recent bar.
    Returns 0 if not enough data.
    """
    if len(bars) < period + 1:
        return Decimal("0")

    trs = []
    for i in range(1, len(bars)):
        trs.append(true_range(bars[i], bars[i - 1].close))

    # Wilder's smoothing: first ATR = simple mean of first N TRs
    atr_val: Decimal = sum(trs[:period]) / Decimal(period)  # type: ignore[assignment]
    for tr in trs[period:]:
        atr_val = (atr_val * (Decimal(period) - 1) + tr) / Decimal(period)  # type: ignore[assignment, operator]

    return Decimal(str(round(float(atr_val), 8)))


def atr_pct(bars: list[Bar], period: int = 14) -> Decimal:
    """ATR as percentage of current price."""
    if not bars:
        return Decimal("0")
    a = atr(bars, period)
    price = bars[-1].close
    if price <= 0:
        return Decimal("0")
    return (a / price * 100).quantize(Decimal("0.01"))


# ── EMA (Exponential Moving Average) ─────────────────────────────────────────

def ema(values: list[Decimal], period: int) -> list[Decimal]:
    """
    EMA of a series. Returns same-length list (first period-1 values are 0).
    """
    if len(values) < period:
        return [Decimal("0")] * len(values)

    result = [Decimal("0")] * (period - 1)
    k = Decimal(str(2)) / Decimal(str(period + 1))

    # Seed with simple mean of first period
    seed = sum(values[:period]) / Decimal(period)  # type: ignore[assignment]
    result.append(seed)

    for v in values[period:]:
        result.append(v * k + result[-1] * (Decimal(1) - k))

    return result


def ema_of_closes(bars: list[Bar], period: int) -> Decimal:
    """EMA of close prices. Returns most recent value."""
    closes = [b.close for b in bars]
    emas = ema(closes, period)
    return emas[-1] if emas else Decimal("0")


# ── VWAP (Volume-Weighted Average Price) ──────────────────────────────────────

def vwap(bars: list[Bar]) -> Decimal:
    """
    Session VWAP from a list of intraday bars (same session).
    All bars should be from the same trading day.
    """
    cum_tp_vol = Decimal("0")
    cum_vol = Decimal("0")
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3
        cum_tp_vol += typical * bar.volume
        cum_vol += bar.volume
    if cum_vol <= 0:
        return Decimal("0")
    return (cum_tp_vol / cum_vol).quantize(Decimal("0.0001"))


def vwap_bands(bars: list[Bar], std_multiplier: Decimal = Decimal("1.5")) -> tuple[Decimal, Decimal, Decimal]:
    """
    VWAP with upper and lower standard deviation bands.
    Returns (vwap, upper_band, lower_band).
    """
    v = vwap(bars)
    if v <= 0 or len(bars) < 2:
        return v, v, v

    # Variance of typical prices around VWAP
    variance = Decimal("0")
    cum_vol = sum(b.volume for b in bars)
    if cum_vol <= 0:
        return v, v, v

    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3
        variance += bar.volume * (typical - v) ** 2

    std_dev = Decimal(str(math.sqrt(float(variance / cum_vol))))
    upper = v + std_multiplier * std_dev
    lower = v - std_multiplier * std_dev
    return v, upper, lower


# ── Volume analysis ───────────────────────────────────────────────────────────

def volume_sma(bars: list[Bar], period: int = 20) -> Decimal:
    """Simple moving average of volume over last N bars."""
    if len(bars) < period:
        return Decimal("0")
    vols = [b.volume for b in bars[-period:]]
    return sum(vols) / Decimal(len(vols))  # type: ignore[return-value]


def relative_volume(bars: list[Bar], period: int = 20) -> Decimal:
    """
    Current bar's volume relative to the N-period average.
    >1.5 = elevated volume (breakout confirmation).
    >3.0 = climactic volume (potential exhaustion).
    """
    avg = volume_sma(bars[:-1], period)
    if avg <= 0:
        return Decimal("1")
    current_vol = bars[-1].volume
    return (current_vol / avg).quantize(Decimal("0.01"))


def is_volume_breakout(bars: list[Bar], min_rvol: float = 1.5) -> bool:
    """
    True if current bar has volume >= min_rvol * average.
    Critical filter: only trade breakouts with elevated volume.
    """
    rvol = relative_volume(bars)
    return float(rvol) >= min_rvol


# ── Trend / regime detection ──────────────────────────────────────────────────

def is_trending_up(bars: list[Bar], fast: int = 9, slow: int = 21) -> bool:
    """
    True if fast EMA > slow EMA (uptrend context).
    Used to filter ORB long-only in bullish trend.
    """
    if len(bars) < slow:
        return False
    fast_ema = ema_of_closes(bars, fast)
    slow_ema = ema_of_closes(bars, slow)
    return fast_ema > slow_ema


def is_trending_down(bars: list[Bar], fast: int = 9, slow: int = 21) -> bool:
    """True if fast EMA < slow EMA (downtrend context)."""
    if len(bars) < slow:
        return False
    fast_ema = ema_of_closes(bars, fast)
    slow_ema = ema_of_closes(bars, slow)
    return fast_ema < slow_ema


def market_regime(bars: list[Bar], atr_period: int = 14) -> str:
    """
    Classify current market regime for a symbol.

    Returns:
      'trending_up'   — clear uptrend, trade longs
      'trending_down' — clear downtrend, avoid longs
      'choppy'        — range-bound, avoid breakout strategies
    """
    if len(bars) < 30:
        return "unknown"

    # Trend direction via EMA
    trending_up = is_trending_up(bars)
    trending_dn = is_trending_down(bars)

    # Choppiness: measure how much price moves vs its range
    # Choppiness Index = 100 * ATR(n) * sqrt(n) / (highest_high - lowest_low)
    n = min(14, len(bars))
    recent = bars[-n:]
    highest = max(b.high for b in recent)
    lowest  = min(b.low for b in recent)
    price_range = highest - lowest
    atr_val = atr(bars, atr_period)

    if price_range <= 0 or atr_val <= 0:
        return "unknown"

    chop = float(100 * float(atr_val) * math.sqrt(n) / float(price_range))
    # Choppiness Index: >61.8 = choppy, <38.2 = trending
    if chop > 61.8:
        return "choppy"
    if trending_up:
        return "trending_up"
    if trending_dn:
        return "trending_down"
    return "neutral"


# ── Gap detection ─────────────────────────────────────────────────────────────

def gap_pct(prev_close: Decimal, today_open: Decimal) -> Decimal:
    """
    Gap percentage from previous close to today's open.
    Positive = gap up, negative = gap down.
    """
    if prev_close <= 0:
        return Decimal("0")
    return ((today_open - prev_close) / prev_close * 100).quantize(Decimal("0.01"))


def is_clean_open(prev_close: Decimal, today_open: Decimal, max_gap_pct: float = 2.0) -> bool:
    """
    True if today's open is within max_gap_pct of yesterday's close.
    Large gaps distort the opening range — avoid them.
    """
    g = abs(float(gap_pct(prev_close, today_open)))
    return g <= max_gap_pct


# ── ATR-based position sizing ─────────────────────────────────────────────────

def atr_position_size(
    account_value: Decimal,
    entry_price: Decimal,
    atr_val: Decimal,
    risk_pct: Decimal,
    atr_stop_multiplier: float = 2.0,
    available_cash: Decimal | None = None,
) -> Decimal:
    """
    ATR-based position sizing.

    Stop distance = atr_val * atr_stop_multiplier
    Risk per share = stop distance
    Position size = (account_value * risk_pct) / risk_per_share

    This is far superior to fixed ORB-low stops because:
    - Scales automatically with instrument volatility
    - Prevents over-sizing in volatile conditions
    - Prevents under-sizing in calm conditions
    """
    if atr_val <= 0 or entry_price <= 0:
        return Decimal("0")

    stop_distance = atr_val * Decimal(str(atr_stop_multiplier))
    risk_dollars = account_value * risk_pct / 100
    qty = risk_dollars / stop_distance

    # Cap by available cash
    if available_cash and available_cash > 0:
        max_by_cash = available_cash / entry_price
        qty = min(qty, max_by_cash)

    return qty.quantize(Decimal("0.01")) if qty > 0 else Decimal("0")


def trailing_stop_price(
    entry_price: Decimal,
    current_price: Decimal,
    atr_val: Decimal,
    side: str,
    atr_multiplier: float = 2.5,
    initial_stop: Decimal | None = None,
) -> Decimal:
    """
    Calculate current trailing stop price.

    For longs: stop = max(initial_stop, current_price - ATR*multiplier)
    The stop only moves up, never down — it "trails" the price.
    """
    trail_distance = atr_val * Decimal(str(atr_multiplier))

    if side == "buy":
        dynamic_stop = current_price - trail_distance
        if initial_stop is not None:
            return max(initial_stop, dynamic_stop)
        return dynamic_stop
    else:
        dynamic_stop = current_price + trail_distance
        if initial_stop is not None:
            return min(initial_stop, dynamic_stop)
        return dynamic_stop


# ── Choppiness Index (standalone) ────────────────────────────────────────────

def choppiness_index(bars: list[Bar], period: int = 14) -> Decimal:
    """
    Choppiness Index over `period` bars.
    Range: 0–100.
      > 61.8  →  choppy / ranging (avoid breakout strategies)
      < 38.2  →  strongly trending (favour momentum / ORB)
    Formula: 100 × ATR(n) × √n / (highest_high − lowest_low)
    """
    if len(bars) < period + 1:
        return Decimal("50")  # neutral default when not enough data
    recent = bars[-period:]
    highest = max(b.high for b in recent)
    lowest  = min(b.low  for b in recent)
    price_range = highest - lowest
    atr_val = atr(bars, period)
    if price_range <= 0 or atr_val <= 0:
        return Decimal("50")
    chop = float(100 * float(atr_val) * math.sqrt(period) / float(price_range))
    return Decimal(str(round(min(max(chop, 0.0), 100.0), 2)))


# ── Adaptive ATR multiplier ───────────────────────────────────────────────────

def adaptive_atr_multiplier(
    bars: list[Bar],
    base_multiplier: float,
    atr_period: int = 14,
    lookback: int = 20,
    floor: float = 1.5,
    ceiling: float = 4.0,
) -> float:
    """
    Scale the ATR stop/trail multiplier by the ratio of current ATR to its
    recent average (lookback bars).  This produces:
      • Wider stops in high-volatility regimes  (fewer whipsaw stop-outs)
      • Tighter stops in calm trending regimes  (faster profit lock-in)

    Formula:  mult = base × (current_atr / avg_atr_lookback)
    Clamped to [floor, ceiling].

    Academic basis: Kaufman (1995) Adaptive Moving Average; Wilder ATR scaling.
    """
    if len(bars) < max(atr_period + 1, lookback + 1):
        return base_multiplier

    current_atr = float(atr(bars, atr_period))
    if current_atr <= 0:
        return base_multiplier

    # Rolling average ATR over the lookback window
    atr_history: list[float] = []
    for i in range(lookback):
        window = bars[:-(i)] if i > 0 else bars
        if len(window) >= atr_period + 1:
            atr_history.append(float(atr(window, atr_period)))

    if not atr_history:
        return base_multiplier

    avg_atr = sum(atr_history) / len(atr_history)
    if avg_atr <= 0:
        return base_multiplier

    mult = base_multiplier * (current_atr / avg_atr)
    return float(min(max(mult, floor), ceiling))


# ── Fractional Kelly position sizing ─────────────────────────────────────────

def kelly_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly criterion — optimal bet size as a fraction of bankroll.

    Full Kelly:  f* = (b·p − q) / b  where b = avg_win/avg_loss, p = win_rate, q = 1−p
    Fractional:  f  = fraction × f*

    Returns a value in [0, 1] representing the fraction of capital to risk.
    Returns 0 if the edge is negative (do not trade).

    Arguments:
        win_rate  – historical win rate (0.0–1.0)
        avg_win   – average winning trade return (positive, e.g. 0.015 for 1.5%)
        avg_loss  – average losing trade return (positive magnitude, e.g. 0.008)
        fraction  – Kelly fraction to use (0.25 = quarter Kelly; conservative)

    Academic reference: Kelly (1956); MacLean, Thorp & Ziemba (2011).
    """
    if avg_loss <= 0 or win_rate <= 0 or avg_win <= 0:
        return 0.0

    b = avg_win / avg_loss           # win/loss ratio
    p = win_rate
    q = 1.0 - win_rate
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        return 0.0                   # negative edge — do not trade

    return float(min(fraction * full_kelly, 1.0))


def kelly_position_size(
    account_value: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    fraction: float = 0.25,
    available_cash: Decimal | None = None,
) -> Decimal:
    """
    Position size using fractional Kelly as the risk percentage.
    Falls back to ATR-based sizing if Kelly fraction produces zero
    (negative edge detected).

    The result is the number of shares/units to buy.
    """
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0 or entry_price <= 0:
        return Decimal("0")

    kf = kelly_fraction(win_rate, avg_win_pct, avg_loss_pct, fraction)
    if kf <= 0:
        return Decimal("0")

    risk_dollars = account_value * Decimal(str(kf))
    qty = risk_dollars / risk_per_share

    if available_cash and available_cash > 0:
        max_by_cash = available_cash / entry_price
        qty = min(qty, max_by_cash)

    return qty.quantize(Decimal("0.01")) if qty > 0 else Decimal("0")


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def bollinger_bands(
    bars: list[Bar],
    period: int = 20,
    std_multiplier: float = 2.0,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Bollinger Bands: (upper, middle, lower).
    middle = SMA(period), bands = middle ± std_multiplier × std_dev(period).
    Returns (price, price, price) when there is insufficient data.
    """
    if len(bars) < period:
        price = bars[-1].close if bars else Decimal("0")
        return price, price, price
    closes = [b.close for b in bars[-period:]]
    middle: Decimal = sum(closes) / Decimal(period)  # type: ignore[assignment]
    variance: Decimal = sum((c - middle) ** 2 for c in closes) / Decimal(period)  # type: ignore[assignment]
    std_dev = Decimal(str(math.sqrt(float(variance))))
    mult = Decimal(str(std_multiplier))
    return middle + mult * std_dev, middle, middle - mult * std_dev


# ── Donchian Channel ──────────────────────────────────────────────────────────

def donchian_channel(bars: list[Bar], period: int = 20) -> tuple[Decimal, Decimal]:
    """
    Donchian Channel: (upper, lower) = N-period high/low.
    close > upper signals momentum breakout; close < lower signals breakdown.
    Returns (0, 0) when insufficient data.
    """
    if len(bars) < period:
        return Decimal("0"), Decimal("0")
    recent = bars[-period:]
    return max(b.high for b in recent), min(b.low for b in recent)


# ── RSI (Relative Strength Index) ────────────────────────────────────────────

def rsi(bars: list[Bar], period: int = 14) -> Decimal:
    """
    Wilder's RSI.  Range 0–100.  Returns 50 (neutral) when insufficient data.
    """
    if len(bars) < period + 1:
        return Decimal("50")
    closes = [b.close for b in bars]
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(-diff)
    avg_gain: Decimal = sum(gains[:period]) / Decimal(period)  # type: ignore[assignment]
    avg_loss: Decimal = sum(losses[:period]) / Decimal(period)  # type: ignore[assignment]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (Decimal(period) - 1) + gains[i]) / Decimal(period)
        avg_loss = (avg_loss * (Decimal(period) - 1) + losses[i]) / Decimal(period)
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return (Decimal("100") - Decimal("100") / (1 + rs)).quantize(Decimal("0.01"))


# ── Session time filters ──────────────────────────────────────────────────────

def is_tradeable_time(
    current_time_utc_hhmm: str,
    session_open_utc: str = "14:30",   # 09:30 ET in UTC
    session_close_utc: str = "21:00",  # 16:00 ET in UTC
    avoid_first_minutes: int = 5,
    avoid_last_minutes: int = 30,
    avoid_lunch: bool = True,
) -> bool:
    """
    Return True only during clean trading windows:
    - Skip the first N minutes (whipsaw)
    - Skip the last N minutes (illiquid)
    - Skip lunch hour 12:00-13:30 ET (choppy, low volume)
    """
    h, m = map(int, current_time_utc_hhmm.split(":"))
    current_mins = h * 60 + m

    open_h, open_m = map(int, session_open_utc.split(":"))
    close_h, close_m = map(int, session_close_utc.split(":"))
    open_mins  = open_h * 60 + open_m
    close_mins = close_h * 60 + close_m

    # Outside session
    if current_mins < open_mins or current_mins >= close_mins:
        return False

    # First N minutes — high volatility, avoid
    if current_mins < open_mins + avoid_first_minutes:
        return False

    # Last N minutes — illiquid, avoid
    if current_mins >= close_mins - avoid_last_minutes:
        return False

    # Lunch chop: 17:00-18:30 UTC = 12:00-13:30 ET
    if avoid_lunch:
        if 17 * 60 <= current_mins < 18 * 60 + 30:
            return False

    return True
