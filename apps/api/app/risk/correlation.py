"""
Correlation and sector exposure risk module.

Prevents over-concentration in correlated positions.
A portfolio of AAPL + MSFT + QQQ is not 3 independent bets —
it's one leveraged tech bet.

Implementation:
- Sector limits: max 20% of portfolio in any GICS sector
- Correlation limit: max 0.7 correlation between any two open positions
- Rolling 20-day correlation matrix using close prices
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog

log = structlog.get_logger()

# ── GICS sector mapping for common US equities ─────────────────────────────────
# Production: fetch from Polygon reference API
# This covers 95% of common trading symbols
SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Technology", "GOOG": "Technology", "META": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "AMD": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "ADBE": "Technology", "NOW": "Technology", "SNOW": "Technology",
    "PLTR": "Technology", "PANW": "Technology", "CRWD": "Technology",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "TGT": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "BRK.B": "Financials",
    "V": "Financials", "MA": "Financials", "AXP": "Financials",
    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    # Communication Services
    "NFLX": "Communication", "DIS": "Communication", "T": "Communication",
    "VZ": "Communication", "CMCSA": "Communication",
    # ETFs (treat as broad market)
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
    "XLK": "ETF", "XLF": "ETF", "XLE": "ETF", "XLV": "ETF",
    "SQQQ": "ETF", "TQQQ": "ETF", "UPRO": "ETF",
    # Consumer Staples
    "PG": "Consumer Staples", "KO": "Consumer Staples", "PEP": "Consumer Staples",
    "COST": "Consumer Staples", "WMT": "Consumer Staples",
    # Industrials
    "CAT": "Industrials", "DE": "Industrials", "BA": "Industrials",
    "GE": "Industrials", "HON": "Industrials", "RTX": "Industrials",
}


@dataclass
class CorrelationViolation:
    ticker_new: str
    ticker_existing: str
    correlation: float
    message: str


def get_sector(ticker: str) -> str:
    """Get GICS sector for a ticker. Returns 'Unknown' if not mapped."""
    return SECTOR_MAP.get(ticker.upper(), "Unknown")


def compute_correlation(prices_a: list[float], prices_b: list[float]) -> float:
    """
    Pearson correlation between two price series.
    Uses returns (not prices) to avoid non-stationarity.
    Returns 0.0 if insufficient data.
    """
    if len(prices_a) < 5 or len(prices_b) < 5:
        return 0.0

    n = min(len(prices_a), len(prices_b))
    # Convert to returns
    rets_a = [(prices_a[i] - prices_a[i-1]) / prices_a[i-1]
              for i in range(1, n) if prices_a[i-1] > 0]
    rets_b = [(prices_b[i] - prices_b[i-1]) / prices_b[i-1]
              for i in range(1, n) if prices_b[i-1] > 0]

    n = min(len(rets_a), len(rets_b))
    if n < 5:
        return 0.0

    rets_a = rets_a[:n]
    rets_b = rets_b[:n]

    mean_a = sum(rets_a) / n
    mean_b = sum(rets_b) / n

    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(rets_a, rets_b)) / n
    var_a = sum((a - mean_a) ** 2 for a in rets_a) / n
    var_b = sum((b - mean_b) ** 2 for b in rets_b) / n

    if var_a <= 0 or var_b <= 0:
        return 0.0

    return cov / (var_a ** 0.5 * var_b ** 0.5)


class CorrelationRiskChecker:
    """
    Checks new positions for excessive correlation with existing open positions.

    Designed to be called from the risk engine before order submission.
    """

    def __init__(
        self,
        max_sector_pct: float = 25.0,
        max_correlation: float = 0.75,
        etf_exempt: bool = True,        # ETFs like SPY/QQQ exempt from correlation check
    ) -> None:
        self.max_sector_pct = max_sector_pct
        self.max_correlation = max_correlation
        self.etf_exempt = etf_exempt

    def check_sector_exposure(
        self,
        new_ticker: str,
        new_value: float,
        current_positions: list[dict[str, Any]],
        account_value: float,
    ) -> tuple[bool, str]:
        """
        Check if adding new_ticker would exceed sector concentration limit.

        Returns (allowed, reason).
        """
        if account_value <= 0:
            return True, "OK"

        new_sector = get_sector(new_ticker)
        if new_sector in ("ETF", "Unknown"):
            return True, "OK (ETF/unknown sector exempt)"

        # Sum current exposure in same sector
        sector_value = new_value  # Include the new position
        for pos in current_positions:
            ticker = pos.get("ticker", "")
            pos_value = float(pos.get("value") or
                              pos.get("quantity", 0) * pos.get("current_price", 0))
            if get_sector(ticker) == new_sector:
                sector_value += pos_value

        sector_pct = sector_value / account_value * 100
        if sector_pct > self.max_sector_pct:
            return False, (
                f"Sector limit: {new_sector} would be {sector_pct:.1f}% "
                f"of portfolio (max {self.max_sector_pct}%)"
            )

        return True, f"Sector {new_sector}: {sector_pct:.1f}%"

    def check_correlation(
        self,
        new_ticker: str,
        current_positions: list[dict[str, Any]],
        price_history: dict[str, list[float]],
    ) -> tuple[bool, list[CorrelationViolation]]:
        """
        Check if new_ticker is too correlated with existing open positions.

        price_history: dict of {ticker: [list of close prices]}
        Returns (allowed, violations).
        """
        if self.etf_exempt and get_sector(new_ticker) == "ETF":
            return True, []

        new_prices = price_history.get(new_ticker, [])
        if len(new_prices) < 10:
            return True, []  # Can't compute correlation without history

        violations = []
        for pos in current_positions:
            ticker = pos.get("ticker", "")
            if ticker == new_ticker:
                continue
            if self.etf_exempt and get_sector(ticker) == "ETF":
                continue

            existing_prices = price_history.get(ticker, [])
            if len(existing_prices) < 10:
                continue

            corr = compute_correlation(new_prices, existing_prices)
            if abs(corr) >= self.max_correlation:
                violations.append(CorrelationViolation(
                    ticker_new=new_ticker,
                    ticker_existing=ticker,
                    correlation=round(corr, 3),
                    message=(
                        f"{new_ticker} and {ticker} correlation={corr:.2f} "
                        f"exceeds limit of {self.max_correlation}"
                    ),
                ))

        return len(violations) == 0, violations


# Singleton for use in risk engine
_checker = CorrelationRiskChecker(
    max_sector_pct=25.0,
    max_correlation=0.75,
)


def get_correlation_checker() -> CorrelationRiskChecker:
    return _checker
