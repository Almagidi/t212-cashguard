"""
Transaction-cost model for portfolio research (spread, slippage, commission, FX fee).

Per side, each fill pays
  * half of the quoted spread plus slippage, as an adverse move in the fill price;
  * a commission (default mirrors the paper-trading policy, with its minimum fee);
  * an FX conversion fee when the instrument currency differs from the account
    currency (Trading 212 charges 0.15% per side on non-GBP instruments for a GBP
    account, hence the 0.0015 default and the GBP default account currency).

Unknown instrument currency is treated as foreign and the FX fee is applied.  That
is deliberately pessimistic: a result is only allowed to look cheap when the caller
has said which instruments are account-currency listings (``instrument_currencies``
or ``default_instrument_currency``).

``stress_multiplier`` (1x, 2x, 3x) scales every cost component, including the
minimum fee, so a cost-stress run is a straight rescaling of the same model.

A model with all costs at zero is rejected unless it was requested explicitly with
``TransactionCostModel.explicit_zero()``; such a model is labelled ``zero_cost_explicit``
in ``describe()`` so downstream results can say they were run frictionless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.execution.paper_policy import (
    PAPER_FEE_BPS,
    PAPER_MIN_FEE,
    PAPER_SLIPPAGE_BPS,
    PAPER_SPREAD_BPS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_FX_FEE_RATE = Decimal("0.0015")
DEFAULT_ACCOUNT_CURRENCY = "GBP"

_BPS = Decimal("10000")  # one unit of rate == 10,000 bps
_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class CostFill:
    price: Decimal
    fee: Decimal  # commission + fx_fee, in account currency
    fx_fee: Decimal


@dataclass(frozen=True)
class TransactionCostModel:
    spread_bps: Decimal = PAPER_SPREAD_BPS  # full quoted spread; half is paid per side
    slippage_bps: Decimal = PAPER_SLIPPAGE_BPS  # per side
    commission_bps: Decimal = PAPER_FEE_BPS  # per side, subject to a minimum fee
    fx_fee_rate: Decimal = DEFAULT_FX_FEE_RATE  # per side, fraction of notional
    account_currency: str = DEFAULT_ACCOUNT_CURRENCY
    instrument_currencies: Mapping[str, str] = field(default_factory=dict)
    default_instrument_currency: str | None = None  # None => unknown => foreign
    spread_bps_by_ticker: Mapping[str, Decimal] = field(default_factory=dict)
    stress_multiplier: Decimal = Decimal("1")
    zero_cost: bool = False

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps", "commission_bps", "fx_fee_rate"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if any(value < 0 for value in self.spread_bps_by_ticker.values()):
            raise ValueError("spread_bps_by_ticker values cannot be negative")
        if self.stress_multiplier <= 0:
            raise ValueError("stress_multiplier must be positive")
        if not self.account_currency.strip():
            raise ValueError("account_currency is required")
        has_cost = self.total_per_side_bps != 0 or any(self.spread_bps_by_ticker.values())
        if self.zero_cost and has_cost:
            raise ValueError("zero_cost=True requires every cost component to be zero")
        if not has_cost and not self.zero_cost:
            raise ValueError(
                "all costs are zero: a frictionless run must be requested explicitly "
                "with TransactionCostModel.explicit_zero()"
            )

    @classmethod
    def explicit_zero(
        cls, *, account_currency: str = DEFAULT_ACCOUNT_CURRENCY
    ) -> TransactionCostModel:
        return cls(
            spread_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
            commission_bps=Decimal("0"),
            fx_fee_rate=Decimal("0"),
            account_currency=account_currency,
            zero_cost=True,
        )

    @property
    def fx_fee_bps(self) -> Decimal:
        """The FX fee (a fraction of notional) expressed in bps like every other component."""
        return self.fx_fee_rate * _BPS

    @property
    def total_per_side_bps(self) -> Decimal:
        """Default-spread cost of one side in bps (full spread, as quoted; FX fee included)."""
        return self.spread_bps + self.slippage_bps + self.commission_bps + self.fx_fee_bps

    def round_trip_cost_bps(self, ticker: str) -> Decimal:
        """All-in cost of buying then selling ``ticker``, in bps of notional, after stress.

        Per side: half the spread, slippage, commission (the minimum fee is not counted) and the
        FX fee when the instrument is foreign.
        """
        per_side = self.spread_for(ticker) / Decimal("2") + self.slippage_bps + self.commission_bps
        if self.is_foreign(ticker):
            per_side += self.fx_fee_bps
        return per_side * Decimal("2") * self.stress_multiplier

    @property
    def label(self) -> str:
        return "zero_cost_explicit" if self.zero_cost else "standard"

    def currency_for(self, ticker: str) -> str | None:
        key = ticker.strip().upper()
        for candidate, currency in self.instrument_currencies.items():
            if candidate.strip().upper() == key:
                return currency.strip().upper()
        if self.default_instrument_currency:
            return self.default_instrument_currency.strip().upper()
        return None

    def is_foreign(self, ticker: str) -> bool:
        currency = self.currency_for(ticker)
        return currency is None or currency != self.account_currency.strip().upper()

    def spread_for(self, ticker: str) -> Decimal:
        key = ticker.strip().upper()
        for candidate, spread in self.spread_bps_by_ticker.items():
            if candidate.strip().upper() == key:
                return spread
        return self.spread_bps

    def fill(self, *, side: str, quantity: Decimal, quote_price: Decimal, ticker: str) -> CostFill:
        if side not in {"buy", "sell"}:
            raise ValueError(f"unknown side {side!r}")
        mult = self.stress_multiplier
        adverse = (self.spread_for(ticker) / Decimal("2") + self.slippage_bps) * mult / _BPS
        factor = Decimal("1") + adverse if side == "buy" else Decimal("1") - adverse
        price = (quote_price * factor).quantize(_QUANTUM)
        gross = price * quantity
        commission = Decimal("0")
        if self.commission_bps > 0:
            commission = max(PAPER_MIN_FEE * mult, gross * self.commission_bps * mult / _BPS)
        fx_fee = Decimal("0")
        if self.fx_fee_rate > 0 and self.is_foreign(ticker):
            fx_fee = gross * self.fx_fee_rate * mult
        return CostFill(
            price=price,
            fee=(commission + fx_fee).quantize(_QUANTUM),
            fx_fee=fx_fee.quantize(_QUANTUM),
        )

    def describe(self, tickers: Sequence[str] | None = None) -> dict[str, Any]:
        """Parameters of the model; with ``tickers``, also the cheapest round-trip cost among them."""
        return {
            "min_round_trip_cost_bps": (
                float(min(self.round_trip_cost_bps(ticker) for ticker in tickers))
                if tickers
                else None
            ),
            "gated": True,
            "label": self.label,
            "zero_cost": self.zero_cost,
            "spread_bps": float(self.spread_bps),
            "spread_bps_by_ticker": {k: float(v) for k, v in self.spread_bps_by_ticker.items()},
            "slippage_bps": float(self.slippage_bps),
            "commission_bps": float(self.commission_bps),
            "fx_fee_rate": float(self.fx_fee_rate),
            "account_currency": self.account_currency.strip().upper(),
            "instrument_currencies": {
                k.strip().upper(): v.strip().upper() for k, v in self.instrument_currencies.items()
            },
            "default_instrument_currency": (
                self.default_instrument_currency.strip().upper()
                if self.default_instrument_currency
                else None
            ),
            "unknown_currency_policy": "treated_as_foreign_fx_fee_applied",
            "stress_multiplier": float(self.stress_multiplier),
        }
