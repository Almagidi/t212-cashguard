from __future__ import annotations

import pytest

SAFE_SECRET = "safe-secret-value-long-enough-for-startup-tests"
SAFE_MASTER_KEY = "safe-master-key-value-long-enough-for-startup-tests"
SAFE_ADMIN_PASSWORD = "safe-admin-password-for-startup-tests"


def _set_safe_startup_settings(monkeypatch: pytest.MonkeyPatch, app_mode: str) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "SECRET_KEY", SAFE_SECRET)
    monkeypatch.setattr(settings, "MASTER_KEY", SAFE_MASTER_KEY)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", SAFE_ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", app_mode == "live")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "live" if app_mode == "live" else "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "configured")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "configured")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "configured" if app_mode == "live" else "")
    monkeypatch.setattr(settings, "MARKET_DATA_PROVIDER", "auto")


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


@pytest.mark.parametrize("app_mode", ["demo", "paper"])
@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("SECRET_KEY", "CHANGE-ME-set-a-real-secret"),
        ("MASTER_KEY", "CHANGE_ME_set_a_real_master"),
        ("ADMIN_PASSWORD", "change-me"),
    ],
)
def test_assert_startup_safe_blocks_default_security_secrets_in_demo_and_paper(
    monkeypatch: pytest.MonkeyPatch,
    app_mode: str,
    setting_name: str,
    unsafe_value: str,
) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    _set_safe_startup_settings(monkeypatch, app_mode)
    monkeypatch.setattr(settings, setting_name, unsafe_value)

    with pytest.raises(RuntimeError) as exc_info:
        assert_startup_safe()

    message = str(exc_info.value)
    assert setting_name in message
    assert unsafe_value not in message


@pytest.mark.parametrize("app_mode", ["demo", "paper"])
def test_assert_startup_safe_allows_safe_security_secrets_in_demo_and_paper(
    monkeypatch: pytest.MonkeyPatch, app_mode: str
) -> None:
    from app.services.startup_validation import assert_startup_safe

    _set_safe_startup_settings(monkeypatch, app_mode)

    report = assert_startup_safe()

    assert report["mode"] == app_mode
    assert report["failures"] == 0


def test_assert_startup_safe_allows_non_secret_failure_in_demo_when_secrets_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    _set_safe_startup_settings(monkeypatch, "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")

    report = assert_startup_safe()

    assert report["mode"] == "demo"
    assert report["failures"] == 1
    assert any(
        check["key"] == "t212_demo_credentials" and check["status"] == "fail"
        for check in report["checks"]
    )


def test_assert_startup_safe_allows_non_secret_failure_in_paper_when_secrets_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import startup_validation

    _set_safe_startup_settings(monkeypatch, "paper")
    fake_report = {
        "status": "fail",
        "mode": "paper",
        "checks": [
            {
                "key": "paper_non_secret_check",
                "label": "Paper non-secret check",
                "status": "fail",
                "detail": "Paper non-secret validation failed.",
            }
        ],
        "failures": 1,
        "warnings": 0,
    }
    monkeypatch.setattr(startup_validation, "build_startup_report", lambda: fake_report)

    report = startup_validation.assert_startup_safe()

    assert report is fake_report


def test_assert_startup_safe_allows_default_security_secrets_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    _set_safe_startup_settings(monkeypatch, "mock")
    monkeypatch.setattr(settings, "SECRET_KEY", "CHANGE-ME-set-a-real-secret")
    monkeypatch.setattr(settings, "MASTER_KEY", "CHANGE-ME-set-a-real-master")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "change-me")

    report = assert_startup_safe()

    assert report["mode"] == "mock"
    assert report["failures"] == 0


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("SECRET_KEY", "CHANGE-ME-set-a-real-secret"),
        ("MASTER_KEY", "CHANGE_ME_set_a_real_master"),
        ("ADMIN_PASSWORD", "change-me"),
    ],
)
def test_assert_startup_safe_still_blocks_default_security_secrets_in_live_mode(
    monkeypatch: pytest.MonkeyPatch, setting_name: str, unsafe_value: str
) -> None:
    from app.core.config import settings
    from app.services.startup_validation import assert_startup_safe

    _set_safe_startup_settings(monkeypatch, "live")
    monkeypatch.setattr(settings, setting_name, unsafe_value)

    with pytest.raises(RuntimeError) as exc_info:
        assert_startup_safe()

    message = str(exc_info.value)
    assert setting_name in message
    assert unsafe_value not in message
