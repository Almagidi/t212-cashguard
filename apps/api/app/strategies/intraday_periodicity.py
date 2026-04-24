"""
Intraday periodicity continuation strategy.

Research motivation:
  Some symbols exhibit persistent strength in the same intraday time buckets
  across recent sessions. This implementation only trades when a positive
  same-slot history lines up with current-session confirmation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.strategies.indicators import (
    Bar,
    atr,
    atr_position_size,
    market_regime,
    relative_volume,
    vwap,
)

if TYPE_CHECKING:
    from datetime import date, datetime

DEFAULT_INTRADAY_PERIODICITY_PARAMS: dict[str, Any] = {
    "slot_minutes": 30,
    "session_open_utc": "14:30",
    "session_close_utc": "21:00",
    "trade_window_start_utc": "17:30",
    "trade_window_end_utc": "20:00",
    "min_history_sessions": 4,
    "min_avg_slot_return_pct": 0.08,
    "min_positive_ratio": 0.60,
    "min_live_slot_return_pct": 0.05,
    "min_rvol": 1.05,
    "min_price": 5.0,
    "atr_stop_multiplier": 1.1,
    "reward_risk_ratio_min": 1.25,
    "risk_per_trade_pct": 0.35,
    "max_position_pct": 4.0,
    "require_above_vwap": True,
}


@dataclass
class IntradayPeriodicitySignal:
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
    avg_slot_return_pct: Decimal = Decimal("0")
    live_slot_return_pct: Decimal = Decimal("0")
    positive_ratio: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")


class IntradayPeriodicityStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_INTRADAY_PERIODICITY_PARAMS, **(params or {})}
        self.required_bars = 24
        self.history_days = 10
        self.max_history_bars = 640

    def _parse_minutes(self, value: str) -> int:
        hours, minutes = map(int, value.split(":"))
        return hours * 60 + minutes

    def _current_slot_index(self, current_time_utc: str) -> int | None:
        current_minutes = self._parse_minutes(current_time_utc)
        trade_start = self._parse_minutes(self.params["trade_window_start_utc"])
        trade_end = self._parse_minutes(self.params["trade_window_end_utc"])
        session_start = self._parse_minutes(self.params["session_open_utc"])
        if current_minutes < trade_start or current_minutes > trade_end:
            return None
        elapsed = current_minutes - session_start
        if elapsed < 0:
            return None
        slot_minutes = int(self.params["slot_minutes"])
        minutes_into_slot = elapsed % slot_minutes
        if minutes_into_slot < max(slot_minutes - 5, 0):
            return None
        return elapsed // slot_minutes

    def _slot_returns(
        self,
        history_bars: list[Bar],
        history_bar_times: list[datetime],
        slot_index: int,
        current_session_date: date | None,
    ) -> list[Decimal]:
        if not history_bars or len(history_bars) != len(history_bar_times):
            return []

        slot_minutes = int(self.params["slot_minutes"])
        session_start = self._parse_minutes(self.params["session_open_utc"])
        grouped: dict[date, list[tuple[datetime, Bar]]] = defaultdict(list)
        for bar_time, bar in zip(history_bar_times, history_bars, strict=True):
            grouped[bar_time.date()].append((bar_time, bar))

        slot_returns: list[Decimal] = []
        for session_date, rows in sorted(grouped.items()):
            if current_session_date is not None and session_date >= current_session_date:
                continue

            slot_bars: list[Bar] = []
            for bar_time, bar in rows:
                minute_of_day = bar_time.hour * 60 + bar_time.minute
                elapsed = minute_of_day - session_start
                if elapsed < 0:
                    continue
                bucket = elapsed // slot_minutes
                if bucket == slot_index:
                    slot_bars.append(bar)

            if len(slot_bars) < 2:
                continue

            slot_open = slot_bars[0].open
            slot_close = slot_bars[-1].close
            if slot_open <= 0:
                continue
            slot_returns.append(((slot_close - slot_open) / slot_open * 100).quantize(Decimal("0.01")))

        return slot_returns

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None = None,
        bar_times: list[datetime] | None = None,
        history_bars: list[Bar] | None = None,
        history_bar_times: list[datetime] | None = None,
    ) -> IntradayPeriodicitySignal | None:
        del prev_close

        if len(bars) < self.required_bars:
            return None

        slot_index = self._current_slot_index(current_time_utc)
        if slot_index is None:
            return None

        current_price = bars[-1].close
        if float(current_price) < self.params["min_price"]:
            return None

        vwap_value = vwap(bars)
        if vwap_value <= 0:
            return None
        if self.params.get("require_above_vwap", True) and current_price <= vwap_value:
            return None

        slot_minutes = int(self.params["slot_minutes"])
        bars_per_slot = max(2, slot_minutes // 5)
        slot_slice = bars[-bars_per_slot:]
        slot_open = slot_slice[0].open
        if slot_open <= 0:
            return None
        live_slot_return_pct = ((current_price - slot_open) / slot_open * 100).quantize(Decimal("0.01"))
        if float(live_slot_return_pct) < self.params["min_live_slot_return_pct"]:
            return None

        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        regime = market_regime(bars)
        if regime in {"trending_down", "choppy"}:
            return None

        current_session_date = bar_times[-1].date() if bar_times else None
        historical_returns = self._slot_returns(
            history_bars or [],
            history_bar_times or [],
            slot_index,
            current_session_date,
        )
        if len(historical_returns) < int(self.params["min_history_sessions"]):
            return None

        avg_slot_return_pct = (sum(historical_returns) / len(historical_returns)).quantize(Decimal("0.01"))
        positive_ratio = Decimal(str(round(
            sum(1 for item in historical_returns if item > 0) / len(historical_returns),
            4,
        )))
        if float(avg_slot_return_pct) < self.params["min_avg_slot_return_pct"]:
            return None
        if float(positive_ratio) < self.params["min_positive_ratio"]:
            return None

        atr_value = atr(bars, 14)
        if atr_value <= 0:
            return None

        stop_price = max(
            current_price - atr_value * Decimal(str(self.params["atr_stop_multiplier"])),
            vwap_value * Decimal("0.997"),
        )
        risk_per_share = current_price - stop_price
        if risk_per_share <= 0:
            return None

        take_profit_price = current_price + risk_per_share * Decimal(str(self.params["reward_risk_ratio_min"]))
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

        confidence = Decimal("0.51")
        if float(avg_slot_return_pct) >= 0.15:
            confidence += Decimal("0.07")
        if float(positive_ratio) >= 0.7:
            confidence += Decimal("0.08")
        if float(live_slot_return_pct) >= 0.15:
            confidence += Decimal("0.06")
        if regime == "trending_up":
            confidence += Decimal("0.06")
        confidence = min(confidence, Decimal("0.85"))

        return IntradayPeriodicitySignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            suggested_quantity=quantity,
            confidence=confidence,
            reason=(
                f"Intraday periodicity continuation: slot {slot_index} averaged "
                f"{float(avg_slot_return_pct):.2f}% across {len(historical_returns)} sessions "
                f"with {float(positive_ratio) * 100:.0f}% positive frequency."
            ),
            params_snapshot={
                "slot_index": slot_index,
                "avg_slot_return_pct": float(avg_slot_return_pct),
                "live_slot_return_pct": float(live_slot_return_pct),
                "positive_ratio": float(positive_ratio),
                "history_sessions": len(historical_returns),
                "vwap": float(vwap_value),
                "atr": float(atr_value),
                "rvol": float(rvol),
                "regime": regime,
            },
            avg_slot_return_pct=avg_slot_return_pct,
            live_slot_return_pct=live_slot_return_pct,
            positive_ratio=positive_ratio,
            atr_value=atr_value,
        )
