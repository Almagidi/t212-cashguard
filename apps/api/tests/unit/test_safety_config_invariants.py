"""Safety-config invariants.

These tests lock the hardcoded, non-configurable safety defaults at the
``Settings`` class level so a future refactor (or an accidental UI/env override
path) cannot silently flip them. They assert against ``model_fields`` defaults,
which are env-independent, so they verify the *code* default regardless of any
local ``.env`` — a stronger guarantee than CI's text grep of ``config.py``.

If one of these fails, treat it as a safety regression, not a flaky test.
"""

from __future__ import annotations

from app.core.config import Settings


def _field_default(name: str):
    """Return the class-level default for a Settings field (env-independent)."""
    return Settings.model_fields[name].default


def test_cash_only_mode_default_is_true():
    # Cash-only mode is hardcoded; CFD/margin/banking flows must stay disabled.
    assert _field_default("CASH_ONLY_MODE") is True


def test_live_trading_disabled_by_default():
    # Live trading must default to disabled. The platform is demo/paper only.
    assert _field_default("LIVE_TRADING_ENABLED") is False


def test_unrealized_pnl_failure_policy_defaults_fail_closed():
    # A failed unrealized-P&L snapshot must fail closed by default, never
    # fall through to the fail-open "assume_zero" behaviour.
    assert _field_default("POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY") == "block_trading"


def test_app_mode_defaults_to_mock():
    # Default runtime mode is the offline mock provider, never "live".
    assert _field_default("APP_MODE") == "mock"


def test_safety_fields_are_typed_booleans():
    # Guard against the defaults being changed to a truthy/falsy non-bool that
    # could subtly defeat ``is True`` / ``is False`` gate checks elsewhere.
    assert isinstance(_field_default("CASH_ONLY_MODE"), bool)
    assert isinstance(_field_default("LIVE_TRADING_ENABLED"), bool)
