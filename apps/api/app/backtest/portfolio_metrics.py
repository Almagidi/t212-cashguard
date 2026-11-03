"""
Return-series metrics shared by the portfolio backtester, its benchmarks and the
walk-forward harness.

All functions work on plain floats / Decimals and are pure.  Risk-free handling:
``risk_free_annual`` is an annual rate (0.02 == 2%); it is converted to a daily
compounding rate and subtracted from each daily return before the Sharpe/Sortino
numerator is formed.  Cash inside the simulated portfolio earns nothing, so a
positive risk-free rate can only make the reported ratios *less* flattering.

Day-count convention: one year is ``TRADING_DAYS_PER_YEAR`` (252) trading sessions
everywhere.  Sharpe and Sortino scale a per-session ratio by sqrt(252); annualised
return and annualised turnover scale by 252 / (number of sessions measured).  Calendar
days (365) are not used.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    """Mean excess return over the standard downside deviation, annualised by sqrt(252).

    The target (minimum acceptable return) is the daily risk-free rate.  The downside deviation
    is the root mean square of ``min(r - target, 0)`` over ALL periods, so periods at or above
    target count as zero shortfall rather than being dropped.  ``None`` when there is no
    shortfall at all.
    """
    if not returns:
        return None
    rf = daily_risk_free(risk_free_annual)
    excess_mean = sum(value - rf for value in returns) / len(returns)
    shortfall_squares = sum(min(value - rf, 0.0) ** 2 for value in returns)
    downside_deviation = (shortfall_squares / len(returns)) ** 0.5
    if downside_deviation <= 0:
        return None
    return float(excess_mean / downside_deviation * (TRADING_DAYS_PER_YEAR**0.5))


def annualised_return_pct_from_periods(final: Decimal, initial: Decimal, periods: int) -> Decimal:
    """Compound growth scaled to a 252-session year; ``periods`` is the number of session returns."""
    if initial <= 0 or final <= 0:
        return Decimal("0")
    exponent = TRADING_DAYS_PER_YEAR / max(1, periods)
    value = ((float(final) / float(initial)) ** exponent - 1.0) * 100.0
    return Decimal(str(value))


def annualised_one_way_turnover_pct(
    traded_notional: Decimal, average_equity: Decimal, periods: int
) -> Decimal:
    """One-way turnover per year, as a percent of average equity.

    ``traded_notional`` is the sum of every buy and every sell.  One-way turnover is half of
    that sum (a full liquidation plus a full re-purchase is 100%, not 200%), divided by average
    equity and scaled to a 252-session year.
    """
    if average_equity <= 0:
        return Decimal("0")
    one_way = traded_notional / Decimal("2")
    scale = Decimal(TRADING_DAYS_PER_YEAR) / Decimal(max(1, periods))
    return one_way / average_equity * scale * Decimal("100")


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
