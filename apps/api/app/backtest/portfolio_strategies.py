"""
Portfolio-level research strategies for longer-horizon Trading 212 use cases.

These strategies are intentionally restricted to price-based inputs because the
current repo does not yet have a validated fundamentals/dividends data layer.
That keeps the research honest and reproducible with the existing Polygon
historical bar pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import pstdev
from typing import Any, Protocol

from app.strategies.indicators import Bar


def _closes(bars: list[Bar], as_of_index: int) -> list[Decimal]:
    return [bar.close for bar in bars[: as_of_index + 1]]


def _normalize(weights: dict[str, Decimal]) -> dict[str, Decimal]:
    positive = {ticker: weight for ticker, weight in weights.items() if weight > 0}
    total = sum(positive.values(), Decimal("0"))
    if total <= 0:
        return {}
    return {ticker: (weight / total).quantize(Decimal("0.0001")) for ticker, weight in positive.items()}


def _equal_weight(tickers: list[str]) -> dict[str, Decimal]:
    if not tickers:
        return {}
    weight = (Decimal("1") / Decimal(str(len(tickers)))).quantize(Decimal("0.0001"))
    return {ticker: weight for ticker in tickers}


def _sma(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window, Decimal("0")) / Decimal(str(period))


def _return(values: list[Decimal], lookback: int, skip_recent: int = 0) -> Decimal | None:
    end_idx = len(values) - 1 - skip_recent
    start_idx = end_idx - lookback
    if start_idx < 0 or end_idx < 0:
        return None
    start = values[start_idx]
    end = values[end_idx]
    if start <= 0:
        return None
    return (end / start) - Decimal("1")


def _annualised_volatility(values: list[Decimal], lookback: int) -> Decimal | None:
    if len(values) < lookback + 1:
        return None
    sample = values[-(lookback + 1) :]
    returns: list[float] = []
    for idx in range(1, len(sample)):
        prev_close = sample[idx - 1]
        current_close = sample[idx]
        if prev_close <= 0:
            return None
        returns.append(float((current_close / prev_close) - Decimal("1")))
    if not returns:
        return None
    return Decimal(str(pstdev(returns) * (252 ** 0.5)))


class PortfolioStrategyProtocol(Protocol):
    label: str
    description: str
    rebalance_frequency: str
    min_history_bars: int

    def target_weights(
        self,
        history: dict[str, list[Bar]],
        *,
        as_of_index: int,
    ) -> dict[str, Decimal]: ...


@dataclass
class StrategyInfo:
    type: str
    label: str
    description: str
    rationale: str
    rebalance_frequency: str
    min_history_bars: int


class CoreBuyHoldStrategy:
    label = "Diversified Buy-and-Hold Core"
    description = "Long-only diversified equity core with low turnover and annual rebalancing."
    rebalance_frequency = "annual"
    min_history_bars = 0

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict[str, Decimal]:
        return _equal_weight(sorted(history))


class EqualWeightRebalanceStrategy:
    label = "Equal-Weight Rebalancing"
    description = "Equal-weight basket with periodic rebalancing toward target allocations."
    rebalance_frequency = "quarterly"
    min_history_bars = 0

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.rebalance_frequency = str(self.params.get("rebalance_frequency", "quarterly"))

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict[str, Decimal]:
        return _equal_weight(sorted(history))


class CrossSectionalMomentumStrategy:
    label = "Cross-Sectional Momentum"
    description = "Buys recent winners from a diversified universe and rotates monthly."
    rebalance_frequency = "monthly"
    min_history_bars = 147  # 126-day lookback, skip most recent 21 days

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.lookback_bars = int(self.params.get("lookback_bars", 126))
        self.skip_recent_bars = int(self.params.get("skip_recent_bars", 21))
        self.top_n = int(self.params.get("top_n", 0))
        self.min_positive_return = Decimal(str(self.params.get("min_positive_return", "0.0")))
        self.min_history_bars = self.lookback_bars + self.skip_recent_bars

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict[str, Decimal]:
        ranked: list[tuple[str, Decimal]] = []
        for ticker, bars in history.items():
            score = _return(_closes(bars, as_of_index), self.lookback_bars, self.skip_recent_bars)
            if score is None or score <= self.min_positive_return:
                continue
            ranked.append((ticker, score))

        if not ranked:
            return {}

        ranked.sort(key=lambda item: item[1], reverse=True)
        n = self.top_n if self.top_n > 0 else max(1, len(ranked) // 3)
        winners = [ticker for ticker, _ in ranked[:n]]
        return _equal_weight(winners)


class LowVolatilityTiltStrategy:
    label = "Low-Volatility Tilt"
    description = "Allocates toward the least volatile assets in the universe with monthly rebalancing."
    rebalance_frequency = "monthly"
    min_history_bars = 63

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.lookback_bars = int(self.params.get("lookback_bars", 63))
        self.selection_count = int(self.params.get("selection_count", 0))
        self.min_history_bars = self.lookback_bars

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict[str, Decimal]:
        ranked: list[tuple[str, Decimal]] = []
        for ticker, bars in history.items():
            vol = _annualised_volatility(_closes(bars, as_of_index), self.lookback_bars)
            if vol is None or vol <= 0:
                continue
            ranked.append((ticker, vol))

        if not ranked:
            return {}

        ranked.sort(key=lambda item: item[1])
        n = self.selection_count if self.selection_count > 0 else max(1, len(ranked) // 2)
        selected = ranked[:n]
        inverse_scores = {ticker: Decimal("1") / vol for ticker, vol in selected if vol > 0}
        return _normalize(inverse_scores)


class TrendFollowingTacticalStrategy:
    label = "Trend-Following Timing"
    description = "Owns assets above their long-term moving average and moves the rest to cash."
    rebalance_frequency = "monthly"
    min_history_bars = 200

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.sma_period = int(self.params.get("sma_period", 200))
        self.distance_weighted = bool(self.params.get("distance_weighted", False))
        self.min_history_bars = self.sma_period

    def target_weights(self, history: dict[str, list[Bar]], *, as_of_index: int) -> dict[str, Decimal]:
        selected: dict[str, Decimal] = {}
        for ticker, bars in history.items():
            closes = _closes(bars, as_of_index)
            sma_value = _sma(closes, self.sma_period)
            if sma_value is None or sma_value <= 0:
                continue
            close = closes[-1]
            if close <= sma_value:
                continue
            distance = (close / sma_value) - Decimal("1")
            selected[ticker] = max(distance, Decimal("0.0001")) if self.distance_weighted else Decimal("1")
        return _normalize(selected)


PORTFOLIO_STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "buy_hold_core": {
        "label": CoreBuyHoldStrategy.label,
        "description": CoreBuyHoldStrategy.description,
        "rationale": "Best baseline for a cash-only automated platform: broad diversification, minimal turnover, and a strong evidence base.",
        "strategy_class": CoreBuyHoldStrategy,
        "rebalance_frequency": CoreBuyHoldStrategy.rebalance_frequency,
        "min_history_bars": CoreBuyHoldStrategy.min_history_bars,
    },
    "equal_weight_rebalance": {
        "label": EqualWeightRebalanceStrategy.label,
        "description": EqualWeightRebalanceStrategy.description,
        "rationale": "Adds systematic rebalancing and concentration control without requiring unsupported fundamentals data.",
        "strategy_class": EqualWeightRebalanceStrategy,
        "rebalance_frequency": EqualWeightRebalanceStrategy.rebalance_frequency,
        "min_history_bars": EqualWeightRebalanceStrategy.min_history_bars,
    },
    "cross_sectional_momentum": {
        "label": CrossSectionalMomentumStrategy.label,
        "description": CrossSectionalMomentumStrategy.description,
        "rationale": "One of the strongest price-based effects in the literature, but implemented here with monthly long-only rotation to control friction.",
        "strategy_class": CrossSectionalMomentumStrategy,
        "rebalance_frequency": CrossSectionalMomentumStrategy.rebalance_frequency,
        "min_history_bars": CrossSectionalMomentumStrategy.min_history_bars,
    },
    "low_volatility_tilt": {
        "label": LowVolatilityTiltStrategy.label,
        "description": LowVolatilityTiltStrategy.description,
        "rationale": "Provides a smoother long-only equity tilt using only observed price risk, which fits the current validated data stack.",
        "strategy_class": LowVolatilityTiltStrategy,
        "rebalance_frequency": LowVolatilityTiltStrategy.rebalance_frequency,
        "min_history_bars": LowVolatilityTiltStrategy.min_history_bars,
    },
    "trend_following_tactical": {
        "label": TrendFollowingTacticalStrategy.label,
        "description": TrendFollowingTacticalStrategy.description,
        "rationale": "Adds drawdown-aware cash timing without leverage or shorting, making it practical for Trading 212 Invest accounts.",
        "strategy_class": TrendFollowingTacticalStrategy,
        "rebalance_frequency": TrendFollowingTacticalStrategy.rebalance_frequency,
        "min_history_bars": TrendFollowingTacticalStrategy.min_history_bars,
    },
}

PORTFOLIO_STRATEGY_TYPES = frozenset(PORTFOLIO_STRATEGY_REGISTRY)


def is_portfolio_strategy_type(strategy_type: str) -> bool:
    return strategy_type in PORTFOLIO_STRATEGY_REGISTRY


def get_portfolio_backtest_strategy(strategy_type: str) -> dict[str, Any]:
    strategy = PORTFOLIO_STRATEGY_REGISTRY.get(strategy_type)
    if strategy is None:
        supported = ", ".join(sorted(PORTFOLIO_STRATEGY_REGISTRY))
        raise ValueError(f"Unsupported portfolio strategy '{strategy_type}'. Supported: {supported}")
    return strategy


def list_portfolio_backtest_strategies() -> list[StrategyInfo]:
    return [
        StrategyInfo(
            type=strategy_type,
            label=str(config["label"]),
            description=str(config["description"]),
            rationale=str(config["rationale"]),
            rebalance_frequency=str(config["rebalance_frequency"]),
            min_history_bars=int(config["min_history_bars"]),
        )
        for strategy_type, config in PORTFOLIO_STRATEGY_REGISTRY.items()
    ]
