"""
Initialise a SQLite integration-test database.

Creates the schema via SQLAlchemy metadata (same models as production) and
seeds the minimum rows needed by the T-OPS-009 Playwright integration suite:

  • Admin user
  • AppSettings (id=1, kill_switch_active=True)
  • VenueConfig for t212  – kill switch ON
  • VenueConfig for kraken – kill switch ON
  • RiskProfile (default)
  • DcaConfig BTC/USD (paper-only, disabled)
  • DcaConfig ETH/USD (paper-only, disabled)

Usage:
    INTEGRATION_DB_PATH=/tmp/t212_integration_test.db \\
    DATABASE_URL="sqlite+aiosqlite:////tmp/t212_integration_test.db" \\
    ... \\
    PYTHONPATH=. python3.12 scripts/init_integration_db.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from decimal import Decimal

# env overrides BEFORE any app import so Settings picks them up
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/t212_integration_test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SECRET_KEY", "integration-test-secret-key-32-chars-x")
os.environ.setdefault("MASTER_KEY", "integration-test-master-key-32-chars-x")
os.environ.setdefault("APP_MODE", "mock")
os.environ.setdefault("ADMIN_EMAIL", "admin@localhost")
os.environ.setdefault("ADMIN_PASSWORD", "change-me")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.db.models import AppSettings, DcaConfig, RiskProfile, User, VenueConfig
from app.db.session import Base

DB_PATH = os.environ.get("INTEGRATION_DB_PATH", "/tmp/t212_integration_test.db")
DB_URL = os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@localhost")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")


async def init_db() -> None:
    engine = create_async_engine(
        DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        db.add(User(
            id=uuid.uuid4(),
            email=ADMIN_EMAIL,
            hashed_password=hash_password(ADMIN_PASSWORD),
            is_active=True,
            is_admin=True,
        ))

        db.add(AppSettings(
            id=1,
            theme="dark",
            timezone="UTC",
            market_data_provider="mock",
            auto_trading_enabled=False,
            kill_switch_active=True,
            live_trading_unlocked=False,
        ))

        db.add(VenueConfig(
            venue="t212",
            kill_switch_active=True,
            auto_trading_enabled=False,
            degraded_mode_active=False,
            note="Integration test: t212 kill switch active.",
        ))

        db.add(VenueConfig(
            venue="kraken",
            kill_switch_active=True,
            auto_trading_enabled=False,
            degraded_mode_active=False,
            note="Integration test: kraken kill switch active.",
        ))

        db.add(RiskProfile(
            id=uuid.uuid4(),
            name="Integration Test Default",
            max_risk_per_trade_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("3.0"),
            max_open_positions=5,
            max_position_size_pct=Decimal("10.0"),
            max_trades_per_day=20,
            stop_after_consecutive_losses=3,
            symbol_cooldown_seconds=300,
            force_flat_eod=True,
            is_default=True,
        ))

        for ticker in ("BTC/USD", "ETH/USD"):
            db.add(DcaConfig(
                id=uuid.uuid4(),
                ticker=ticker,
                venue="kraken",
                cadence_days=7,
                fixed_cash_amount=Decimal("100"),
                dip_buy_enabled=True,
                dip_threshold_pct=Decimal("5.0"),
                dip_buy_multiplier=Decimal("2.0"),
                dip_ema_period=20,
                min_cash_reserve=Decimal("500"),
                max_position_percent=Decimal("25.0"),
                paper_only=True,
                enabled=False,
            ))

        await db.commit()

    await engine.dispose()
    print(f"✓ Integration DB initialised: {DB_PATH}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(init_db())
