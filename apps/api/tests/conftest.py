"""
Test fixtures.
Uses SQLite in-memory database so tests run without PostgreSQL.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest_asyncio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Override settings BEFORE importing app modules
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["SECRET_KEY"] = "test-secret-key-32-chars-minimum-x"
os.environ["MASTER_KEY"] = "test-master-key-32-chars-minimum-x"
os.environ["APP_MODE"] = "mock"
os.environ["ADMIN_EMAIL"] = "admin@test.com"
os.environ["ADMIN_PASSWORD"] = "testpassword123"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "12345"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "777"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-telegram-secret"

from app.db.session import Base, get_db
from app.main import _login_attempts, _login_lockouts, app

# ─── Test DB ─────────────────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestSessionLocal = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean database for every test."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client with DB override."""

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    _login_attempts.clear()
    _login_lockouts.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    _login_attempts.clear()
    _login_lockouts.clear()
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient, db: AsyncSession) -> str:
    """Create admin user and return auth token."""
    import uuid

    from app.core.security import hash_password
    from app.db.models import AppSettings, RiskProfile, User

    # Create admin user
    user = User(
        id=uuid.uuid4(),
        email="admin@test.com",
        hashed_password=hash_password("testpassword123"),
        is_active=True,
        is_admin=True,
    )
    db.add(user)

    # App settings
    settings_obj = AppSettings(
        id=1,
        theme="dark",
        timezone="UTC",
        auto_trading_enabled=False,
        kill_switch_active=False,
        live_trading_unlocked=False,
    )
    db.add(settings_obj)

    # Default risk profile
    profile = RiskProfile(
        id=uuid.uuid4(),
        name="Test Profile",
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
    db.add(profile)
    await db.commit()

    # Login
    resp = await client.post(
        "/v1/auth/login",
        json={
            "email": "admin@test.com",
            "password": "testpassword123",
        },
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest_asyncio.fixture
def auth_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
