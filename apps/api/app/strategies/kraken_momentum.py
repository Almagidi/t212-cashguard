"""
Kraken Crypto Momentum — Donchian Channel Breakout on 1-hour bars.

QUARANTINED — not approved for the active Kraken strategy ladder.

APPROVED = False: _make_engine has no arm for this type; it cannot be
constructed through any active runtime path.  This file is retained for
reference only.  Do not wire it into strategy_runner, seeds, or schema
surfaces without explicit re-approval.

Active ladder (#2, #3):
  kraken_trend_follow    — daily Donchian breakout with EMA50 filter
  kraken_breakout_retest — 4h S/R flip continuation

PAPER_ONLY = True: strategy_runner blocks live execution unconditionally.
No live execution path exists for this strategy family.

Entry conditions (all must be true):
  1. Close > 20-period Donchian upper band  (breakout above channel)
  2. Volume > 1.5x 20-period average        (volume-confirmed breakout)
  3. EMA9 > EMA21                            (trend filter — uptrend only)
  4. Choppiness Index < 61.8                 (trending market, not range-bound)
  5. ATR > 0                                 (instrument is moving)

Position sizing: ATR-based, capped at max_position_pct of account.
Stop: entry - atr_stop_multiplier * ATR
Target: 2R (stop distance * reward_risk_ratio)

Crypto is 24/7 — no session time filter applied.
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
    "donchian_period": 20,
    "ema_fast": 9,
    "ema_slow": 21,
    "min_rvol": 1.5,
    "atr_period": 14,
    "atr_stop_multiplier": 2.0,
    "reward_risk_ratio": 2.0,
    "risk_per_trade_pct": 0.75,
    "max_position_pct": 8.0,
    "max_choppiness": 61.8,
    "min_price": 100.0,  # avoids dust/micro-cap tokens
}


@dataclass
class KrakenMomentumSignal:
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
    atr_value: Decimal = Decimal("0")
    rvol: Decimal = Decimal("1")


class KrakenMomentumStrategy:
    """
    QUARANTINED: not approved for the active Kraken strategy ladder.
    APPROVED = False — not constructible via _make_engine.
    Donchian breakout on Kraken 1-hour crypto bars.
    PAPER_ONLY guard in strategy_runner prevents live execution.
    """

    VENUE = "kraken"
    PAPER_ONLY = True
    APPROVED: bool = False
    DATA_PROVIDER_TYPE = "kraken"
    BAR_INTERVAL_MINUTES = 60
    history_days = 12        # 12 days * 24h = 288 potential 1h bars (< 720 limit)
    max_history_bars = 200
    required_bars = 25       # EMA21 + Donchian20 + ATR14

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
    ) -> KrakenMomentumSignal | None:
        slow = self.params["ema_slow"]
        donchian_period = self.params["donchian_period"]
        min_bars = max(slow + 1, donchian_period + 1, self.params["atr_period"] + 1)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1].close
        if float(current_price) < self.params["min_price"]:
            return None

        # ── Trend filter ──────────────────────────────────────────────────────
        fast_ema = ema_of_closes(bars, self.params["ema_fast"])
        slow_ema = ema_of_closes(bars, slow)
        if fast_ema <= slow_ema:
            return None

        # ── Choppiness filter ─────────────────────────────────────────────────
        chop = choppiness_index(bars, self.params["atr_period"])
        if float(chop) >= self.params["max_choppiness"]:
            return None

        # ── Donchian breakout ─────────────────────────────────────────────────
        # Use bars[:-1] for the channel so the current bar is the breakout bar.
        channel_bars = bars[:-1]
        if len(channel_bars) < donchian_period:
            return None
        upper, _ = donchian_channel(channel_bars, donchian_period)
        if upper <= 0 or current_price <= upper:
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

        confidence = Decimal("0.50")
        if float(rvol) >= 2.5:
            confidence += Decimal("0.15")
        elif float(rvol) >= 1.5:
            confidence += Decimal("0.08")
        if fast_ema > slow_ema * Decimal("1.002"):
            confidence += Decimal("0.10")
        confidence = min(confidence, Decimal("0.90"))

        return KrakenMomentumSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp,
            suggested_quantity=qty,
            confidence=confidence,
            reason=(
                f"Kraken momentum breakout above {float(upper):.2f} "
                f"(price={float(current_price):.2f}, ATR={float(atr_val):.4f}, "
                f"RVOL={float(rvol):.2f}x, chop={float(chop):.1f})"
            ),
            params_snapshot={
                "donchian_upper": float(upper),
                "ema_fast": float(fast_ema),
                "ema_slow": float(slow_ema),
                "atr": float(atr_val),
                "rvol": float(rvol),
                "chop": float(chop),
                "stop": float(stop_price),
                "tp": float(tp),
                "venue": self.VENUE,
                "paper_only": self.PAPER_ONLY,
            },
            atr_value=atr_val,
            rvol=rvol,
        )
