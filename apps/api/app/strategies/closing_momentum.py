"""
Closing momentum strategy.

Research motivation:
  The first half-hour return often carries information about the closing
  auction direction. This implementation keeps the live version conservative:
  it only trades long in liquid names when early-session strength persists into
  the final half-hour, volume remains supportive, and price is above VWAP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    gap_pct,
    market_regime,
    relative_volume,
    vwap,
)

DEFAULT_CLOSING_MOMENTUM_PARAMS: dict[str, Any] = {
    "opening_window_minutes": 30,
    "candle_interval_minutes": 5,
    "trade_window_start_utc": "20:30",
    "trade_window_end_utc": "20:55",
    "session_open_utc": "14:30",
    "session_close_utc": "21:00",
    "min_opening_return_pct": 0.35,
    "min_day_return_pct": 0.25,
    "max_gap_pct": 3.0,
    "min_rvol": 1.15,
    "min_price": 5.0,
    "atr_stop_multiplier": 1.2,
    "reward_risk_ratio_min": 1.3,
    "risk_per_trade_pct": 0.45,
    "max_position_pct": 5.0,
    "require_above_vwap": True,
}


@dataclass
class ClosingMomentumSignal:
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
    opening_return_pct: Decimal = Decimal("0")
    day_return_pct: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")
    vwap_value: Decimal = Decimal("0")


class ClosingMomentumStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_CLOSING_MOMENTUM_PARAMS, **(params or {})}
        self.required_bars = max(24, int(self.params["opening_window_minutes"]) // int(self.params["candle_interval_minutes"]) + 12)
        self.history_days = 5
        self.max_history_bars = 180

    def _parse_minutes(self, value: str) -> int:
        hours, minutes = map(int, value.split(":"))
        return hours * 60 + minutes

    def _in_trade_window(self, current_time_utc: str) -> bool:
        current_minutes = self._parse_minutes(current_time_utc)
        return self._parse_minutes(self.params["trade_window_start_utc"]) <= current_minutes <= self._parse_minutes(self.params["trade_window_end_utc"])

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None = None,
        bar_times: list[Any] | None = None,
    ) -> ClosingMomentumSignal | None:
        del bar_times

        if len(bars) < self.required_bars or prev_close is None or prev_close <= 0:
            return None
        if not self._in_trade_window(current_time_utc):
            return None

        opening_bar_count = max(
            2,
            int(self.params["opening_window_minutes"]) // int(self.params["candle_interval_minutes"]),
        )
        if len(bars) < opening_bar_count + 2:
            return None

        session_open = bars[0].open
        current_bar = bars[-1]
        current_price = current_bar.close
        if float(current_price) < self.params["min_price"]:
            return None

        session_gap = abs(float(gap_pct(prev_close, session_open)))
        if session_gap > float(self.params["max_gap_pct"]):
            return None

        opening_close = bars[opening_bar_count - 1].close
        opening_return_pct = ((opening_close - prev_close) / prev_close * 100).quantize(Decimal("0.01"))
        day_return_pct = ((current_price - session_open) / session_open * 100).quantize(Decimal("0.01")) if session_open > 0 else Decimal("0")
        if float(opening_return_pct) < self.params["min_opening_return_pct"]:
            return None
        if float(day_return_pct) < self.params["min_day_return_pct"]:
            return None

        vwap_value = vwap(bars)
        if vwap_value <= 0:
            return None
        if self.params.get("require_above_vwap", True) and current_price <= vwap_value:
            return None

        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        regime = market_regime(bars)
        if regime in {"trending_down", "choppy"}:
            return None

        atr_value = atr(bars, 14)
        if atr_value <= 0:
            return None

        stop_price = max(
            current_price - atr_value * Decimal(str(self.params["atr_stop_multiplier"])),
            vwap_value * Decimal("0.9975"),
        )
        risk_per_share = current_price - stop_price
        if risk_per_share <= 0:
            return None

        take_profit_price = current_price + (risk_per_share * Decimal(str(self.params["reward_risk_ratio_min"])))
        reward_risk_ratio = (take_profit_price - current_price) / risk_per_share
        if float(reward_risk_ratio) < self.params["reward_risk_ratio_min"]:
            return None

        quantity = atr_position_size(
            account_value=account_value,
            entry_price=current_price,
            atr_val=atr_value,
            risk_pct=Decimal(str(self.params["risk_per_trade_pct"])),
            atr_stop_multiplier=self.params["atr_stop_multiplier"],
            available_cash=available_cash,
        )
        max_by_pct = account_value * Decimal(str(self.params["max_position_pct"])) / 100 / current_price
        quantity = min(quantity, max_by_pct)
        if quantity < Decimal("0.01"):
            return None

        confidence = Decimal("0.54")
        if float(opening_return_pct) >= 0.75:
            confidence += Decimal("0.08")
        if float(day_return_pct) >= 0.6:
            confidence += Decimal("0.06")
        if float(rvol) >= 1.5:
            confidence += Decimal("0.07")
        if regime == "trending_up":
            confidence += Decimal("0.08")
        confidence = min(confidence, Decimal("0.88"))

        return ClosingMomentumSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            suggested_quantity=quantity,
            confidence=confidence,
            reason=(
                f"Closing momentum confirmed: first 30m return {float(opening_return_pct):.2f}%, "
                f"day return {float(day_return_pct):.2f}%, RVOL {float(rvol):.2f}x."
            ),
            params_snapshot={
                "opening_return_pct": float(opening_return_pct),
                "day_return_pct": float(day_return_pct),
                "rvol": float(rvol),
                "vwap": float(vwap_value),
                "atr": float(atr_value),
                "regime": regime,
            },
            opening_return_pct=opening_return_pct,
            day_return_pct=day_return_pct,
            atr_value=atr_value,
            vwap_value=vwap_value,
        )
