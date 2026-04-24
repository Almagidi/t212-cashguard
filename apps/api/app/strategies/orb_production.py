"""
Opening Range Breakout — Production grade.
All filters, ATR sizing, trailing stops, partial exits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.strategies.indicators import (
    Bar,
    atr,
    adaptive_atr_multiplier,
    atr_position_size,
    choppiness_index,
    gap_pct,
    is_tradeable_time,
    is_trending_down,
    is_trending_up,
    kelly_position_size,
    market_regime,
    relative_volume,
    trailing_stop_price,
)

DEFAULT_PARAMS: dict[str, Any] = {
    "orb_minutes": 15,
    "candle_interval_minutes": 5,
    "min_range_pct": 0.15,
    "max_range_pct": 3.5,
    "min_rvol": 1.5,
    "max_gap_pct": 2.0,
    "require_trend": True,
    "trend_ema_fast": 9,
    "trend_ema_slow": 21,
    "min_price": 5.0,
    "min_atr_pct": 0.3,
    "max_atr_pct": 6.0,
    "atr_stop_multiplier": 2.0,
    # --- Adaptive trailing stop ---
    # The trail multiplier now adapts to current vs average volatility.
    # Use adaptive_atr_multiplier() at runtime; this value is the *base*.
    "atr_trail_multiplier": 2.5,
    "atr_trail_multiplier_floor": 1.5,   # minimum trail width (tight trending market)
    "atr_trail_multiplier_ceiling": 4.0, # maximum trail width (very volatile session)
    "adaptive_trail": True,              # set False to revert to fixed multiplier
    # --- Reward / size ---
    "reward_risk_ratio_min": 1.5,
    "take_profit_1r_pct": 0.5,
    "risk_per_trade_pct": 0.75,
    "max_position_pct": 8.0,
    # --- Short-side (ORB breakdown) ---
    # Set allow_short=True to enable the breakdown signal (CFDs only).
    # Stocks on T212 do not support short selling; leave False for equities.
    "allow_short": False,
    # --- Fractional Kelly overlay ---
    # When sufficient trade history is provided (win_rate etc.), Kelly
    # further scales position size.  Disabled by default (no history at startup).
    "use_kelly": False,
    "kelly_fraction": 0.25,  # quarter-Kelly for conservatism (Thorp 2006)
    # --- Session ---
    "avoid_first_minutes": 5,
    "avoid_last_minutes": 30,
    "avoid_lunch": True,
    "session_open_utc": "14:30",
    "session_close_utc": "21:00",
}


@dataclass
class ORBSignal:
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
    reward_risk_ratio: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")
    regime: str = "unknown"
    rvol: Decimal = Decimal("1")


@dataclass
class ORBState:
    """Live trade state for exit monitoring."""
    ticker: str
    strategy_id: str
    side: str
    entry_price: Decimal
    quantity: Decimal
    remaining_quantity: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    take_profit_1r: Decimal
    take_profit_2r: Decimal
    partial_exit_done: bool = False
    atr_at_entry: Decimal = Decimal("0")
    entered_at: datetime | None = None


class OpeningRangeBreakoutStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_PARAMS, **(params or {})}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _compute_opening_range(self, bars: list[Bar]) -> tuple[Decimal, Decimal] | None:
        orb_count = self.params["orb_minutes"] // self.params["candle_interval_minutes"]
        if len(bars) < orb_count:
            return None
        orb = bars[:orb_count]
        return max(b.high for b in orb), min(b.low for b in orb)

    def _validate_range(
        self, orb_high: Decimal, orb_low: Decimal, ref: Decimal
    ) -> tuple[bool, str]:
        if ref <= 0:
            return False, "Bad ref price"
        rng = float((orb_high - orb_low) / ref * 100)
        if rng < self.params["min_range_pct"]:
            return False, f"Range {rng:.2f}% too narrow"
        if rng > self.params["max_range_pct"]:
            return False, f"Range {rng:.2f}% too wide"
        return True, f"Range {rng:.2f}% valid"

    def _check_filters(
        self,
        bars: list[Bar],
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> tuple[bool, str]:
        if not bars:
            return False, "No bars"

        price = bars[-1].close

        if float(price) < self.params["min_price"]:
            return False, f"Price {price} < min {self.params['min_price']}"

        if not is_tradeable_time(
            current_time_utc,
            session_open_utc=self.params["session_open_utc"],
            session_close_utc=self.params["session_close_utc"],
            avoid_first_minutes=self.params["avoid_first_minutes"],
            avoid_last_minutes=self.params["avoid_last_minutes"],
            avoid_lunch=self.params["avoid_lunch"],
        ):
            return False, "Outside tradeable window"

        if prev_close and prev_close > 0:
            g = abs(float(gap_pct(prev_close, bars[0].open)))
            if g > self.params["max_gap_pct"]:
                return False, f"Gap {g:.1f}% too large"

        atr_val = atr(bars, 14)
        if atr_val > 0:
            atr_p = float(atr_val / price * 100)
            if atr_p < self.params["min_atr_pct"]:
                return False, f"ATR {atr_p:.2f}% too low"
            if atr_p > self.params["max_atr_pct"]:
                return False, f"ATR {atr_p:.2f}% too high"

        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return False, f"RVOL {rvol:.2f} < {self.params['min_rvol']}"

        regime = market_regime(bars)
        if regime == "choppy":
            return False, "Market choppy — skip"

        if self.params.get("require_trend") and len(bars) >= self.params["trend_ema_slow"]:
            if not is_trending_up(bars, self.params["trend_ema_fast"], self.params["trend_ema_slow"]):
                return False, "No uptrend confirmation"

        return True, "All filters passed"

    def _check_filters_short(
        self,
        bars: list[Bar],
        current_time_utc: str,
        prev_close: Decimal | None,
    ) -> tuple[bool, str]:
        """
        Mirrored filter set for the short / breakdown side.
        Requires a downtrend (EMA9 < EMA21) and trending (not choppy) regime.
        """
        if not bars:
            return False, "No bars"

        price = bars[-1].close

        if float(price) < self.params["min_price"]:
            return False, f"Price {price} < min {self.params['min_price']}"

        if not is_tradeable_time(
            current_time_utc,
            session_open_utc=self.params["session_open_utc"],
            session_close_utc=self.params["session_close_utc"],
            avoid_first_minutes=self.params["avoid_first_minutes"],
            avoid_last_minutes=self.params["avoid_last_minutes"],
            avoid_lunch=self.params["avoid_lunch"],
        ):
            return False, "Outside tradeable window"

        if prev_close and prev_close > 0:
            g = abs(float(gap_pct(prev_close, bars[0].open)))
            if g > self.params["max_gap_pct"]:
                return False, f"Gap {g:.1f}% too large"

        atr_val = atr(bars, 14)
        if atr_val > 0:
            atr_p = float(atr_val / price * 100)
            if atr_p < self.params["min_atr_pct"]:
                return False, f"ATR {atr_p:.2f}% too low"
            if atr_p > self.params["max_atr_pct"]:
                return False, f"ATR {atr_p:.2f}% too high"

        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return False, f"RVOL {rvol:.2f} < {self.params['min_rvol']}"

        regime = market_regime(bars)
        if regime == "choppy":
            return False, "Market choppy — skip shorts"

        if self.params.get("require_trend") and len(bars) >= self.params["trend_ema_slow"]:
            if not is_trending_down(
                bars, self.params["trend_ema_fast"], self.params["trend_ema_slow"]
            ):
                return False, "No downtrend confirmation for short"

        return True, "All short filters passed"

    # ── Public API ────────────────────────────────────────────────────────────

    def _build_signal(
        self,
        *,
        ticker: str,
        side: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        orb_high: Decimal,
        orb_low: Decimal,
        atr_val: Decimal,
        rvol: Decimal,
        regime: str,
        win_rate: float = 0.5,
        avg_win_pct: float = 0.015,
        avg_loss_pct: float = 0.0075,
    ) -> ORBSignal | None:
        """
        Shared signal builder for long (breakout) and short (breakdown) sides.
        Applies fractional Kelly sizing overlay when enabled.
        """
        current_price = bars[-1].close

        if side == "buy":
            # Long breakout — stop below ORB high, target 2R above entry
            stop_price = current_price - atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
            stop_price = min(stop_price, orb_high * Decimal("0.999"))
        else:
            # Short breakdown — stop above ORB low, target 2R below entry
            stop_price = current_price + atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
            stop_price = max(stop_price, orb_low * Decimal("1.001"))

        risk_per_share = abs(current_price - stop_price)
        if risk_per_share <= 0:
            return None

        if side == "buy":
            tp2 = current_price + risk_per_share * 2
        else:
            tp2 = current_price - risk_per_share * 2

        rr = risk_per_share * 2 / risk_per_share  # always 2.0 by design

        if float(rr) < self.params["reward_risk_ratio_min"]:
            return None

        # ── Position sizing ──────────────────────────────────────────────────
        # Primary: ATR-based (always available)
        qty = atr_position_size(
            account_value=account_value,
            entry_price=current_price,
            atr_val=atr_val,
            risk_pct=Decimal(str(self.params["risk_per_trade_pct"])),
            atr_stop_multiplier=self.params["atr_stop_multiplier"],
            available_cash=available_cash,
        )

        # Optional overlay: fractional Kelly scales ATR qty when trade history exists
        if self.params.get("use_kelly", False):
            kelly_qty = kelly_position_size(
                account_value=account_value,
                entry_price=current_price,
                stop_price=stop_price,
                win_rate=win_rate,
                avg_win_pct=avg_win_pct,
                avg_loss_pct=avg_loss_pct,
                fraction=self.params.get("kelly_fraction", 0.25),
                available_cash=available_cash,
            )
            # Take the more conservative of the two
            if kelly_qty > 0:
                qty = min(qty, kelly_qty)

        max_by_pct = (
            account_value * Decimal(str(self.params["max_position_pct"])) / 100 / current_price
        )
        qty = min(qty, max_by_pct)

        if qty < Decimal("0.01"):
            return None

        # ── Confidence scoring ───────────────────────────────────────────────
        confidence = Decimal("0.50")
        if float(rvol) >= 2.0:
            confidence += Decimal("0.15")
        elif float(rvol) >= 1.5:
            confidence += Decimal("0.10")
        if regime in ("trending_up", "trending_down"):
            confidence += Decimal("0.15")
        if float(rr) >= 2.0:
            confidence += Decimal("0.10")
        # Confirmation: price has moved convincingly past the breakout/breakdown level
        buffer = orb_high * Decimal("1.005") if side == "buy" else orb_low * Decimal("0.995")
        if (side == "buy" and current_price > buffer) or (side == "sell" and current_price < buffer):
            confidence += Decimal("0.10")
        confidence = min(confidence, Decimal("0.95"))

        direction = "breakout" if side == "buy" else "breakdown"
        level = orb_high if side == "buy" else orb_low

        return ORBSignal(
            ticker=ticker,
            side=side,
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=tp2,
            suggested_quantity=qty if side == "buy" else -qty,
            confidence=confidence,
            reason=(
                f"ORB {direction} {'above' if side == 'buy' else 'below'} {level:.2f}. "
                f"ATR={atr_val:.2f}, RVOL={float(rvol):.2f}x, "
                f"regime={regime}, R:R={float(rr):.2f}"
            ),
            params_snapshot={
                "orb_high": float(orb_high), "orb_low": float(orb_low),
                "atr": float(atr_val), "rvol": float(rvol),
                "stop": float(stop_price), "tp2": float(tp2),
                "regime": regime, "rr": float(rr), "side": side,
            },
            reward_risk_ratio=rr,
            atr_value=atr_val,
            regime=regime,
            rvol=rvol,
        )

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None = None,
        # Optional Kelly inputs — supply from closed-trade statistics
        win_rate: float = 0.5,
        avg_win_pct: float = 0.015,
        avg_loss_pct: float = 0.0075,
    ) -> ORBSignal | None:
        """
        Generate an ORB entry signal for the given ticker.
        Checks the long (breakout) side first, then the short (breakdown)
        side if allow_short=True (suitable for CFDs).
        """
        if len(bars) < 4:
            return None

        orb = self._compute_opening_range(bars)
        if orb is None:
            return None
        orb_high, orb_low = orb

        ok, _ = self._validate_range(orb_high, orb_low, bars[0].open)
        if not ok:
            return None

        atr_val = atr(bars, 14)
        if atr_val <= 0:
            return None

        rvol = relative_volume(bars, 20)
        regime = market_regime(bars)

        current_price = bars[-1].close

        # ── Long side (breakout above ORB high) ──────────────────────────────
        if current_price > orb_high:
            ok, _ = self._check_filters(bars, current_time_utc, prev_close)
            if ok:
                return self._build_signal(
                    ticker=ticker, side="buy", bars=bars,
                    account_value=account_value, available_cash=available_cash,
                    orb_high=orb_high, orb_low=orb_low,
                    atr_val=atr_val, rvol=rvol, regime=regime,
                    win_rate=win_rate, avg_win_pct=avg_win_pct, avg_loss_pct=avg_loss_pct,
                )

        # ── Short side (breakdown below ORB low) — CFDs only ─────────────────
        if self.params.get("allow_short", False) and current_price < orb_low:
            ok, _ = self._check_filters_short(bars, current_time_utc, prev_close)
            if ok:
                return self._build_signal(
                    ticker=ticker, side="sell", bars=bars,
                    account_value=account_value, available_cash=available_cash,
                    orb_high=orb_high, orb_low=orb_low,
                    atr_val=atr_val, rvol=rvol, regime=regime,
                    win_rate=win_rate, avg_win_pct=avg_win_pct, avg_loss_pct=avg_loss_pct,
                )

        return None

    def check_exit_conditions(
        self,
        ticker: str,
        state: ORBState,
        current_price: Decimal,
        bars: list[Bar],
    ) -> ORBSignal | None:
        atr_val = atr(bars, 14) if len(bars) >= 15 else state.atr_at_entry

        # ── Adaptive trailing stop multiplier ────────────────────────────────
        # In high-volatility sessions the trail widens to avoid stop-outs on noise.
        # In calm trending sessions it tightens to lock in profits faster.
        # (Kaufman 1995; academic basis: ATR-regime adaptive stops)
        if self.params.get("adaptive_trail", True) and len(bars) >= 21:
            trail_mult = adaptive_atr_multiplier(
                bars=bars,
                base_multiplier=float(self.params["atr_trail_multiplier"]),
                atr_period=14,
                lookback=20,
                floor=float(self.params.get("atr_trail_multiplier_floor", 1.5)),
                ceiling=float(self.params.get("atr_trail_multiplier_ceiling", 4.0)),
            )
        else:
            trail_mult = float(self.params["atr_trail_multiplier"])

        new_trail = trailing_stop_price(
            entry_price=state.entry_price,
            current_price=current_price,
            atr_val=atr_val,
            side=state.side,
            atr_multiplier=trail_mult,
            initial_stop=state.initial_stop,
        )
        # For longs the stop only moves up; for shorts it only moves down
        if state.side == "buy":
            current_stop = max(state.current_stop, new_trail)
        else:
            current_stop = min(state.current_stop, new_trail)

        is_long = (state.side == "buy")
        exit_side = "sell" if is_long else "buy"

        # Stop hit
        stop_hit = (current_price <= current_stop) if is_long else (current_price >= current_stop)
        if stop_hit:
            if is_long:
                stop_type = "trailing_stop" if current_stop > state.initial_stop else "stop"
            else:
                stop_type = "trailing_stop" if current_stop < state.initial_stop else "stop"
            return ORBSignal(
                ticker=ticker, side=exit_side, signal_type=stop_type,
                entry_price=current_price, stop_price=current_stop,
                take_profit_price=state.take_profit_2r,
                suggested_quantity=-state.remaining_quantity,
                confidence=Decimal("1.0"),
                reason=f"Stop at {current_price:.2f} (stop={current_stop:.2f})",
                atr_value=atr_val,
            )

        # Full TP
        tp_hit = (current_price >= state.take_profit_2r) if is_long else (current_price <= state.take_profit_2r)
        if tp_hit:
            return ORBSignal(
                ticker=ticker, side=exit_side, signal_type="take_profit",
                entry_price=current_price, stop_price=current_stop,
                take_profit_price=state.take_profit_2r,
                suggested_quantity=-state.remaining_quantity,
                confidence=Decimal("1.0"),
                reason=f"TP2 at {current_price:.2f}",
                atr_value=atr_val,
            )

        # Partial exit at 1R
        partial_hit = (
            (current_price >= state.take_profit_1r) if is_long
            else (current_price <= state.take_profit_1r)
        )
        if not state.partial_exit_done and partial_hit:
            partial = (
                state.remaining_quantity * Decimal(str(self.params["take_profit_1r_pct"]))
            ).quantize(Decimal("0.01"))
            if partial >= Decimal("0.01"):
                return ORBSignal(
                    ticker=ticker, side=exit_side, signal_type="partial_exit",
                    entry_price=current_price, stop_price=current_stop,
                    take_profit_price=state.take_profit_2r,
                    suggested_quantity=-partial,
                    confidence=Decimal("0.95"),
                    reason=f"Partial exit at 1R ({current_price:.2f})",
                    atr_value=atr_val,
                )

        return None
