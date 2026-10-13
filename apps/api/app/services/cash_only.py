"""Cash-only invariant for strategies.

CashGuard holds assets it has paid for in full. It does not sell short and does not hold
CFDs, so nothing a strategy is configured with may ask for either.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Strategy parameters that ask for short selling.
SHORT_SELLING_PARAMETERS: tuple[str, ...] = ("allow_short",)
CASH_ONLY_STRATEGY_REFUSAL = (
    "Short selling is not supported: CashGuard is a cash-only application. "
    "Remove or disable these strategy parameters: {parameters}."
)


def short_selling_parameters(params: Mapping[str, Any] | None) -> list[str]:
    """Names of the parameters in ``params`` that ask for short selling.

    Only an absent parameter, ``None``, ``False`` or zero means "no"; any other value,
    including a string such as "false", is treated as a request and refused.
    """
    if not params:
        return []
    return [
        name
        for name in SHORT_SELLING_PARAMETERS
        if params.get(name) is not None and params.get(name) not in (False, 0)
    ]


def reject_short_selling_parameters(params: Mapping[str, Any] | None) -> None:
    """Raise ``ValueError`` when ``params`` asks for short selling."""
    requested = short_selling_parameters(params)
    if requested:
        raise ValueError(CASH_ONLY_STRATEGY_REFUSAL.format(parameters=", ".join(requested)))


def _positive_quantity(value: object) -> Decimal | None:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return quantity if quantity.is_finite() and quantity > 0 else None


def sellable_quantity(positions: object, ticker: str) -> Decimal:
    """How much of ``ticker`` the broker reports as held and free to sell.

    Zero when the instrument is not held or when anything about its position cannot be
    read: a sell is covered only by a quantity the broker has clearly reported. Where the
    broker gives both the holding and the amount free to sell, the smaller one counts.
    """
    if not isinstance(positions, list):
        return Decimal("0")
    wanted = ticker.strip().upper()
    total = Decimal("0")
    for position in positions:
        if not isinstance(position, dict):
            return Decimal("0")
        if str(position.get("ticker", "")).strip().upper() != wanted:
            continue
        held = _positive_quantity(position.get("quantity"))
        if held is None:
            return Decimal("0")
        if position.get("maxSell") is not None:
            free = _positive_quantity(position.get("maxSell"))
            if free is None:
                return Decimal("0")
            held = min(held, free)
        total += held
    return total
