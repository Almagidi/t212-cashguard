from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import RiskProfile
from app.services.strategy_presets import (
    PRESET_CONFIGS,
    build_unique_strategy_name,
    ensure_preset_risk_profile,
    get_strategy_preset,
    list_strategy_presets,
)


def _db_with_scalar(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _db_with_names(names):
    scalars = MagicMock()
    scalars.all.return_value = names
    result = MagicMock()
    result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def test_list_strategy_presets_returns_all_configs():
    presets = list_strategy_presets()

    assert len(presets) == len(PRESET_CONFIGS)
    assert {preset.key for preset in presets} == set(PRESET_CONFIGS)


def test_get_strategy_preset_returns_config_by_key():
    preset = get_strategy_preset("orb")

    assert preset.key == "orb"
    assert preset.label == "Opening Range Breakout"
    assert preset.risk_template.max_open_positions == 3


@pytest.mark.asyncio
async def test_ensure_preset_risk_profile_reuses_existing_profile():
    profile = MagicMock(spec=RiskProfile)
    db = _db_with_scalar(profile)

    assert await ensure_preset_risk_profile(db, "vwap_reclaim") is profile
    db.add.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_preset_risk_profile_creates_profile_from_template():
    db = _db_with_scalar(None)

    profile = await ensure_preset_risk_profile(db, "opening_fade")

    template = get_strategy_preset("opening_fade").risk_template
    assert profile.name == template.name
    assert profile.max_risk_per_trade_pct == template.max_risk_per_trade_pct
    assert profile.max_daily_loss_pct == template.max_daily_loss_pct
    assert profile.max_open_positions == template.max_open_positions
    assert profile.max_position_size_pct == template.max_position_size_pct
    assert profile.max_trades_per_day == template.max_trades_per_day
    assert profile.stop_after_consecutive_losses == template.stop_after_consecutive_losses
    assert profile.symbol_cooldown_seconds == template.symbol_cooldown_seconds
    assert profile.force_flat_eod == template.force_flat_eod
    assert profile.is_default is False
    db.add.assert_called_once_with(profile)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_unique_strategy_name_returns_base_when_unused():
    db = _db_with_names(["Demo ORB Breakout 2"])

    assert await build_unique_strategy_name(db, "Demo ORB Breakout") == "Demo ORB Breakout"


@pytest.mark.asyncio
async def test_build_unique_strategy_name_increments_until_available():
    db = _db_with_names([
        "Demo ORB Breakout",
        "Demo ORB Breakout 2",
        "Demo ORB Breakout 3",
    ])

    assert await build_unique_strategy_name(db, "Demo ORB Breakout") == "Demo ORB Breakout 4"
