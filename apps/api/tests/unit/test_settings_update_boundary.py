"""Settings PATCH boundary guards.

``PATCH /v1/settings`` applies every field admitted by ``AppSettingsUpdate``
onto the ``AppSettings`` row, which also stores the safety gates
(``auto_trading_enabled``, ``kill_switch_active``, ``live_trading_unlocked``).
Those gates may only change through their dedicated, audited flows
(emergency routes / live-readiness checklist), never through the generic
settings route. These tests pin that boundary so widening it requires a
deliberate, reviewed change here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import AppSettingsUpdate
from app.api.v1.routes.settings import PATCHABLE_SETTINGS_FIELDS

# Fields that the generic settings route is allowed to write.
EXPECTED_PATCHABLE_FIELDS = frozenset(
    {
        "theme",
        "timezone",
        "market_data_provider",
        "daily_stats_reset_time",
    }
)

# AppSettings columns that must never be reachable via PATCH /v1/settings.
SAFETY_GATE_FIELDS = frozenset(
    {
        "auto_trading_enabled",
        "kill_switch_active",
        "live_trading_unlocked",
        "extra",
    }
)


def test_app_settings_update_allowlist_is_pinned() -> None:
    assert frozenset(AppSettingsUpdate.model_fields) == EXPECTED_PATCHABLE_FIELDS, (
        "AppSettingsUpdate gained or lost fields. Every field listed here is "
        "mass-assigned onto the AppSettings row by PATCH /v1/settings. If the "
        "change is intentional, update EXPECTED_PATCHABLE_FIELDS and "
        "PATCHABLE_SETTINGS_FIELDS together, and confirm the new field is not "
        "a safety gate."
    )


def test_app_settings_update_excludes_safety_gate_fields() -> None:
    overlap = frozenset(AppSettingsUpdate.model_fields) & SAFETY_GATE_FIELDS
    assert not overlap, (
        f"AppSettingsUpdate must never expose safety gates via the generic "
        f"settings route: {sorted(overlap)}. Kill switch and auto-trading "
        f"change through /v1/emergency/*; live_trading_unlocked changes "
        f"through the live-readiness checklist."
    )


def test_route_allowlist_matches_schema_fields() -> None:
    assert frozenset(AppSettingsUpdate.model_fields) == PATCHABLE_SETTINGS_FIELDS, (
        "The route-level allowlist in app/api/v1/routes/settings.py must stay "
        "in lockstep with AppSettingsUpdate so the route fails closed if the "
        "schema is widened without updating the route."
    )


@pytest.mark.parametrize("field", sorted(SAFETY_GATE_FIELDS))
def test_app_settings_update_rejects_safety_gate_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        AppSettingsUpdate(**{field: True})


def test_app_settings_update_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppSettingsUpdate(theme="dark", not_a_real_field="x")


def test_app_settings_update_still_accepts_allowlisted_fields() -> None:
    update = AppSettingsUpdate(
        theme="light",
        timezone="Europe/London",
        market_data_provider="mock",
        daily_stats_reset_time="00:00",
    )
    assert update.model_dump(exclude_none=True) == {
        "theme": "light",
        "timezone": "Europe/London",
        "market_data_provider": "mock",
        "daily_stats_reset_time": "00:00",
    }
