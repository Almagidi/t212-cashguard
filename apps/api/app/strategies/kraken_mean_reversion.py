# ── QUARANTINED — NOT AN APPROVED KRAKEN STRATEGY ─────────────────────────────
# Mean reversion is not on the approved Kraken strategy ladder.
# This file is kept for reference only. It must not be registered in
# strategy_runner._make_engine() and must not be seeded as an active strategy.
# APPROVED = False is enforced on the class below.
# ──────────────────────────────────────────────────────────────────────────────
"""
Kraken Crypto Mean Reversion — Bollinger Band Snapback on 4-hour bars.

QUARANTINED: not on the approved Kraken strategy ladder.
PAPER_ONLY = True: strategy_runner blocks live execution unconditionally.
No live execution path exists for this strategy family.

Entry conditions (all must be true):
  1. Previous bar closed AT OR BELOW the lower Bollinger Band (oversold)
  2. Current bar closes ABOVE the lower Bollinger Band (reclaim / snapback)
  3. Volume on the reclaim bar > 1.3x 20-period average (confirmation)
  4. RSI(14) < 45 at entry (still in recovery, not already overbought)
  5. ATR > 0

Target: middle band (SMA20).
Stop: entry - atr_stop_multiplier * ATR.

4-hour bars capture medium-term crypto swings while filtering intraday noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    bollinger_bands,
    relative_volume,
    rsi,
)

DEFAULT_PARAMS: dict[str, Any] = {
    "bb_period": 20,
    "bb_std_multiplier": 2.0,
    "rsi_period": 14,
    "rsi_entry_max": 45,      # entry only when RSI < 45 (still recovering)
    "min_rvol": 1.3,
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
    "risk_per_trade_pct": 0.5,
    "max_position_pct": 6.0,
    "min_price": 10.0,
}


@dataclass
class KrakenMeanReversionSignal:
    ticker: str
    side: str
    signal_type: str
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    suggested_quantity: Decimal
    confidence: Decimal
    reason: str
    params_snapshot: dict[str, Any] = field(default_factory=dict)
    bb_middle: Decimal = Decimal("0")
    bb_lower: Decimal = Decimal("0")
    rsi_value: Decimal = Decimal("50")


class KrakenMeanReversionStrategy:
    """
    Bollinger Band snapback on Kraken 4-hour crypto bars.
    QUARANTINED: not on the approved Kraken strategy ladder.
    PAPER_ONLY guard in strategy_runner prevents live execution.
    """

    VENUE = "kraken"
    PAPER_ONLY = True
    APPROVED: bool = False  # not on the approved Kraken strategy ladder
    DATA_PROVIDER_TYPE = "kraken"
    BAR_INTERVAL_MINUTES = 240
    history_days = 30        # 30 days * 6 4h-bars = 180 bars (< 720 Kraken limit)
    max_history_bars = 100
    required_bars = 22       # BB20 + RSI14 + ATR14

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_PARAMS, **(params or {})}

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,  # accepted for interface parity; unused (24/7 crypto)
        **_: Any,
    ) -> KrakenMeanReversionSignal | None:
        period = self.params["bb_period"]
        min_bars = max(period + 2, self.params["rsi_period"] + 2, self.params["atr_period"] + 2)
        if len(bars) < min_bars:
            return None

        current_bar = bars[-1]
        prev_bar = bars[-2]
        current_price = current_bar.close

        if float(current_price) < self.params["min_price"]:
            return None

        # Bollinger Bands computed on all-but-current bar so the current bar is
        # the "event" bar (the snapback), not part of the band calculation.
        bb_bars = bars[:-1]
        upper, middle, lower = bollinger_bands(
            bb_bars, period, self.params["bb_std_multiplier"]
        )
        if lower <= 0 or middle <= 0:
            return None

        # ── Condition 1: previous bar at or below lower band ──────────────────
        if prev_bar.close > lower:
            return None

        # ── Condition 2: current bar reclaims above lower band ────────────────
        if current_price <= lower:
            return None

        # ── Condition 3: volume confirmation ──────────────────────────────────
        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        # ── Condition 4: RSI still in recovery territory ──────────────────────
        rsi_val = rsi(bars, self.params["rsi_period"])
        if float(rsi_val) >= self.params["rsi_entry_max"]:
            return None

        # ── ATR-based stop ────────────────────────────────────────────────────
        atr_val = atr(bars, self.params["atr_period"])
        if atr_val <= 0:
            return None

        stop_price = current_price - atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
        risk_per_unit = current_price - stop_price
        if risk_per_unit <= 0:
            return None

        tp = middle  # target: mean reversion to SMA20
        if tp <= current_price:
            return None

        rr = (tp - current_price) / risk_per_unit

        qty = atr_position_size(
            account_value=account_value,
            entry_price=current_price,
            atr_val=atr_val,
            risk_pct=Decimal(str(self.params["risk_per_trade_pct"])),
            atr_stop_multiplier=self.params["atr_stop_multiplier"],
            available_cash=available_cash,
        )
        max_by_pct = account_value * Decimal(str(self.params["max_position_pct"])) / 100 / current_price
        qty = min(qty, max_by_pct)
        if qty < Decimal("0.0001"):
            return None

        confidence = Decimal("0.50")
        if float(rvol) >= 2.0:
            confidence += Decimal("0.12")
        elif float(rvol) >= 1.3:
            confidence += Decimal("0.06")
        if float(rsi_val) < 35:
            confidence += Decimal("0.10")  # deeper oversold = stronger snapback case
        if float(rr) >= 1.5:
            confidence += Decimal("0.08")
        confidence = min(confidence, Decimal("0.85"))

        return KrakenMeanReversionSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp,
            suggested_quantity=qty,
            confidence=confidence,
            reason=(
                f"Kraken BB snapback: {float(current_price):.2f} reclaimed "
                f"lower band {float(lower):.2f} "
                f"(RSI={float(rsi_val):.1f}, RVOL={float(rvol):.2f}x, "
                f"target SMA20={float(middle):.2f})"
            ),
            params_snapshot={
                "bb_upper": float(upper),
                "bb_middle": float(middle),
                "bb_lower": float(lower),
                "rsi": float(rsi_val),
                "rvol": float(rvol),
                "atr": float(atr_val),
                "stop": float(stop_price),
                "tp": float(tp),
                "rr": float(rr),
                "venue": self.VENUE,
                "paper_only": self.PAPER_ONLY,
            },
            bb_middle=middle,
            bb_lower=lower,
            rsi_value=rsi_val,
        )
