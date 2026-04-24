"""
Strategy registry for research and backtesting.
"""
from __future__ import annotations

from typing import Any

from app.backtest.data_fetcher import ORB_PARAM_GRID
from app.strategies.closing_momentum import ClosingMomentumStrategy
from app.strategies.intraday_periodicity import IntradayPeriodicityStrategy
from app.strategies.opening_fade import OpeningFadeStrategy
from app.strategies.orb_production import OpeningRangeBreakoutStrategy
from app.strategies.vwap_reclaim import VWAPReclaimStrategy

FADE_PARAM_GRID: list[dict[str, Any]] = [
    {
        "min_gap_pct": 1.5,
        "max_gap_pct": 4.0,
        "min_rvol": 1.5,
        "n_confirm": 2,
        "risk_per_trade_pct": 0.5,
        "reward_risk_ratio_min": 1.5,
    },
    {
        "min_gap_pct": 2.0,
        "max_gap_pct": 5.0,
        "min_rvol": 1.8,
        "n_confirm": 2,
        "risk_per_trade_pct": 0.5,
        "reward_risk_ratio_min": 1.8,
    },
    {
        "min_gap_pct": 1.5,
        "max_gap_pct": 4.5,
        "min_rvol": 2.0,
        "n_confirm": 3,
        "risk_per_trade_pct": 0.4,
        "reward_risk_ratio_min": 1.5,
    },
]

VWAP_PARAM_GRID: list[dict[str, Any]] = [
    {
        "min_rvol": 1.5,
        "atr_stop_multiplier": 1.5,
        "reward_risk_ratio_min": 1.5,
        "risk_per_trade_pct": 0.5,
        "max_position_pct": 6.0,
    },
    {
        "min_rvol": 1.8,
        "atr_stop_multiplier": 1.2,
        "reward_risk_ratio_min": 1.6,
        "risk_per_trade_pct": 0.6,
        "max_position_pct": 6.0,
    },
    {
        "min_rvol": 2.0,
        "atr_stop_multiplier": 1.5,
        "reward_risk_ratio_min": 1.8,
        "risk_per_trade_pct": 0.5,
        "max_position_pct": 5.0,
    },
]

CLOSING_MOMENTUM_PARAM_GRID: list[dict[str, Any]] = [
    {
        "min_opening_return_pct": 0.35,
        "min_day_return_pct": 0.25,
        "min_rvol": 1.15,
        "atr_stop_multiplier": 1.2,
        "reward_risk_ratio_min": 1.3,
    },
    {
        "min_opening_return_pct": 0.5,
        "min_day_return_pct": 0.35,
        "min_rvol": 1.25,
        "atr_stop_multiplier": 1.1,
        "reward_risk_ratio_min": 1.4,
    },
]

INTRADAY_PERIODICITY_PARAM_GRID: list[dict[str, Any]] = [
    {
        "min_history_sessions": 4,
        "min_avg_slot_return_pct": 0.08,
        "min_positive_ratio": 0.60,
        "min_live_slot_return_pct": 0.05,
        "min_rvol": 1.05,
    },
    {
        "min_history_sessions": 5,
        "min_avg_slot_return_pct": 0.10,
        "min_positive_ratio": 0.65,
        "min_live_slot_return_pct": 0.08,
        "min_rvol": 1.15,
    },
]

BACKTEST_STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "orb": {
        "label": "Opening Range Breakout",
        "description": "Trend-following breakout strategy with ATR-based sizing and trend filters.",
        "strategy_class": OpeningRangeBreakoutStrategy,
        "param_grid": ORB_PARAM_GRID,
    },
    "opening_fade": {
        "label": "Opening Fade",
        "description": "Mean-reversion fade of opening gaps with confirmation and choppiness filters.",
        "strategy_class": OpeningFadeStrategy,
        "param_grid": FADE_PARAM_GRID,
    },
    "vwap_reclaim": {
        "label": "VWAP Reclaim",
        "description": "Intraday VWAP reclaim continuation setup with RVOL and ATR controls.",
        "strategy_class": VWAPReclaimStrategy,
        "param_grid": VWAP_PARAM_GRID,
    },
    "closing_momentum": {
        "label": "Closing Momentum",
        "description": "Buy late-session strength only when the opening impulse persists into the close.",
        "strategy_class": ClosingMomentumStrategy,
        "param_grid": CLOSING_MOMENTUM_PARAM_GRID,
    },
    "intraday_periodicity": {
        "label": "Intraday Periodicity",
        "description": "Trades same-slot continuation only when recent sessions show a repeatable positive time-of-day edge.",
        "strategy_class": IntradayPeriodicityStrategy,
        "param_grid": INTRADAY_PERIODICITY_PARAM_GRID,
    },
}


def get_backtest_strategy(strategy_type: str) -> dict[str, Any]:
    strategy = BACKTEST_STRATEGY_REGISTRY.get(strategy_type)
    if strategy is None:
        supported = ", ".join(sorted(BACKTEST_STRATEGY_REGISTRY))
        raise ValueError(f"Unsupported strategy_type '{strategy_type}'. Supported: {supported}")
    return strategy


def list_backtest_strategies() -> list[dict[str, str]]:
    return [
        {
            "type": strategy_type,
            "label": str(config["label"]),
            "description": str(config["description"]),
        }
        for strategy_type, config in BACKTEST_STRATEGY_REGISTRY.items()
    ]
