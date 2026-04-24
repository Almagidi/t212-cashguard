from __future__ import annotations

import pytest


def test_build_startup_report_is_clean_in_mock_mode() -> None:
    from app.services.startup_validation import build_startup_report

    report = build_startup_report()

    assert report["mode"] == "mock"
    assert report["status"] == "pass"
    assert report["failures"] == 0


def test_assert_startup_safe_blocks_unsafe_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "mock")

    with pytest.raises(RuntimeError):
        assert_startup_safe()
