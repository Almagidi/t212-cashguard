"""
Kraken Crypto Breakout-Retest / S/R Flip Continuation — 4-hour bars.

Strategy #3 of the approved Kraken ladder.

PAPER_ONLY = True: strategy_runner blocks live execution unconditionally.
No live execution path exists for this strategy family.

This strategy requires a 3-phase multi-bar setup:

Phase 1 — Prior resistance identification (resistance_lookback bars before the setup window):
  - Resistance level R = highest high over the resistance lookback window.
  - This window ends strictly before the setup window, so the breakout bars
    cannot inflate R.

Phase 2 — Breakout detection (within breakout_window bars, before current bar):
  - At least one bar in the setup window must have closed above R.
  - Scanned sequentially — the breakout bar is the first close > R.

Phase 3 — Retest confirmation (a bar after the breakout bar, still in setup window):
  - After the breakout bar, at least one bar's LOW must have come within
    retest_zone_pct of R (price pulled back toward the broken level from above).
  - That bar's CLOSE must have held at or above R * (1 - retest_zone_pct)
    (former resistance acting as support — the S/R flip held).

Phase 4 — Current bar continuation signal:
  - Current bar (bars[-1]) closes above R.
  - Volume > min_rvol * 20-period average.
  - ATR > 0.

Anti-cheat guards:
  - Resistance window, setup window, and current bar are fully non-overlapping.
  - Breakout bar must precede retest bar (sequential scan enforced, not same bar).
  - Current bar is never part of the resistance or setup window computation.

Stop: entry - atr_stop_multiplier * ATR
Target: 2R

4-hour bars capture intraday-to-multiday crypto swing setups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    relative_volume,
)

DEFAULT_PARAMS: dict[str, Any] = {
    "resistance_lookback": 20,   # bars defining the prior resistance zone
    "breakout_window": 8,        # bars in which breakout + retest must occur (before current)
    "retest_zone_pct": 0.02,     # 2% band around R counts as a valid retest touch
    "min_rvol": 1.3,
    "atr_period": 14,
    "atr_stop_multiplier": 2.0,
    "reward_risk_ratio": 2.0,
    "risk_per_trade_pct": 0.5,
    "max_position_pct": 6.0,
    "min_price": 100.0,
}


@dataclass
class KrakenBreakoutRetestSignal:
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
    resistance_level: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")
    rvol: Decimal = Decimal("1")


class KrakenBreakoutRetestStrategy:
    """
    Breakout-retest / S/R flip continuation on Kraken 4-hour crypto bars.
    Strategy #3 of the approved Kraken ladder.
    PAPER_ONLY guard in strategy_runner prevents live execution.
    """

    VENUE = "kraken"
    PAPER_ONLY = True
    DATA_PROVIDER_TYPE = "kraken"
    BAR_INTERVAL_MINUTES = 240   # 4-hour bars
    history_days = 20
    max_history_bars = 120
    required_bars = 30           # resistance_lookback(20) + breakout_window(8) + 2

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_PARAMS, **(params or {})}

    def _find_resistance(self, bars: list[Bar], r_lookback: int, b_window: int) -> Decimal:
        """
        Highest high over bars[-(r_lookback + b_window + 1) : -(b_window + 1)].
        This window ends before the setup window begins — the breakout bars
        cannot raise the resistance level.
        """
        start = -(r_lookback + b_window + 1)
        end = -(b_window + 1)
        resistance_bars = bars[start:end]
        if not resistance_bars:
            return Decimal("0")
        return max(b.high for b in resistance_bars)

    def _check_setup(
        self,
        setup_bars: list[Bar],
        R: Decimal,
        zone: Decimal,
    ) -> bool:
        """
        Sequential scan of setup_bars for:
          1. Breakout bar: first bar whose close > R.
          2. Retest bar: a bar after the breakout bar where
             low <= R*(1+zone) [came back near level] AND
             close >= R*(1-zone) [held above level — S/R flip confirmed].
        Both must be present and in that order. Returns True if satisfied.
        """
        breakout_idx: int | None = None
        for i, bar in enumerate(setup_bars):
            if bar.close > R:
                breakout_idx = i
                break

        if breakout_idx is None:
            return False

        for bar in setup_bars[breakout_idx + 1:]:
            if bar.low <= R * (1 + zone) and bar.close >= R * (1 - zone):
                return True

        return False

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,  # accepted for interface parity; unused (24/7 crypto)
        **_: Any,
    ) -> KrakenBreakoutRetestSignal | None:
        r_lookback = self.params["resistance_lookback"]
        b_window = self.params["breakout_window"]
        required = r_lookback + b_window + 1  # minimum bars including current bar
        if len(bars) < required:
            return None

        current_price = bars[-1].close
        if float(current_price) < self.params["min_price"]:
            return None

        # ── Phase 1: establish resistance ─────────────────────────────────────
        R = self._find_resistance(bars, r_lookback, b_window)
        if R <= 0:
            return None

        # ── Phase 2 + 3: breakout then retest in setup window ─────────────────
        # setup_bars = bars[-(b_window + 1):-1] — excludes current bar entirely.
        setup_bars = bars[-(b_window + 1):-1]
        zone = Decimal(str(self.params["retest_zone_pct"]))
        if not self._check_setup(setup_bars, R, zone):
            return None

        # ── Phase 4: current bar closes above R ───────────────────────────────
        if current_price <= R:
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
        confidence = min(confidence, Decimal("0.85"))

        return KrakenBreakoutRetestSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp,
            suggested_quantity=qty,
            confidence=confidence,
            reason=(
                f"Kraken breakout-retest: S/R flip at {float(R):.2f} confirmed "
                f"(price={float(current_price):.2f}, RVOL={float(rvol):.2f}x)"
            ),
            params_snapshot={
                "resistance_level": float(R),
                "atr": float(atr_val),
                "rvol": float(rvol),
                "stop": float(stop_price),
                "tp": float(tp),
                "venue": self.VENUE,
                "paper_only": self.PAPER_ONLY,
            },
            resistance_level=R,
            atr_value=atr_val,
            rvol=rvol,
        )
