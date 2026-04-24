from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from app.db.models import RiskProfile, Strategy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

StrategyPresetKey = Literal[
    "orb",
    "opening_fade",
    "vwap_reclaim",
    "closing_momentum",
    "intraday_periodicity",
]


@dataclass(frozen=True)
class RiskTemplateConfig:
    name: str
    max_risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    max_open_positions: int
    max_position_size_pct: Decimal
    max_trades_per_day: int
    stop_after_consecutive_losses: int
    symbol_cooldown_seconds: int
    force_flat_eod: bool
    summary: str


@dataclass(frozen=True)
class StrategyPresetConfig:
    key: StrategyPresetKey
    label: str
    description: str
    session_window: str
    style: str
    default_tickers: tuple[str, ...]
    strategy_params: dict[str, Any]
    risk_template: RiskTemplateConfig
    session_start: str = "09:30"
    session_end: str = "16:00"
    extended_hours: bool = False
    eod_flatten: bool = True


PRESET_CONFIGS: dict[StrategyPresetKey, StrategyPresetConfig] = {
    "orb": StrategyPresetConfig(
        key="orb",
        label="Opening Range Breakout",
        description="Trend continuation in highly liquid names after a clean opening range break.",
        session_window="09:35-15:25 ET",
        style="Momentum breakout",
        default_tickers=("AAPL", "MSFT", "NVDA", "AMZN", "META", "SPY"),
        strategy_params={
            "orb_minutes": 15,
            "min_range_pct": 0.2,
            "max_range_pct": 2.2,
            "min_rvol": 1.6,
            "atr_stop_multiplier": 1.8,
            "reward_risk_ratio_min": 1.7,
            "risk_per_trade_pct": 0.6,
            "max_position_pct": 6.0,
            "avoid_first_minutes": 5,
            "avoid_last_minutes": 35,
            "allow_short": False,
        },
        risk_template=RiskTemplateConfig(
            name="Demo ORB Breakout",
            max_risk_per_trade_pct=Decimal("0.60"),
            max_daily_loss_pct=Decimal("1.80"),
            max_open_positions=3,
            max_position_size_pct=Decimal("6.00"),
            max_trades_per_day=4,
            stop_after_consecutive_losses=2,
            symbol_cooldown_seconds=1800,
            force_flat_eod=True,
            summary="0.60% risk per trade, 1.80% daily stop, max 3 open positions, 2-loss pause.",
        ),
    ),
    "opening_fade": StrategyPresetConfig(
        key="opening_fade",
        label="Gap Reversal Fade",
        description="Fade outsized overnight shocks only when the open starts to mean-revert on liquid names.",
        session_window="09:35-10:10 ET",
        style="Gap mean reversion",
        default_tickers=("AAPL", "TSLA", "NVDA", "AMD", "META", "SPY"),
        strategy_params={
            "min_gap_pct": 2.0,
            "max_gap_pct": 5.0,
            "min_rvol": 1.7,
            "n_confirm": 2,
            "chop_threshold": 52.0,
            "atr_stop_multiplier": 1.35,
            "risk_per_trade_pct": 0.45,
            "max_position_pct": 5.0,
            "fade_window_minutes": 40,
            "avoid_first_minutes": 5,
            "allow_short": False,
        },
        risk_template=RiskTemplateConfig(
            name="Demo Gap Fade",
            max_risk_per_trade_pct=Decimal("0.45"),
            max_daily_loss_pct=Decimal("1.50"),
            max_open_positions=2,
            max_position_size_pct=Decimal("5.00"),
            max_trades_per_day=3,
            stop_after_consecutive_losses=2,
            symbol_cooldown_seconds=2700,
            force_flat_eod=True,
            summary="0.45% risk per trade, 1.50% daily stop, max 2 open positions, longer cooldown.",
        ),
    ),
    "vwap_reclaim": StrategyPresetConfig(
        key="vwap_reclaim",
        label="VWAP Reclaim",
        description="Buy intraday pullbacks that reclaim VWAP with volume confirmation in healthy trends.",
        session_window="10:30-15:25 ET",
        style="VWAP continuation",
        default_tickers=("AAPL", "MSFT", "NVDA", "QQQ", "SPY", "AMZN"),
        strategy_params={
            "min_rvol": 1.6,
            "atr_stop_multiplier": 1.35,
            "reward_risk_ratio_min": 1.5,
            "risk_per_trade_pct": 0.4,
            "max_position_pct": 5.0,
            "min_bars_below_vwap": 3,
            "avoid_first_minutes": 60,
            "avoid_last_minutes": 35,
        },
        risk_template=RiskTemplateConfig(
            name="Demo VWAP Reclaim",
            max_risk_per_trade_pct=Decimal("0.40"),
            max_daily_loss_pct=Decimal("1.50"),
            max_open_positions=2,
            max_position_size_pct=Decimal("5.00"),
            max_trades_per_day=4,
            stop_after_consecutive_losses=2,
            symbol_cooldown_seconds=1800,
            force_flat_eod=True,
            summary="0.40% risk per trade, 1.50% daily stop, max 2 open positions, 30m symbol cooldown.",
        ),
    ),
    "closing_momentum": StrategyPresetConfig(
        key="closing_momentum",
        label="Closing Momentum",
        description="Join persistent strength only in the final half-hour when the session stays above VWAP.",
        session_window="15:30-15:55 ET",
        style="Late-session continuation",
        default_tickers=("AAPL", "MSFT", "NVDA", "META", "SPY", "QQQ"),
        strategy_params={
            "min_opening_return_pct": 0.45,
            "min_day_return_pct": 0.30,
            "min_rvol": 1.2,
            "atr_stop_multiplier": 1.2,
            "reward_risk_ratio_min": 1.35,
            "risk_per_trade_pct": 0.35,
            "max_position_pct": 4.5,
        },
        risk_template=RiskTemplateConfig(
            name="Demo Closing Momentum",
            max_risk_per_trade_pct=Decimal("0.35"),
            max_daily_loss_pct=Decimal("1.25"),
            max_open_positions=2,
            max_position_size_pct=Decimal("4.50"),
            max_trades_per_day=2,
            stop_after_consecutive_losses=2,
            symbol_cooldown_seconds=3600,
            force_flat_eod=True,
            summary="0.35% risk per trade, 1.25% daily stop, max 2 trades/day, one-hour cooldown.",
        ),
    ),
    "intraday_periodicity": StrategyPresetConfig(
        key="intraday_periodicity",
        label="Intraday Periodicity",
        description="Trade recurring positive time-of-day slots only when recent session history agrees.",
        session_window="12:30-15:00 ET",
        style="Time-of-day continuation",
        default_tickers=("AAPL", "MSFT", "NVDA", "AMZN", "META", "QQQ"),
        strategy_params={
            "min_history_sessions": 5,
            "min_avg_slot_return_pct": 0.10,
            "min_positive_ratio": 0.65,
            "min_live_slot_return_pct": 0.08,
            "min_rvol": 1.1,
            "atr_stop_multiplier": 1.1,
            "reward_risk_ratio_min": 1.25,
            "risk_per_trade_pct": 0.3,
            "max_position_pct": 4.0,
        },
        risk_template=RiskTemplateConfig(
            name="Demo Intraday Periodicity",
            max_risk_per_trade_pct=Decimal("0.30"),
            max_daily_loss_pct=Decimal("1.25"),
            max_open_positions=2,
            max_position_size_pct=Decimal("4.00"),
            max_trades_per_day=2,
            stop_after_consecutive_losses=2,
            symbol_cooldown_seconds=3600,
            force_flat_eod=True,
            summary="0.30% risk per trade, 1.25% daily stop, max 2 trades/day, high selectivity.",
        ),
    ),
}


def list_strategy_presets() -> list[StrategyPresetConfig]:
    return list(PRESET_CONFIGS.values())


def get_strategy_preset(preset_key: StrategyPresetKey) -> StrategyPresetConfig:
    return PRESET_CONFIGS[preset_key]


async def ensure_preset_risk_profile(
    db: AsyncSession,
    preset_key: StrategyPresetKey,
) -> RiskProfile:
    preset = get_strategy_preset(preset_key)
    template = preset.risk_template
    result = await db.execute(select(RiskProfile).where(RiskProfile.name == template.name).limit(1))
    profile = result.scalar_one_or_none()
    if profile:
        return profile

    profile = RiskProfile(
        id=uuid.uuid4(),
        name=template.name,
        max_risk_per_trade_pct=template.max_risk_per_trade_pct,
        max_daily_loss_pct=template.max_daily_loss_pct,
        max_open_positions=template.max_open_positions,
        max_position_size_pct=template.max_position_size_pct,
        max_trades_per_day=template.max_trades_per_day,
        stop_after_consecutive_losses=template.stop_after_consecutive_losses,
        symbol_cooldown_seconds=template.symbol_cooldown_seconds,
        force_flat_eod=template.force_flat_eod,
        is_default=False,
    )
    db.add(profile)
    await db.flush()
    return profile


async def build_unique_strategy_name(
    db: AsyncSession,
    base_name: str,
) -> str:
    existing_names = set(
        (await db.execute(select(Strategy.name).where(Strategy.name.like(f"{base_name}%")))).scalars().all()
    )
    if base_name not in existing_names:
        return base_name

    suffix = 2
    while f"{base_name} {suffix}" in existing_names:
        suffix += 1
    return f"{base_name} {suffix}"
