"""
Kraken Crypto Higher-Timeframe Breakout — Daily Donchian breakout with EMA50 trend filter.

Strategy #2 of the approved Kraken ladder: higher-timeframe trend-following breakout.

PAPER_ONLY = True: strategy_runner blocks live execution unconditionally.
No live execution path exists for this strategy family.

Entry conditions (all must be true):
  1. Current close > 20-period Donchian upper band computed on bars[:-1]
     (no same-bar leakage: the channel is established before the breakout bar closes)
  2. Price above EMA50 (trend filter — only enter with the prevailing trend)
  3. Choppiness Index < 55 (confirming trending market structure, not range-bound)
  4. Volume > 1.3x 20-period average (participation confirmation)
  5. ATR > 0

Stop: entry - atr_stop_multiplier * ATR (wide stop; daily bars need room to run)
Target: 3R

Daily bars filter intraday noise and capture multi-day crypto trend breakouts.
EMA50 ensures the trade aligns with the medium-term trend direction.
The Donchian upper (computed on prior bars) is the explicit, deterministic breakout level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    choppiness_index,
    donchian_channel,
    ema_of_closes,
    relative_volume,
)

DEFAULT_PARAMS: dict[str, Any] = {
    "breakout_period": 20,
    "ema_trend_period": 50,
    "min_rvol": 1.3,
    "atr_period": 14,
    "atr_stop_multiplier": 3.0,    # wide stop for daily bars
    "reward_risk_ratio": 3.0,      # 3R target — trend trades need room
    "risk_per_trade_pct": 0.5,
    "max_position_pct": 6.0,
    "max_choppiness": 55.0,
    "min_price": 10.0,
}


@dataclass
class KrakenHTFBreakoutSignal:
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
    breakout_level: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")
    rvol: Decimal = Decimal("1")


class KrakenHTFBreakoutStrategy:
    """
    Daily Donchian breakout with EMA50 trend filter on Kraken.
    Strategy #2 of the approved Kraken ladder.
    PAPER_ONLY guard in strategy_runner prevents live execution.
    """

    VENUE = "kraken"
    PAPER_ONLY = True
    DATA_PROVIDER_TYPE = "kraken"
    BAR_INTERVAL_MINUTES = 1440   # daily bars
    history_days = 120
    max_history_bars = 100
    required_bars = 55            # EMA50 + Donchian20 + ATR14 + 1 lookback bar

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
    ) -> KrakenHTFBreakoutSignal | None:
        breakout_period = self.params["breakout_period"]
        ema_period = self.params["ema_trend_period"]
        min_bars = max(ema_period + 1, breakout_period + 2, self.params["atr_period"] + 1)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1].close
        if float(current_price) < self.params["min_price"]:
            return None

        # ── Trend filter: price above EMA50 ──────────────────────────────────
        slow_ema = ema_of_closes(bars, ema_period)
        if current_price <= slow_ema:
            return None

        # ── Donchian breakout (no same-bar leakage) ───────────────────────────
        # Channel computed on bars[:-1] so the current bar is the breakout candidate.
        channel_bars = bars[:-1]
        if len(channel_bars) < breakout_period:
            return None
        upper, _ = donchian_channel(channel_bars, breakout_period)
        if upper <= 0 or current_price <= upper:
            return None

        # ── Choppiness filter ─────────────────────────────────────────────────
        chop = choppiness_index(bars, self.params["atr_period"])
        if float(chop) >= self.params["max_choppiness"]:
            return None

        # ── Volume confirmation ───────────────────────────────────────────────
        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        # ── ATR and signal construction ───────────────────────────────────────
        atr_val = atr(bars, self.params["atr_period"])
        if atr_val <= 0:
            return None

        stop_price = current_price - atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
        risk_per_unit = current_price - stop_price
        if risk_per_unit <= 0:
            return None

        tp = current_price + risk_per_unit * Decimal(str(self.params["reward_risk_ratio"]))

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

        confidence = Decimal("0.55")
        if float(rvol) >= 2.0:
            confidence += Decimal("0.12")
        elif float(rvol) >= 1.3:
            confidence += Decimal("0.06")
        if float(chop) < 38.2:
            confidence += Decimal("0.10")
        confidence = min(confidence, Decimal("0.88"))

        return KrakenHTFBreakoutSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp,
            suggested_quantity=qty,
            confidence=confidence,
            reason=(
                f"Kraken HTF breakout above {float(upper):.2f} "
                f"(price={float(current_price):.2f}, EMA50={float(slow_ema):.2f}, "
                f"chop={float(chop):.1f}, RVOL={float(rvol):.2f}x)"
            ),
            params_snapshot={
                "breakout_level": float(upper),
                "ema50": float(slow_ema),
                "atr": float(atr_val),
                "rvol": float(rvol),
                "chop": float(chop),
                "stop": float(stop_price),
                "tp": float(tp),
                "venue": self.VENUE,
                "paper_only": self.PAPER_ONLY,
            },
            breakout_level=upper,
            atr_value=atr_val,
            rvol=rvol,
        )
