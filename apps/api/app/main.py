"""
T212 CashGuard Trader — FastAPI Application.
All routes properly separated into focused modules.
# reload-trigger: 2026-04-08
"""
# mypy: disable-error-code="no-untyped-def,arg-type,assignment,misc,return-value,unreachable"

# ruff: noqa: E402, I001
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import configure_logging, set_trace_id
from app.services.startup_validation import assert_startup_safe

try:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
except ImportError:  # pragma: no cover - optional dependency
    sentry_sdk = None
    CeleryIntegration = FastApiIntegration = SqlalchemyIntegration = None

configure_logging()
log = structlog.get_logger()

# ── Sentry (optional — no-op when SENTRY_DSN is empty or package missing) ────
if settings.SENTRY_DSN and sentry_sdk is not None:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_MODE,
        release="cashguard@1.0.0",
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            CeleryIntegration(),
        ],
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
        # Never send PII: user emails, passwords, or raw request bodies
        send_default_pii=False,
        # Strip auth headers before sending to Sentry
        before_send=lambda event, _hint: _scrub_sentry_event(event),
    )
    log.info("sentry.initialised", environment=settings.APP_MODE)
elif settings.SENTRY_DSN:
    log.warning("sentry.unavailable", detail="SENTRY_DSN is set but sentry_sdk is not installed")


def _scrub_sentry_event(event: dict) -> dict:
    """Remove sensitive headers and cookies from Sentry events."""
    request = event.get("request", {})
    headers = request.get("headers", {})
    for sensitive in ("authorization", "cookie", "x-api-key"):
        if sensitive in headers:
            headers[sensitive] = "[Filtered]"
    return event


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_report = assert_startup_safe()
    log.info(
        "app.startup.validation",
        status=startup_report["status"],
        failures=startup_report["failures"],
        warnings=startup_report["warnings"],
    )
    log.info("app.startup", mode=settings.APP_MODE, version="1.0.0")
    yield
    log.info("app.shutdown")


app = FastAPI(
    title="T212 CashGuard Trader API",
    description="Cash-only intraday trading automation for Trading 212.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Security headers ──────────────────────────────────────────────────────────
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "  # unsafe-inline kept for Swagger UI
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' ws: wss:; "  # allow WebSocket connections
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = _CSP
    if not settings.DEBUG:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Request correlation + structured access log ───────────────────────────────
@app.middleware("http")
async def request_logging(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())[:12]
    set_trace_id(trace_id)
    start = time.perf_counter()
    response = await call_next(request)
    log.info(
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


def _rate_limiting_disabled_for_mock_e2e() -> bool:
    """Disable API rate limiting only for explicit mock-mode E2E validation."""
    return os.getenv("APP_MODE", "").lower() == "mock" and os.getenv(
        "DISABLE_RATE_LIMITING", ""
    ).lower() in {"1", "true", "yes", "on"}


# ── Auth rate limiting ────────────────────────────────────────────────────────
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
LOGIN_RATE_LIMIT_MAX_FAILURES = 5
LOGIN_RATE_LIMIT_LOCKOUT_SECONDS = 60

_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_lockouts: dict[str, float] = {}
_login_lock = asyncio.Lock()


@app.middleware("http")
async def auth_rate_limit(request: Request, call_next):
    if _rate_limiting_disabled_for_mock_e2e():
        return await call_next(request)

    if request.url.path == "/v1/auth/login" and request.method == "POST":
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()

        async with _login_lock:
            attempts = _login_attempts[ip]
            cutoff = now - LOGIN_RATE_LIMIT_WINDOW_SECONDS
            attempts[:] = [t for t in attempts if t >= cutoff]
            lockout_until = _login_lockouts.get(ip)

            if lockout_until and lockout_until > now:
                retry_after = max(1, int(lockout_until - now))
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many login attempts. Try again in 1 minute."},
                    headers={"Retry-After": str(retry_after)},
                )

            if lockout_until and lockout_until <= now:
                _login_lockouts.pop(ip, None)

        response = await call_next(request)

        async with _login_lock:
            attempts = _login_attempts[ip]
            now = time.monotonic()
            cutoff = now - LOGIN_RATE_LIMIT_WINDOW_SECONDS
            attempts[:] = [t for t in attempts if t >= cutoff]

            if response.status_code == 401:
                attempts.append(now)
                if len(attempts) >= LOGIN_RATE_LIMIT_MAX_FAILURES:
                    _login_lockouts[ip] = now + LOGIN_RATE_LIMIT_LOCKOUT_SECONDS
            elif response.status_code < 400:
                attempts.clear()
                _login_lockouts.pop(ip, None)
        return response

    return await call_next(request)


# ── General API rate limiting (100 req/10s per IP, health + metrics exempt) ───
_RATE_LIMIT_RPS_WINDOW = 10  # seconds
_RATE_LIMIT_MAX_REQUESTS = 100  # per window
_RATE_EXEMPT_PREFIXES = ("/v1/health", "/v1/ws/", "/metrics", "/docs", "/openapi")

_ip_request_counts: dict[str, list[float]] = defaultdict(list)
_rate_lock = asyncio.Lock()


@app.middleware("http")
async def general_rate_limit(request: Request, call_next):
    if _rate_limiting_disabled_for_mock_e2e():
        return await call_next(request)

    path = request.url.path
    if any(path.startswith(p) for p in _RATE_EXEMPT_PREFIXES):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_RPS_WINDOW

    async with _rate_lock:
        bucket = _ip_request_counts[ip]
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please slow down."},
                headers={"Retry-After": str(_RATE_LIMIT_RPS_WINDOW)},
            )
        bucket.append(now)

    return await call_next(request)


# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)

# ── Routes — all clean focused modules ───────────────────────────────────────
PREFIX = "/v1"

from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.broker import router as broker_router
from app.api.v1.routes.account import router as account_router
from app.api.v1.routes.instruments import router as instruments_router
from app.api.v1.routes.strategies import router as strategies_router
from app.api.v1.routes.signals import router as signals_router
from app.api.v1.routes.orders import router as orders_router
from app.api.v1.routes.positions import router as positions_router
from app.api.v1.routes.risk import router as risk_router
from app.api.v1.routes.alerts import router as alerts_router
from app.api.v1.routes.settings import router as settings_router
from app.api.v1.routes.emergency import router as emergency_router
from app.api.v1.routes.reports import router as reports_router
from app.api.v1.routes.trades import router as trades_router
from app.api.v1.routes.audit import router as audit_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.telegram import router as telegram_router
from app.api.v1.routes.intelligence import router as intelligence_router, regime_router
from app.api.v1.routes.operator import router as operator_router
from app.api.v1.routes.dca import router as dca_router
from app.api.metrics import router as metrics_router

for r in [
    auth_router,
    broker_router,
    account_router,
    instruments_router,
    strategies_router,
    signals_router,
    orders_router,
    positions_router,
    risk_router,
    alerts_router,
    settings_router,
    emergency_router,
    reports_router,
    trades_router,
    audit_router,
    health_router,
    telegram_router,
    intelligence_router,
    operator_router,
    dca_router,
]:
    app.include_router(r, prefix=PREFIX)

app.include_router(metrics_router)  # /metrics — no prefix

from app.api.v1.routes.backtest import router as backtest_router, attribution_router

app.include_router(backtest_router, prefix=PREFIX)
app.include_router(attribution_router, prefix=PREFIX)

# ── WebSocket ─────────────────────────────────────────────────────────────────
from app.api.v1.routes.ws import router as ws_router

app.include_router(ws_router)  # /v1/ws/live  (WebSocket, no prefix)
app.include_router(regime_router, prefix=PREFIX)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": "T212 CashGuard Trader API",
        "version": "1.0.0",
        "mode": settings.APP_MODE,
        "docs": "/docs",
        "metrics": "/metrics",
        "health": "/v1/health/live",
        "cash_only": True,
    }
