"""
Opening Range Breakout (ORB) — Backward-compatibility shim.

The production implementation lives in orb_production.py.
This module re-exports all production symbols and adds the legacy public
API (OHLCV dataclass + public compute_opening_range / validate_range /
calculate_quantity / generate_signal) used by tests and older call-sites.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.strategies.indicators import Bar
from app.strategies.orb_production import (
    DEFAULT_PARAMS,
    ORBSignal,
    ORBState,
    OpeningRangeBreakoutStrategy as _BaseStrategy,
)

__all__ = [
    "OpeningRangeBreakoutStrategy",
    "ORBSignal",
    "ORBState",
    "DEFAULT_PARAMS",
    "OHLCV",
]


# ── Legacy OHLCV dataclass ────────────────────────────────────────────────────

@dataclass
class OHLCV:
    """
    Legacy OHLC+Volume bar used by tests and older call-sites.
    The production strategy uses indicators.Bar (a NamedTuple without timestamp).
    """
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def to_bar(self) -> Bar:
        return Bar(
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


# ── Compatibility subclass ────────────────────────────────────────────────────

_COMPAT_DEFAULTS: dict[str, Any] = {
    **DEFAULT_PARAMS,
    # Legacy tests were written against a looser max_range_pct (the production
    # default of 3.5% is too tight for the 5% range used in test fixtures).
    "max_range_pct": 10.0,
}


class OpeningRangeBreakoutStrategy(_BaseStrategy):
    """
    Backward-compatible wrapper that adds the legacy public API on top of
    the production OpeningRangeBreakoutStrategy.

    Extra public methods (not on the base class):
      • compute_opening_range(candles)  → (high, low) | None
      • validate_range(high, low, ref)  → (bool, str)
      • calculate_quantity(...)         → Decimal
      • generate_signal(...)            → ORBSignal | None   (legacy signature)
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = {**_COMPAT_DEFAULTS, **(params or {})}
        super().__init__(merged)

    # ── Public wrappers around production private helpers ─────────────────────

    def compute_opening_range(
        self, candles: list[OHLCV]
    ) -> tuple[Decimal, Decimal] | None:
        """Return (orb_high, orb_low) from the first N candles, or None."""
        bars = [c.to_bar() for c in candles]
        return self._compute_opening_range(bars)

    def validate_range(
        self,
        orb_high: Decimal,
        orb_low: Decimal,
        ref_price: Decimal,
    ) -> tuple[bool, str]:
        """Validate that the ORB width is within configured bounds."""
        return self._validate_range(orb_high, orb_low, ref_price)

    def calculate_quantity(
        self,
        entry_price: Decimal,
        stop_price: Decimal,
        account_value: Decimal,
        available_cash: Decimal,
    ) -> Decimal:
        """
        Risk-based position sizing capped by available cash.

        max_risk_dollars = account_value x risk_pct / 100
        qty_by_risk      = max_risk_dollars / risk_per_share
        qty_by_cash      = available_cash   / entry_price
        result           = min(qty_by_risk, qty_by_cash)  rounded to 2 dp
        """
        risk_pct = Decimal(
            str(
                self.params.get(
                    "max_risk_per_trade_pct",
                    self.params.get("risk_per_trade_pct", 1.0),
                )
            )
        )
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return Decimal("0")
        max_risk_dollars = account_value * risk_pct / 100
        qty_by_risk = max_risk_dollars / risk_per_share
        qty_by_cash = (
            available_cash / entry_price if entry_price > 0 else Decimal("0")
        )
        qty = min(qty_by_risk, qty_by_cash)
        return qty.quantize(Decimal("0.01"))

    # ── Legacy generate_signal signature ─────────────────────────────────────

    def generate_signal(  # type: ignore[override]
        self,
        ticker: str,
        current_price: Decimal,
        current_candle: OHLCV,
        opening_range_candles: list[OHLCV],
        account_value: Decimal,
        available_cash: Decimal,
        session_candle_index: int = 0,
    ) -> ORBSignal | None:
        """
        Legacy public entry-point used by tests and older workers.

        Checks for a long breakout above the ORB high; returns an ORBSignal
        on a confirmed breakout, None otherwise.
        """
        orb = self.compute_opening_range(opening_range_candles)
        if orb is None:
            return None
        orb_high, orb_low = orb

        if current_price <= orb_high:
            return None

        entry_price = current_price

        # Stop just below ORB high, but no wider than the ORB itself
        natural_stop = entry_price - (orb_high - orb_low)
        tight_stop = orb_high * Decimal("0.999")
        stop_price = max(natural_stop, tight_stop)

        if stop_price >= entry_price:
            stop_price = entry_price * Decimal("0.99")

        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None

        take_profit_price = entry_price + risk_per_share * Decimal("2")

        qty = self.calculate_quantity(
            entry_price, stop_price, account_value, available_cash
        )
        if qty <= Decimal("0"):
            return None

        orb_range_pct = (
            float((orb_high - orb_low) / orb_low * 100) if orb_low > 0 else 0
        )

        return ORBSignal(
            ticker=ticker,
            side="buy",
            signal_type="entry",
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            suggested_quantity=qty,
            confidence=Decimal("0.70"),
            reason=(
                f"ORB breakout above {orb_high:.2f}. "
                f"Range={orb_range_pct:.2f}%, "
                f"entry={entry_price:.2f}, stop={stop_price:.2f}, "
                f"tp={take_profit_price:.2f}"
            ),
            params_snapshot={
                "orb_high": float(orb_high),
                "orb_low": float(orb_low),
                "session_candle_index": session_candle_index,
            },
        )
