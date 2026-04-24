"""Health check routes."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text

from app.api.schemas import DepsHealth, HealthStatus, MarketDataHealth, StartupHealth, WorkersHealth
from app.core.config import settings
from app.core.security import CredentialDecryptionError, decrypt_field
from app.db.models import BrokerConnection
from app.db.session import get_db
from app.services.broker_connection_recovery import mark_broker_connection_reconnect_required
from app.services.feed_health import get_feed_health_snapshot
from app.services.startup_validation import build_startup_report
from app.services.worker_health import build_worker_health

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthStatus)
async def health_live() -> HealthStatus:
    """Liveness probe — always returns 200 if process is running."""
    return HealthStatus(
        status="ok",
        timestamp=datetime.now(UTC),
        version="1.0.0",
        mode=settings.APP_MODE,
    )


@router.get("/ready", response_model=HealthStatus)
async def health_ready(db: AsyncSession = Depends(get_db)) -> HealthStatus:
    """Readiness probe — checks DB connectivity."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB not ready: {exc}") from exc
    return HealthStatus(
        status="ok",
        timestamp=datetime.now(UTC),
        version="1.0.0",
        mode=settings.APP_MODE,
    )


@router.get("/deps", response_model=DepsHealth)
async def health_deps(db: AsyncSession = Depends(get_db)) -> DepsHealth:
    """Dependency health — DB, Redis, broker, market data."""
    startup_report = build_startup_report()
    worker_health = await build_worker_health(db)
    db_ok = "ok"
    redis_ok = "ok"
    broker_status = "mock" if settings.APP_MODE == "mock" else "not_connected"

    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = "error"

    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
    except Exception:
        redis_ok = "error"

    if settings.APP_MODE != "mock":
        try:
            result = await db.execute(
                select(BrokerConnection)
                .order_by(BrokerConnection.updated_at.desc(), BrokerConnection.created_at.desc())
                .limit(1)
            )
            conn = result.scalar_one_or_none()
            if conn:
                try:
                    decrypt_field(conn.api_key_encrypted)
                    decrypt_field(conn.api_secret_encrypted)
                    broker_status = "configured" if conn.is_active else "not_connected"
                except CredentialDecryptionError as exc:
                    await mark_broker_connection_reconnect_required(
                        db,
                        conn,
                        str(exc),
                        commit=True,
                    )
                    broker_status = "reconnect_required"
        except Exception:
            broker_status = "error"

    from app.market_data import get_provider_name
    market_status = get_provider_name()

    return DepsHealth(
        database=db_ok,
        redis=redis_ok,
        broker=broker_status,
        market_data=market_status,
        workers=worker_health["status"],
        startup=startup_report["status"],
    )


@router.get("/startup", response_model=StartupHealth)
async def health_startup() -> StartupHealth:
    return StartupHealth(**build_startup_report())


@router.get("/workers", response_model=WorkersHealth)
async def health_workers(db: AsyncSession = Depends(get_db)) -> WorkersHealth:
    return WorkersHealth(**(await build_worker_health(db)))


@router.get("/market-data", response_model=MarketDataHealth)
async def health_market_data(_: AsyncSession = Depends(get_db)) -> MarketDataHealth:
    return MarketDataHealth(**get_feed_health_snapshot())
