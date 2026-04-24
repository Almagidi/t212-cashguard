"""
Opening Fade Strategy — production grade.

Scientific basis:
  Berkman, Koch & Westerholm (2014); Ni, Wang & Xiao (2015).
  Stocks (and CFDs) that open with a gap > min_gap_pct frequently fail to
  sustain that gap and mean-revert toward the prior close / session VWAP
  within the first 30–60 minutes.  This behaviour is most pronounced on days
  without a fundamental catalyst (earnings, M&A news) and is reinforced by
  the Choppiness Index: a choppy/ranging session provides the best fade context.

Relationship to ORB:
  While ORB trades *continuation* of an opening move, the Fade trades
  *reversal*.  The strategy_runner chooses between them based on regime:
    Choppiness Index > 61.8  →  Fade is active, ORB skips
    Choppiness Index ≤ 61.8  →  ORB is active, Fade skips

Directional support:
  • Gap-up fade  →  short entry (CFD) or skip on equity accounts.
  • Gap-down fade →  long entry (stock or CFD).

The strategy is designed to be registered as type="opening_fade" in the
Strategy model, with is_live=False (paper mode) until validated.
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
    gap_pct,
    is_tradeable_time,
    market_regime,
    relative_volume,
    vwap,
)

# ── Default parameters ────────────────────────────────────────────────────────

DEFAULT_FADE_PARAMS: dict[str, Any] = {
    # --- Gap requirements ---
    "min_gap_pct": 1.5,          # Minimum gap size to consider a fade setup
    "max_gap_pct": 6.0,          # Above this, gap is likely fundamental — skip
    # --- Volume confirmation ---
    "min_rvol": 1.5,             # Need above-average volume on reversal bar
    # --- Regime gate ---
    # The fade is most reliable in choppy/ranging sessions.
    # choppiness_index > chop_threshold  →  proceed; else skip.
    "chop_threshold": 50.0,      # Conservative threshold (pure chop is 61.8)
    # --- Reversal confirmation bars ---
    # Price must have *failed* to hold the gap direction for N bars before entry.
    # E.g. gap-up: price closes BELOW the session open for n_confirm bars.
    "n_confirm": 2,
    # --- Sizing and risk ---
    "atr_stop_multiplier": 1.5,  # Tight stop (fade = high-precision entry)
    "reward_risk_ratio_min": 1.5,
    "risk_per_trade_pct": 0.5,   # Smaller risk vs ORB (mean-reversion less reliable)
    "max_position_pct": 6.0,
    "take_profit_1r_pct": 0.5,   # Partial exit at 1R
    # --- CFD / stock mode ---
    # allow_short=True enables gap-up fades (requires CFD or short-selling account).
    # Set False for equity-only accounts — only gap-DOWN fades (longs) will trigger.
    "allow_short": False,
    # --- Session window ---
    "avoid_first_minutes": 3,    # Enter after initial whipsaw (3 min buffer)
    "fade_window_minutes": 45,   # Only fade within first 45 min of session
    "session_open_utc": "14:30",
    "session_close_utc": "21:00",
    "avoid_last_minutes": 60,    # Do not fade late in session
    "avoid_lunch": True,
    "min_price": 5.0,
    "min_atr_pct": 0.3,
    "max_atr_pct": 6.0,
}


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class FadeSignal:
    ticker: str
    side: str          # "buy" (gap-down fade) or "sell" (gap-up fade, CFD)
    signal_type: str   # "entry" or "partial_exit" or "stop" or "take_profit"
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    suggested_quantity: Decimal
    confidence: Decimal
    reason: str
    params_snapshot: dict[str, Any] = field(default_factory=dict)
    reward_risk_ratio: Decimal = Decimal("0")
    atr_value: Decimal = Decimal("0")
    gap_value: Decimal = Decimal("0")
    rvol: Decimal = Decimal("1")


# ── Strategy class ────────────────────────────────────────────────────────────

class OpeningFadeStrategy:
    """
    Opening Fade — trades mean-reversion of overextended gap opens.

    Usage in strategy_runner (type="opening_fade"):
        engine = OpeningFadeStrategy(strategy.params)
        signal = engine.generate_signal(
            ticker, bars, account_value, available_cash, current_time_utc,
            prev_close=prev_close,
        )
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**DEFAULT_FADE_PARAMS, **(params or {})}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _time_in_fade_window(self, current_time_utc: str) -> bool:
        """True if we are within the valid fade entry window."""
        h, m = map(int, current_time_utc.split(":"))
        current_mins = h * 60 + m

        open_h, open_m = map(int, self.params["session_open_utc"].split(":"))
        open_mins = open_h * 60 + open_m
        close_h, close_m = map(int, self.params["session_close_utc"].split(":"))
        close_mins = close_h * 60 + close_m

        if current_mins < open_mins or current_mins >= close_mins:
            return False

        after_buffer = current_mins >= open_mins + self.params["avoid_first_minutes"]
        within_fade = current_mins <= open_mins + self.params["fade_window_minutes"]
        before_close = current_mins < close_mins - self.params["avoid_last_minutes"]

        # Avoid lunch chop zone (17:00–18:30 UTC = 12:00–13:30 ET)
        if self.params["avoid_lunch"] and (17 * 60 <= current_mins < 18 * 60 + 30):
            return False

        return after_buffer and within_fade and before_close

    def _count_confirm_bars(
        self, bars: list[Bar], session_open: Decimal, direction: str
    ) -> int:
        """
        Count how many consecutive bars (from most recent) have closed
        on the fade side of the session open.
        direction='down'  →  close < session_open  (gap-up fading)
        direction='up'    →  close > session_open  (gap-down fading)
        """
        count = 0
        for bar in reversed(bars):
            if direction == "down" and bar.close < session_open:
                count += 1
            elif direction == "up" and bar.close > session_open:
                count += 1
            else:
                break
        return count

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_signal(
        self,
        ticker: str,
        bars: list[Bar],
        account_value: Decimal,
        available_cash: Decimal,
        current_time_utc: str,
        prev_close: Decimal | None = None,
        session_open: Decimal | None = None,
    ) -> FadeSignal | None:
        """
        Attempt to generate an Opening Fade entry signal.

        Args:
            bars:         Intraday bars for the current session.  bars[0].open
                          is treated as the session open price unless
                          ``session_open`` is provided explicitly.
            prev_close:   Previous session's closing price.  Required — the
                          gap is measured relative to this value.
            session_open: Override for the session's opening price.  Pass
                          this when ``bars`` includes pre-session history so
                          that bars[0] is NOT the opening bar.

        Returns None if conditions are not met; otherwise returns a FadeSignal.
        """
        if len(bars) < 5 or prev_close is None or prev_close <= 0:
            return None

        # ── Time gate ────────────────────────────────────────────────────────
        if not self._time_in_fade_window(current_time_utc):
            return None

        # Use explicit session_open if provided, otherwise assume bars[0] is
        # the first bar of the current session (normal production use-case).
        session_open = session_open if (session_open is not None and session_open > 0) else bars[0].open
        current_price = bars[-1].close

        if float(session_open) < self.params["min_price"]:
            return None

        # ── Gap measurement ───────────────────────────────────────────────────
        gap = gap_pct(prev_close, session_open)   # positive = gap up, negative = gap down
        gap_abs = abs(float(gap))

        if gap_abs < self.params["min_gap_pct"]:
            return None
        if gap_abs > self.params["max_gap_pct"]:
            return None

        # ── ATR / RVOL pre-check ──────────────────────────────────────────────
        atr_val = atr(bars, 14)
        if atr_val <= 0:
            return None

        atr_p = float(atr_val / Decimal(str(session_open)) * 100)
        if atr_p < self.params["min_atr_pct"] or atr_p > self.params["max_atr_pct"]:
            return None

        rvol = relative_volume(bars, 20)
        if float(rvol) < self.params["min_rvol"]:
            return None

        # ── Regime gate: fade is reliable only in choppy/ranging sessions ────
        chop = choppiness_index(bars, period=14)
        if float(chop) < self.params["chop_threshold"]:
            return None   # trending session — ORB is better here

        # ── Determine fade direction ──────────────────────────────────────────
        gap_up   = float(gap) > 0
        gap_down = float(gap) < 0

        if gap_up:
            # Gap-up fade → short (needs allow_short=True)
            if not self.params.get("allow_short", False):
                return None
            # Confirmation: price has already started to give back the gap
            n = self._count_confirm_bars(bars, session_open, direction="down")
            if n < self.params["n_confirm"]:
                return None

            # Entry: current close, stop above session high
            session_high = max(b.high for b in bars)
            stop_price   = session_high + atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
            target_price = prev_close  # target = prior close (full gap fill)

            # Ensure adequate R:R
            risk   = abs(stop_price - current_price)
            reward = abs(current_price - target_price)
            if risk <= 0 or (reward / risk) < Decimal(str(self.params["reward_risk_ratio_min"])):
                return None

            side = "sell"
            reason = (
                f"Gap-up fade: gap={float(gap):.1f}%, chop={float(chop):.1f}, "
                f"RVOL={float(rvol):.2f}x, confirm_bars={n}. "
                f"Target=prev_close {prev_close:.2f}"
            )

        elif gap_down:
            # Gap-down fade → long (works on stocks and CFDs)
            n = self._count_confirm_bars(bars, session_open, direction="up")
            if n < self.params["n_confirm"]:
                return None

            # Entry: current close, stop below session low
            session_low  = min(b.low for b in bars)
            stop_price   = session_low - atr_val * Decimal(str(self.params["atr_stop_multiplier"]))
            target_price = prev_close   # target = prior close (full gap fill)

            risk   = abs(current_price - stop_price)
            reward = abs(target_price - current_price)
            if risk <= 0 or (reward / risk) < Decimal(str(self.params["reward_risk_ratio_min"])):
                return None

            side = "buy"
            reason = (
                f"Gap-down fade: gap={float(gap):.1f}%, chop={float(chop):.1f}, "
                f"RVOL={float(rvol):.2f}x, confirm_bars={n}. "
                f"Target=prev_close {prev_close:.2f}"
            )
        else:
            return None

        # ── Position sizing ───────────────────────────────────────────────────
        qty = atr_position_size(
            account_value=account_value,
            entry_price=current_price,
            atr_val=atr_val,
            risk_pct=Decimal(str(self.params["risk_per_trade_pct"])),
            atr_stop_multiplier=self.params["atr_stop_multiplier"],
            available_cash=available_cash,
        )
        max_by_pct = (
            account_value * Decimal(str(self.params["max_position_pct"])) / 100 / current_price
        )
        qty = min(qty, max_by_pct)

        if qty < Decimal("0.01"):
            return None

        # ── Confidence scoring ────────────────────────────────────────────────
        rr = reward / risk
        confidence = Decimal("0.45")   # Fades have slightly lower base confidence
        if float(rvol) >= 2.0:         confidence += Decimal("0.15")
        elif float(rvol) >= 1.5:       confidence += Decimal("0.10")
        if float(chop) > 61.8:         confidence += Decimal("0.10")  # strongly choppy
        if float(rr) >= 2.0:           confidence += Decimal("0.10")
        if n >= 3:                     confidence += Decimal("0.10")  # stronger confirmation
        confidence = min(confidence, Decimal("0.90"))

        return FadeSignal(
            ticker=ticker,
            side=side,
            signal_type="entry",
            entry_price=current_price,
            stop_price=stop_price,
            take_profit_price=target_price,
            suggested_quantity=qty if side == "buy" else -qty,
            confidence=confidence,
            reason=reason,
            params_snapshot={
                "gap_pct": float(gap),
                "chop": float(chop),
                "rvol": float(rvol),
                "atr": float(atr_val),
                "stop": float(stop_price),
                "target": float(target_price),
                "confirm_bars": n,
                "rr": float(rr),
                "side": side,
            },
            reward_risk_ratio=rr,
            atr_value=atr_val,
            gap_value=gap,
            rvol=rvol,
        )

    def check_exit_conditions(
        self,
        ticker: str,
        side: str,
        current_price: Decimal,
        entry_price: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal,
        remaining_qty: Decimal,
        bars: list[Bar],
        partial_exit_done: bool = False,
    ) -> FadeSignal | None:
        """
        Check whether an open fade position should be exited.
        Fades use a fixed stop (not trailing) since the position is short-lived.
        """
        is_long   = (side == "buy")
        exit_side = "sell" if is_long else "buy"

        atr_val = atr(bars, 14) if len(bars) >= 15 else Decimal("0")

        # Stop check
        stop_hit = (current_price <= stop_price) if is_long else (current_price >= stop_price)
        if stop_hit:
            return FadeSignal(
                ticker=ticker, side=exit_side, signal_type="stop",
                entry_price=current_price, stop_price=stop_price,
                take_profit_price=take_profit_price,
                suggested_quantity=-remaining_qty,
                confidence=Decimal("1.0"),
                reason=f"Fade stop at {current_price:.2f} (stop={stop_price:.2f})",
                atr_value=atr_val,
            )

        # Take-profit check (gap fill = full target)
        tp_hit = (current_price >= take_profit_price) if is_long else (current_price <= take_profit_price)
        if tp_hit:
            return FadeSignal(
                ticker=ticker, side=exit_side, signal_type="take_profit",
                entry_price=current_price, stop_price=stop_price,
                take_profit_price=take_profit_price,
                suggested_quantity=-remaining_qty,
                confidence=Decimal("1.0"),
                reason=f"Fade TP (gap fill) at {current_price:.2f}",
                atr_value=atr_val,
            )

        # Partial exit at 1R
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share > 0:
            one_r = (entry_price + risk_per_share) if is_long else (entry_price - risk_per_share)
            partial_hit = (
                (current_price >= one_r) if is_long else (current_price <= one_r)
            )
            if not partial_exit_done and partial_hit:
                partial = (
                    remaining_qty * Decimal(str(self.params["take_profit_1r_pct"]))
                ).quantize(Decimal("0.01"))
                if partial >= Decimal("0.01"):
                    return FadeSignal(
                        ticker=ticker, side=exit_side, signal_type="partial_exit",
                        entry_price=current_price, stop_price=stop_price,
                        take_profit_price=take_profit_price,
                        suggested_quantity=-partial,
                        confidence=Decimal("0.90"),
                        reason=f"Fade partial exit at 1R ({current_price:.2f})",
                        atr_value=atr_val,
                    )

        return None
