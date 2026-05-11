"""
Startup configuration validation for safer operations and clearer diagnostics.
"""
from __future__ import annotations

from typing import Any, Literal

from app.core.config import settings
from app.services.safety_policy import credentials_configured_status

StartupStatus = Literal["pass", "warn", "fail"]


def _check(
    *,
    key: str,
    label: str,
    status: StartupStatus,
    detail: str,
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
    }


def build_startup_report() -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    checks.append(_check(
        key="secret_key",
        label="JWT secret configured",
        status="pass" if not settings.SECRET_KEY.startswith("CHANGE-ME-") else "warn",
        detail=(
            "SECRET_KEY is not using the repository fallback value."
            if not settings.SECRET_KEY.startswith("CHANGE-ME-")
            else "SECRET_KEY is still using the built-in fallback and should be replaced."
        ),
    ))
    checks.append(_check(
        key="master_key",
        label="Master key configured",
        status="pass" if not settings.MASTER_KEY.startswith("CHANGE-ME-") else "warn",
        detail=(
            "MASTER_KEY is not using the repository fallback value."
            if not settings.MASTER_KEY.startswith("CHANGE-ME-")
            else "MASTER_KEY is still using the built-in fallback and should be replaced."
        ),
    ))
    checks.append(_check(
        key="admin_password",
        label="Admin password changed from default",
        status="pass" if settings.ADMIN_PASSWORD != "change-me" else "warn",
        detail=(
            "Admin password differs from the documented default."
            if settings.ADMIN_PASSWORD != "change-me"
            else "Admin password is still the default `change-me`."
        ),
    ))

    live_mode = settings.APP_MODE == "live"
    live_execution_enabled = bool(settings.LIVE_TRADING_ENABLED)
    environment_matches_mode = (
        (live_mode and settings.T212_ENVIRONMENT == "live")
        or (not live_mode and settings.T212_ENVIRONMENT == "demo")
    )
    checks.append(_check(
        key="broker_environment_alignment",
        label="Broker environment aligned with app mode",
        status="pass" if environment_matches_mode else ("fail" if live_mode else "warn"),
        detail=(
            "Trading 212 environment matches the selected app mode."
            if environment_matches_mode
            else "T212_ENVIRONMENT does not match the current app mode."
        ),
    ))
    checks.append(_check(
        key="live_execution_flag",
        label="Live execution flag enabled for live mode",
        status="pass" if (not live_mode or live_execution_enabled) else "fail",
        detail=(
            "Live execution flag is enabled."
            if live_mode and live_execution_enabled
            else "Live execution flag is not required outside live mode."
            if not live_mode
            else "APP_MODE is live but LIVE_TRADING_ENABLED is false."
        ),
    ))
    credential_status = credentials_configured_status()
    checks.append(_check(
        key="t212_demo_credentials",
        label="Trading 212 demo credentials configured",
        status="pass" if settings.APP_MODE != "demo" or settings.T212_DEMO_API_KEY else "warn",
        detail=f"TRADING212_DEMO_API_KEY: {credential_status['T212_DEMO_API_KEY']}",
    ))
    checks.append(_check(
        key="t212_live_credentials",
        label="Trading 212 live credentials configured",
        status="pass" if not live_mode or settings.T212_LIVE_API_KEY else "fail",
        detail=f"TRADING212_LIVE_API_KEY: {credential_status['T212_LIVE_API_KEY']}",
    ))
    checks.append(_check(
        key="live_credentials_guarded",
        label="Live credentials guarded by live flag",
        status="pass" if live_execution_enabled or not settings.T212_LIVE_API_KEY else "warn",
        detail=(
            "Live credentials are configured and LIVE_TRADING_ENABLED=true."
            if live_execution_enabled and settings.T212_LIVE_API_KEY
            else "Live credentials are configured but LIVE_TRADING_ENABLED=false; live broker calls remain blocked."
            if settings.T212_LIVE_API_KEY
            else "No live Trading 212 credentials are configured."
        ),
    ))

    mock_provider = settings.MARKET_DATA_PROVIDER == "mock"
    mock_market_in_live = live_mode and mock_provider
    market_data_status: StartupStatus
    market_data_detail: str
    if mock_market_in_live:
        market_data_status = "fail"
        market_data_detail = "Live mode is configured with mock market data, which is unsafe for real trading."
    elif settings.APP_MODE == "demo" and mock_provider:
        market_data_status = "warn"
        market_data_detail = "Market data provider is mock-backed; acceptable for demo mode but not ideal for realism."
    else:
        market_data_status = "pass"
        market_data_detail = "Market data provider is appropriate for the current mode."
    checks.append(_check(
        key="market_data_provider",
        label="Market data provider suitable for mode",
        status=market_data_status,
        detail=market_data_detail,
    ))

    telegram_ready = bool(
        settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_CHAT_ID
        and settings.TELEGRAM_WEBHOOK_SECRET
        and (
            settings.telegram_allowed_chat_ids
            or settings.telegram_allowed_user_ids
        )
    )
    checks.append(_check(
        key="telegram_supervision",
        label="Telegram supervision configured",
        status="pass" if telegram_ready else ("warn" if live_mode else "pass"),
        detail=(
            "Telegram monitoring and control are configured."
            if telegram_ready
            else "Telegram supervision is incomplete; configure it before enabling live automation."
            if live_mode
            else "Telegram supervision is optional outside live mode."
        ),
    ))

    failures = [check for check in checks if check["status"] == "fail"]
    warnings = [check for check in checks if check["status"] == "warn"]

    return {
        "status": "fail" if failures else ("warn" if warnings else "pass"),
        "mode": settings.APP_MODE,
        "checks": checks,
        "failures": len(failures),
        "warnings": len(warnings),
    }


def assert_startup_safe() -> dict[str, Any]:
    report = build_startup_report()
    if settings.APP_MODE == "live" and report["failures"] > 0:
        details = "; ".join(check["detail"] for check in report["checks"] if check["status"] == "fail")
        raise RuntimeError(f"Unsafe live startup configuration: {details}")
    return report
