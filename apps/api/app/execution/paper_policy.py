"""Deterministic Decimal-only execution policy for local paper orders."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal

PaperSimulationProfile = Literal["standard", "partial_fill", "no_liquidity"]
PaperFillOutcome = Literal["filled", "partially_filled", "rejected"]

PAPER_SPREAD_BPS = Decimal("10")
PAPER_SLIPPAGE_BPS = Decimal("5")
PAPER_FEE_BPS = Decimal("2")
PAPER_MIN_FEE = Decimal("0.01")
PAPER_FILL_LATENCY_MS = 25

_BPS_DENOMINATOR = Decimal("10000")
_PRICE_QUANTUM = Decimal("0.00000001")
_QUANTITY_QUANTUM = Decimal("0.00000001")
_CASH_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class PaperFillDecision:
    profile: PaperSimulationProfile
    outcome: PaperFillOutcome
    quote_price: Decimal
    filled_quantity: Decimal
    fill_price: Decimal | None
    fee_amount: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    fill_latency_ms: int
    rejection_code: str | None = None


def _reject(
    profile: PaperSimulationProfile, quote_price: Decimal, rejection_code: str
) -> PaperFillDecision:
    return PaperFillDecision(
        profile=profile,
        outcome="rejected",
        quote_price=quote_price,
        filled_quantity=Decimal("0"),
        fill_price=None,
        fee_amount=Decimal("0"),
        spread_bps=PAPER_SPREAD_BPS,
        slippage_bps=PAPER_SLIPPAGE_BPS,
        fill_latency_ms=PAPER_FILL_LATENCY_MS,
        rejection_code=rejection_code,
    )


def evaluate_paper_fill(
    *,
    side: str,
    quantity: Decimal,
    quote_price: Decimal,
    profile: PaperSimulationProfile,
) -> PaperFillDecision:
    """Return a deterministic policy decision without I/O or binary floats."""
    try:
        quantity = quantity.quantize(_QUANTITY_QUANTUM, rounding=ROUND_DOWN)
        quote_price = quote_price.quantize(_PRICE_QUANTUM, rounding=ROUND_DOWN)
    except InvalidOperation:
        return _reject(profile, Decimal("0"), "paper_input_out_of_range")
    if quantity <= 0 or quote_price <= 0:
        return _reject(profile, quote_price, "paper_input_below_precision")
    if profile == "no_liquidity":
        return _reject(profile, quote_price, "paper_no_liquidity")

    filled_quantity = quantity
    outcome: PaperFillOutcome = "filled"
    if profile == "partial_fill":
        filled_quantity = (quantity / Decimal("2")).quantize(_QUANTITY_QUANTUM)
        if filled_quantity <= 0:
            return _reject(profile, quote_price, "paper_partial_fill_below_precision")
        outcome = "partially_filled"

    half_spread_rate = PAPER_SPREAD_BPS / Decimal("2") / _BPS_DENOMINATOR
    slippage_rate = PAPER_SLIPPAGE_BPS / _BPS_DENOMINATOR
    adverse_rate = half_spread_rate + slippage_rate
    multiplier = Decimal("1") + adverse_rate if side == "buy" else Decimal("1") - adverse_rate
    fill_price = (quote_price * multiplier).quantize(_PRICE_QUANTUM)
    gross_value = fill_price * filled_quantity
    fee = max(PAPER_MIN_FEE, gross_value * PAPER_FEE_BPS / _BPS_DENOMINATOR)

    return PaperFillDecision(
        profile=profile,
        outcome=outcome,
        quote_price=quote_price,
        filled_quantity=filled_quantity,
        fill_price=fill_price,
        fee_amount=fee.quantize(_CASH_QUANTUM),
        spread_bps=PAPER_SPREAD_BPS,
        slippage_bps=PAPER_SLIPPAGE_BPS,
        fill_latency_ms=PAPER_FILL_LATENCY_MS,
    )
