"""
Return-series metrics shared by the portfolio backtester, its benchmarks and the
walk-forward harness.

All functions work on plain floats / Decimals and are pure.  Risk-free handling:
``risk_free_annual`` is an annual rate (0.02 == 2%); it is converted to a daily
compounding rate and subtracted from each daily return before the Sharpe/Sortino
numerator is formed.  Cash inside the simulated portfolio earns nothing, so a
positive risk-free rate can only make the reported ratios *less* flattering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

TRADING_DAYS_PER_YEAR = 252


def stddev(values: Sequence[float]) -> float:
    """Population standard deviation (matches the historical engine behaviour)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return float(variance**0.5)


def daily_risk_free(risk_free_annual: float) -> float:
    if risk_free_annual <= -1.0:
        raise ValueError("risk_free_annual must be greater than -100%")
    return float((1.0 + risk_free_annual) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0)


def returns_from_equity(equities: Sequence[Decimal], base: Decimal) -> list[float]:
    """Simple daily returns; ``base`` is the equity immediately before ``equities[0]``."""
    returns: list[float] = []
    previous = base
    for equity in equities:
        if previous > 0:
            returns.append(float((equity / previous) - 1))
        previous = equity
    return returns


def sharpe_ratio(returns: Sequence[float], risk_free_annual: float = 0.0) -> float | None:
    if not returns:
        return None
    rf = daily_risk_free(risk_free_annual)
    excess = [value - rf for value in returns]
    std = stddev(excess)
    if std <= 0:
        return None
    return float((sum(excess) / len(excess)) / std * (TRADING_DAYS_PER_YEAR**0.5))


def sortino_ratio(returns: Sequence[float], risk_free_annual: float = 0.0) -> float | None:
    """Historical engine definition: mean excess return over the stdev of losing days."""
    if not returns:
        return None
    rf = daily_risk_free(risk_free_annual)
    excess_mean = sum(value - rf for value in returns) / len(returns)
    downside_std = stddev([value for value in returns if value < 0])
    if downside_std <= 0:
        return None
    return float(excess_mean / downside_std * (TRADING_DAYS_PER_YEAR**0.5))


def max_drawdown_pct_from_equity(equities: Sequence[Decimal], base: Decimal) -> float:
    """Peak-to-trough drawdown in percent; the running peak starts at ``base``."""
    peak = base
    worst = 0.0
    for equity in equities:
        if equity > peak:
            peak = equity
        if peak > 0:
            worst = max(worst, float((peak - equity) / peak) * 100.0)
    return worst


def max_drawdown_pct_from_returns(returns: Sequence[float]) -> float:
    level = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        level *= 1.0 + value
        peak = max(peak, level)
        if peak > 0:
            worst = max(worst, (peak - level) / peak * 100.0)
    return worst


def total_return_pct_from_returns(returns: Sequence[float]) -> float:
    level = 1.0
    for value in returns:
        level *= 1.0 + value
    return (level - 1.0) * 100.0
