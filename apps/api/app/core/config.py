"""
Core application configuration.
All settings from environment variables / .env file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file(start: Path) -> Path:
    """Find the nearest ancestor .env without assuming a fixed checkout depth."""
    for directory in (start.parent, *start.parents):
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return Path(".env")


_ENV_FILE = _find_env_file(Path(__file__).resolve())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "T212 CashGuard Trader"
    APP_VERSION: str = "1.0.0"
    APP_MODE: Literal["mock", "paper", "demo", "live"] = "mock"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Security — MUST be set in .env for production.
    # Fallback constants are only used if .env is completely missing so that the
    # app at least starts consistently (avoids random token invalidity on restart).
    SECRET_KEY: str = "CHANGE-ME-set-a-real-secret-in-dot-env-" + "a" * 24
    MASTER_KEY: str = "CHANGE-ME-set-a-real-master-in-dot-env-" + "b" * 24
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8
    ALGORITHM: str = "HS256"

    # Cookie settings (httpOnly auth)
    COOKIE_SECURE: bool = True  # False in local dev (no HTTPS)
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str = ""  # empty = current domain

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard"

    # Redis
    REDIS_URL: str = "redis://:cashguard_redis@localhost:6379/0"

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    # Admin
    ADMIN_EMAIL: str = "admin@localhost"
    ADMIN_PASSWORD: str = "change-me"

    # Trading 212
    T212_API_KEY: str = ""
    T212_API_SECRET: str = ""
    T212_DEMO_API_KEY: str = ""
    T212_DEMO_API_SECRET: str = ""
    T212_LIVE_API_KEY: str = ""
    T212_LIVE_API_SECRET: str = ""
    T212_ENVIRONMENT: Literal["demo", "live"] = "demo"
    T212_DEMO_ORDER_ENABLED: bool = False

    @property
    def t212_base_url(self) -> str:
        if self.T212_ENVIRONMENT == "live":
            return "https://live.trading212.com"
        return "https://demo.trading212.com"

    # Market data
    POLYGON_API_KEY: str = ""  # Get free key at polygon.io
    MARKET_DATA_PROVIDER: str = "mock"  # mock | auto | alpaca | polygon | validated
    BENZINGA_API_KEY: str = ""
    BENZINGA_BASE_URL: str = "https://api.benzinga.com"

    # Alpaca real-time market data (free paper account)
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"  # paper = free account

    # Celery
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # Data retention (days)
    AUDIT_LOG_RETENTION_DAYS: int = 90  # Audit logs kept for 90 days
    RISK_EVENT_RETENTION_DAYS: int = 30  # Risk events kept for 30 days

    # Observability
    SENTRY_DSN: str = ""  # Leave empty to disable Sentry
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1  # 10 % of transactions traced
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.1

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    # Safety — HARDCODED, NOT configurable via UI
    CASH_ONLY_MODE: bool = True
    LIVE_TRADING_ENABLED: bool = False

    # Alerts
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    ALERT_EMAIL_FROM: str = ""
    ALERT_EMAIL_TO: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""
    TELEGRAM_ALLOWED_USER_IDS: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_CONFIRM_WINDOW_SECONDS: int = 120
    DISCORD_WEBHOOK_URL: str = ""  # Discord channel webhook URL
    SLACK_WEBHOOK_URL: str = ""  # Slack incoming webhook URL

    @property
    def telegram_allowed_chat_ids(self) -> set[str]:
        return {
            chat_id.strip()
            for chat_id in self.TELEGRAM_ALLOWED_CHAT_IDS.split(",")
            if chat_id.strip()
        }

    @property
    def telegram_allowed_user_ids(self) -> set[str]:
        return {
            user_id.strip()
            for user_id in self.TELEGRAM_ALLOWED_USER_IDS.split(",")
            if user_id.strip()
        }

    # Drawdown-adaptive sizing thresholds
    DRAWDOWN_TIER1_PCT: float = 0.5  # at this daily loss%, size scales to 75%
    DRAWDOWN_TIER2_PCT: float = 1.0  # at this daily loss%, size scales to 50%
    DRAWDOWN_TIER3_PCT: float = 1.5  # at this daily loss%, size scales to 25%

    # Strategy promotion pipeline thresholds
    STRATEGY_PROMOTION_MIN_DRY_RUN_SIGNALS: int = 3
    STRATEGY_PROMOTION_MIN_DRY_RUN_DAYS: int = 1
    STRATEGY_PROMOTION_MIN_DEMO_ORDERS: int = 3
    STRATEGY_PROMOTION_MIN_DEMO_DAYS: int = 2
    STRATEGY_PROMOTION_MIN_DEMO_FILL_RATE: float = 0.50
    STRATEGY_PROMOTION_MAX_DEMO_ERROR_RATE: float = 0.25
    STRATEGY_PROMOTION_MAX_DEMO_RISK_BLOCK_RATE: float = 0.50

    # Portfolio allocator thresholds
    PORTFOLIO_ALLOCATOR_MIN_SCORE: float = 0.55
    PORTFOLIO_ALLOCATOR_BASE_GROSS_EXPOSURE_PCT: float = 75.0
    PORTFOLIO_ALLOCATOR_VOLATILE_GROSS_EXPOSURE_PCT: float = 45.0
    PORTFOLIO_ALLOCATOR_RISK_OFF_GROSS_EXPOSURE_PCT: float = 25.0
    PORTFOLIO_ALLOCATOR_MAX_SYMBOL_EXPOSURE_PCT: float = 20.0
    PORTFOLIO_ALLOCATOR_MAX_SLEEVE_SYMBOL_EXPOSURE_PCT: float = 60.0
    PORTFOLIO_ALLOCATOR_MAX_RUN_RISK_PCT: float = 2.0


settings = Settings()
