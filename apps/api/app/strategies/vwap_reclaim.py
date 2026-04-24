"""
VWAP Reclaim Strategy.

Setup: Price dips below VWAP, then reclaims it with volume.
This is a mean-reversion / momentum hybrid — works best in trending markets.

Rules:
  1. Price must have been below VWAP earlier in the session
  2. Price reclaims VWAP on a green bar
  3. Volume on the reclaim bar > 1.5x average
  4. ATR stop below the VWAP
  5. Target: VWAP + 1.5 * ATR (tighter than ORB)

When to use:
  - After a dip in an uptrending market
  - Not in gap-up opens (already above VWAP)
  - Works best between 10:30-14:00 ET (after initial ORB window)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    is_tradeable_time,
    market_regime,
    relative_volume,
    vwap,
)

DEFAULT_VWAP_PARAMS: dict[str, Any] = {
    "min_rvol": 1.5,
    "atr_stop_multiplier": 1.5,     # Tighter stop than ORB (VWAP is natural support)
    "reward_risk_ratio_min": 1.5,
    "risk_per_trade_pct": 0.5,      # Smaller size — mean reversion less reliable
    "max_position_pct": 6.0,
    "min_bars_below_vwap": 2,       # Must have been below VWAP for N bars
    "min_price": 5.0,
    "avoid_first_minutes": 60,      # Only after 10:30 ET
    "avoid_last_minutes": 30,
    "avoid_lunch": True,
    "session_open_utc": "14:30",
    "session_close_utc": "21:00",
}


@dataclass
class VWAPSignal:
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
    vwap_value: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")


class VWAPReclaimStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_VWAP_PARAMS, **(params or {})}

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
    ) -> VWAPSignal | None:
        if len(bars) < 20:
            return None

        if not is_tradeable_time(
            current_time_utc,
            session_open_utc=self.params["session_open_utc"],
            session_close_utc=self.params["session_close_utc"],
            avoid_first_minutes=self.params["avoid_first_minutes"],
            avoid_last_minutes=self.params["avoid_last_minutes"],
            avoid_lunch=self.params["avoid_lunch"],
        ):
            return None

        current_bar = bars[-1]
        vwap_val = vwap(bars)
        if vwap_val <= 0:
            return None

        current_price = current_bar.close

        # Price filter
        if float(current_price) < self.params["min_price"]:
            return None

        # The reclaim: current bar closes ABOVE VWAP
        if current_bar.close <= vwap_val:
            return None

        # Previous N bars must have been below VWAP (the "dip")
        n_below = self.params["min_bars_below_vwap"]
        prev_bars = bars[-n_below - 1 : -1]
        if not all(b.close < vwap_val for b in prev_bars):
            return None

        # Volume confirmation
        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        # ATR-based stop (below VWAP)
        atr_val = atr(bars, 14)
        if atr_val <= 0:
            return None

        stop_price = vwap_val - atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
        risk_per_share = current_price - stop_price
        if risk_per_share <= 0:
            return None

        tp = current_price + risk_per_share * Decimal("1.5")
        rr = (tp - current_price) / risk_per_share

        if float(rr) < self.params["reward_risk_ratio_min"]:
            return None

        # Skip choppy markets
        regime = market_regime(bars)
        if regime == "choppy":
            return None

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

        if qty < Decimal("0.01"):
            return None

        confidence = Decimal("0.55")
        if float(rvol) >= 2.0:       confidence += Decimal("0.15")
        if regime == "trending_up":  confidence += Decimal("0.15")
        if float(rr) >= 2.0:         confidence += Decimal("0.10")
        confidence = min(confidence, Decimal("0.90"))

        return VWAPSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp,
            suggested_quantity=qty,
            confidence=confidence,
            reason=(
                f"VWAP reclaim at {current_price:.2f} "
                f"(VWAP={float(vwap_val):.2f}, RVOL={float(rvol):.2f}x, R:R={float(rr):.2f})"
            ),
            params_snapshot={
                "vwap": float(vwap_val),
                "atr": float(atr_val),
                "rvol": float(rvol),
                "stop": float(stop_price),
                "tp": float(tp),
                "rr": float(rr),
                "regime": regime,
            },
            vwap_value=vwap_val,
            atr_value=atr_val,
        )
