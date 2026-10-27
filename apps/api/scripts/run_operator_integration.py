"""Run the real operator E2E against a new, migrated, loopback PostgreSQL database.

DATABASE_URL supplies an existing local test cluster/role with CREATEDB permission.
Its database is never migrated, seeded or reset. Only this run's random database is
changed, and cleanup refuses to force-drop sessions or touch a replaced database.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

API_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = API_ROOT.parent / "web"


def postgres_url(value: str) -> URL:
    try:
        url = make_url(value)
        if (
            url.drivername != "postgresql+asyncpg"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or not url.database
            or url.query
        ):
            raise ValueError
        return url
    except Exception:
        raise ValueError(
            "DATABASE_URL must name an explicit loopback PostgreSQL test cluster."
        ) from None


def fixture_environment(source: Mapping[str, str], database: str) -> dict[str, str]:
    if not re.fullmatch(r"cashguard_operator_[0-9a-f]{32}", database):
        raise ValueError("Invalid disposable fixture database identity.")
    url = postgres_url(source.get("DATABASE_URL", ""))
    runtime = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CI",
        "NODE_OPTIONS",
        "PLAYWRIGHT_BROWSERS_PATH",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "TEMP",
        "TMP",
    }
    env = {
        key: value
        for key, value in source.items()
        if key in runtime or key.startswith("INTEGRATION_")
    }
    # Explicit empties also prevent framework .env loaders filling these values.
    for key in (
        "T212_DEMO_API_KEY",
        "T212_DEMO_API_SECRET",
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
        "POLYGON_API_KEY",
        "BENZINGA_API_KEY",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "SENTRY_AUTH_TOKEN",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_WEBHOOK_SECRET",
    ):
        env[key] = ""
    env.update(
        DATABASE_URL=url.set(database=database).render_as_string(hide_password=False),
        CASHGUARD_ENV_FILE="",
        REDIS_URL=source.get("REDIS_URL", ""),
        CELERY_BROKER_URL="memory://",
        CELERY_RESULT_BACKEND="cache+memory://",
        API_URL="http://127.0.0.1:8001",
        APP_MODE="mock",
        MARKET_DATA_PROVIDER="mock",
        LIVE_TRADING_ENABLED="false",
        T212_DEMO_ORDER_ENABLED="false",
        T212_DEMO_MULTI_ORDER_ENABLED="false",
        DEMO_RECONCILIATION_WORKER_ENABLED="false",
        DEMO_RECONCILIATION_SCHEDULER_ENABLED="false",
        SECRET_KEY="integration-test-secret-key-32-chars-x",
        MASTER_KEY="integration-test-master-key-32-chars-x",
        ADMIN_EMAIL="admin@localhost",
        ADMIN_PASSWORD="change-me",
        E2E_ADMIN_EMAIL="admin@localhost",
        E2E_ADMIN_PASSWORD="change-me",
        SENTRY_DSN="",
        NEXT_PUBLIC_SENTRY_DSN="",
        CORS_ORIGINS="http://localhost:3001,http://127.0.0.1:3001",
        NEXT_PUBLIC_API_URL="http://127.0.0.1:8001",
        NEXT_PUBLIC_APP_MODE="mock",
        NEXT_TELEMETRY_DISABLED="1",
        BASE_URL="http://localhost:3001",
        INTEGRATION_API_PORT="8001",
        INTEGRATION_WEB_PORT="3001",
    )
    return env


@asynccontextmanager
async def disposable_database(url: URL, name: str) -> AsyncIterator[None]:
    if not re.fullmatch(r"cashguard_operator_[0-9a-f]{32}", name):
        raise ValueError("Invalid disposable fixture database identity.")
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            print(f"Attempting disposable operator database: {name}", flush=True)
            try:
                await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            except BaseException:
                print(
                    f"CREATE not acknowledged for {name}; no DROP attempted. Inspect this test identity manually.",
                    flush=True,
                )
                raise
            oid = await connection.scalar(
                text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": name}
            )
            if oid is None:
                raise RuntimeError(
                    "New fixture identity could not be verified; manual test cleanup required."
                )
            print(f"Disposable operator database created: {name}", flush=True)
            try:
                yield
            finally:
                actual = await connection.scalar(
                    text(
                        "SELECT oid FROM pg_database WHERE datname=:name AND pg_get_userbyid(datdba)=current_user"
                    ),
                    {"name": name},
                )
                if actual != oid:
                    raise RuntimeError("Fixture identity changed; refusing database cleanup.")
                # No FORCE, no termination of connections not owned by this process.
                await connection.execute(text(f'DROP DATABASE "{name}"'))
                print(f"Disposable operator database removed: {name}", flush=True)
    finally:
        await engine.dispose()


async def block_fixture(env: Mapping[str, str]) -> None:
    # Only called for the database just created by disposable_database, after seed.
    engine = create_async_engine(env["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE app_settings SET kill_switch_active=true"))
            await connection.execute(text("UPDATE venue_configs SET kill_switch_active=true"))
    finally:
        await engine.dispose()


def require_clean_web_env(root: Path) -> None:
    # Next loads its own dotenv files independently of the API's dotenv switch.
    for filename in (".env", ".env.local", ".env.development", ".env.development.local"):
        if (root / filename).exists():
            raise ValueError(
                "Operator E2E requires a clean web checkout without runtime dotenv files."
            )


def require_free_ports() -> None:
    for port in (8001, 3001):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))


def wait_for_ready(process: subprocess.Popen[bytes]) -> None:
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(20):
            if process.poll() is not None:
                raise RuntimeError("Integration API exited before readiness.")
            try:
                response = client.get("http://127.0.0.1:8001/v1/health/ready")
                if response.status_code == 200:
                    deps = client.get("http://127.0.0.1:8001/v1/health/deps")
                    expected = {"database": "ok", "redis": "ok", "market_data": "mock"}
                    if deps.status_code != 200 or any(
                        deps.json().get(k) != v for k, v in expected.items()
                    ):
                        raise RuntimeError("Integration dependencies are not ready in mock mode.")
                    return
                if response.status_code == 503:
                    raise RuntimeError("Integration API refused startup; inspect its startup log.")
            except httpx.TransportError:
                pass
            time.sleep(1)
    raise RuntimeError("Integration API did not become ready on loopback port 8001.")


def stop_owned(process: subprocess.Popen[bytes]) -> None:
    # Playwright handles SIGINT with plugin teardown (its Next server has a
    # separate group). Wait for that cleanup before using a forceful fallback.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    # A leader may already have exited while children remain in its owned group.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def run_owned(command: list[str], cwd: Path, env: Mapping[str, str]) -> None:
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    try:
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, command)
    finally:
        stop_owned(process)


async def main() -> None:
    require_free_ports()
    require_clean_web_env(WEB_ROOT)
    url = postgres_url(os.environ.get("DATABASE_URL", ""))
    name = "cashguard_operator_" + uuid.uuid4().hex
    env = fixture_environment(os.environ, name)
    # Redis is a required local test service; do not fall back to a developer .env.
    redis = make_url(env.get("REDIS_URL", ""))
    if (
        redis.drivername != "redis"
        or redis.host not in {"localhost", "127.0.0.1", "::1"}
        or redis.query
    ):
        raise ValueError("REDIS_URL must name an explicit loopback test Redis service.")
    async with disposable_database(url, name):
        for module, args in (("alembic", ["upgrade", "head"]), ("app.db.seed", [])):
            run_owned([sys.executable, "-m", module, *args], API_ROOT, env)
        await block_fixture(env)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--no-access-log",
            ],
            cwd=API_ROOT,
            env=env,
            start_new_session=True,
        )
        try:
            wait_for_ready(process)
            run_owned(
                [
                    "node",
                    str(WEB_ROOT / "node_modules" / "playwright" / "cli.js"),
                    "test",
                    "--config=playwright.integration.config.ts",
                ],
                WEB_ROOT,
                env,
            )
        finally:
            stop_owned(process)


if __name__ == "__main__":
    # SIGTERM, like Ctrl-C, must unwind the owned API/database cleanup.
    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    asyncio.run(main())
