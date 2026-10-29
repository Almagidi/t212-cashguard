"""Resolve backtest parameter intent without changing strategy or risk policy."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from app.backtest.strategy_registry import get_backtest_strategy
from app.services.strategy_presets import PRESET_CONFIGS

_POSITIVE_COUNTS = {
    "orb_minutes",
    "candle_interval_minutes",
    "opening_window_minutes",
    "slot_minutes",
    "n_confirm",
    "trend_ema_fast",
    "trend_ema_slow",
    "min_bars_below_vwap",
    "min_history_sessions",
}


def _validate_params(params: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    validated = deepcopy(params)
    for key, value in params.items():
        if key not in defaults:
            raise ValueError(f"Unknown strategy parameter: {key}")
        default = defaults[key]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            if valid:
                try:
                    valid = math.isfinite(value)
                except OverflowError:
                    valid = False
        else:
            valid = isinstance(value, str)
        if not valid:
            raise ValueError(
                f"Invalid scalar type or non-finite value for strategy parameter: {key}"
            )
        if key in _POSITIVE_COUNTS and value <= 0:
            raise ValueError(f"Strategy parameter {key} must be a positive integer")
        if key.startswith("avoid_") and key.endswith("_minutes") and value < 0:
            raise ValueError(f"Strategy parameter {key} must be non-negative")
        if key.endswith("_utc") and (
            re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) is None or int(value[:2]) > 23
        ):
            raise ValueError(f"Strategy parameter {key} must be HH:MM in UTC")
    return validated


def resolve_backtest_configuration(
    strategy_type: str,
    strategy_params: dict[str, Any],
    preset_key: str | None,
    *,
    parameters_supplied: bool,
) -> dict[str, Any]:
    config = get_backtest_strategy(strategy_type)
    strategy_class = config["strategy_class"]
    defaults = deepcopy(strategy_class().params)
    supplied = _validate_params(strategy_params, defaults)
    label = str(config["label"])
    source = "custom" if supplied else "class_defaults"
    if preset_key is not None:
        if preset_key != strategy_type or preset_key not in PRESET_CONFIGS:
            raise ValueError("preset_key must identify the requested strategy_type")
        preset = PRESET_CONFIGS[preset_key]
        expected = deepcopy(strategy_class(deepcopy(preset.strategy_params)).params)
        _validate_params(expected, defaults)
        if parameters_supplied and supplied != expected:
            raise ValueError(
                "Preset parameters differ from the complete current preset; reload the catalog "
                "or omit preset_key for an explicit custom run"
            )
        effective = expected
        source, label = "preset", preset.label
    else:
        effective = {**defaults, **supplied}
    if (
        strategy_type == "orb"
        and effective["orb_minutes"] // effective["candle_interval_minutes"] < 1
    ):
        raise ValueError("orb_minutes must include at least one candle_interval_minutes bar")
    return {
        "strategy_type": strategy_type,
        "preset_key": preset_key,
        "source": source,
        "label": label,
        "strategy_params": deepcopy(effective),
    }
