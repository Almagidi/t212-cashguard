"""
Database seeder — creates admin user, default risk profile,
demo instruments, and demo strategy.
Run: python -m app.db.seed
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.db.models import (
    AppSettings,
    Instrument,
    RiskProfile,
    Strategy,
    User,
)


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        # --- Admin user ---
        result = await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                id=uuid.uuid4(),
                email=settings.ADMIN_EMAIL,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                is_active=True,
                is_admin=True,
            )
            db.add(user)
            print(f"✓ Created admin user: {settings.ADMIN_EMAIL}")
        else:
            # Always sync the password from .env so re-seeding after a
            # credential change doesn't leave stale hashes in the DB.
            user.hashed_password = hash_password(settings.ADMIN_PASSWORD)
            print(f"✓ Updated admin password for: {settings.ADMIN_EMAIL}")

        # --- App settings ---
        result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
        app_settings = result.scalar_one_or_none()
        if not app_settings:
            app_settings = AppSettings(
                id=1,
                theme="dark",
                timezone="America/New_York",
                market_data_provider="mock",
                auto_trading_enabled=False,
                kill_switch_active=False,
                live_trading_unlocked=False,
            )
            db.add(app_settings)
            print("✓ Created default app settings")

        # --- Default risk profile ---
        result = await db.execute(select(RiskProfile).where(RiskProfile.is_default == True))  # noqa: E712
        risk_profile = result.scalar_one_or_none()
        if not risk_profile:
            risk_profile = RiskProfile(
                id=uuid.uuid4(),
                name="Conservative Default",
                max_risk_per_trade_pct=Decimal("1.0"),
                max_daily_loss_pct=Decimal("3.0"),
                max_open_positions=5,
                max_position_size_pct=Decimal("10.0"),
                max_trades_per_day=20,
                stop_after_consecutive_losses=3,
                symbol_cooldown_seconds=300,
                force_flat_eod=True,
                is_default=True,
            )
            db.add(risk_profile)
            print("✓ Created default risk profile")

        await db.flush()

        # --- Demo instruments ---
        demo_instruments = [
            {"ticker": "AAPL", "name": "Apple Inc.", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "MSFT", "name": "Microsoft Corporation", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "TSLA", "name": "Tesla Inc.", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "GOOGL", "name": "Alphabet Inc.", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "AMZN", "name": "Amazon.com Inc.", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "NVDA", "name": "NVIDIA Corporation", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "META", "name": "Meta Platforms Inc.", "type": "STOCK", "currency_code": "USD"},
            {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "type": "ETF", "currency_code": "USD"},
            {"ticker": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF", "currency_code": "USD"},
            {"ticker": "IWM", "name": "iShares Russell 2000 ETF", "type": "ETF", "currency_code": "USD"},
        ]

        for inst_data in demo_instruments:
            result = await db.execute(select(Instrument).where(Instrument.ticker == inst_data["ticker"]))
            existing = result.scalar_one_or_none()
            if not existing:
                inst = Instrument(
                    id=uuid.uuid4(),
                    ticker=inst_data["ticker"],
                    name=inst_data["name"],
                    type=inst_data["type"],
                    currency_code=inst_data["currency_code"],
                    extended_hours=True,
                    working_schedule_id=1,
                    trading_enabled=True,
                    synced_at=datetime.now(timezone.utc),
                )
                db.add(inst)
        print(f"✓ Seeded {len(demo_instruments)} instruments")

        # --- Demo ORB strategy ---
        result = await db.execute(select(Strategy).where(Strategy.name == "ORB Demo Strategy"))
        strategy = result.scalar_one_or_none()
        if not strategy:
            result2 = await db.execute(select(RiskProfile).where(RiskProfile.is_default == True))  # noqa: E712
            rp = result2.scalar_one_or_none()
            strategy = Strategy(
                id=uuid.uuid4(),
                name="ORB Demo Strategy",
                type="orb",
                description="Opening Range Breakout — trades the first 15-minute candle breakout at session open.",
                is_enabled=False,
                is_live=False,
                risk_profile_id=rp.id if rp else None,
                params={
                    "orb_minutes": 15,
                    "min_range_pct": 0.3,
                    "max_range_pct": 3.0,
                    "risk_reward_ratio": 2.0,
                },
                allowed_tickers=["AAPL", "MSFT", "TSLA", "NVDA", "SPY"],
                session_start="09:30",
                session_end="16:00",
                extended_hours=False,
                eod_flatten=True,
            )
            db.add(strategy)
            print("✓ Created demo ORB strategy")

        await db.commit()
        print("\n✅ Database seeding complete")


if __name__ == "__main__":
    asyncio.run(seed())
