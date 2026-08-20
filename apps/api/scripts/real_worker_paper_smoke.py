"""Disposable PostgreSQL/Redis real-Celery proof for scheduled paper execution."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTAINER_READY_TIMEOUT = 60
WORKER_READY_TIMEOUT = 60
TASK_RESULT_TIMEOUT = 180
TASK_NAME = "app.workers.tasks.run_strategy_signals"
API_ROOT = Path(__file__).resolve().parents[1]
WORKER_LAUNCHER = API_ROOT / "scripts" / "real_worker_tripwire_worker.py"
sys.path.insert(0, str(API_ROOT))
FORBIDDEN_LOG_MARKERS = (
    "FORBIDDEN_CONSTRUCTION:",
    "FORBIDDEN_NETWORK:",
    "https://live.trading212.com",
    "https://demo.trading212.com",
    "https://api.kraken.com",
    "redis.task_lock_unavailable",
)
DISABLED_EXTERNAL_INTEGRATIONS = (
    "SENTRY_DSN",
    "SENTRY_AUTH_TOKEN",
    "SMTP_HOST",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "ALERT_EMAIL_FROM",
    "ALERT_EMAIL_TO",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_WEBHOOK_SECRET",
    "DISCORD_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
)


class EvidenceFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnedResources:
    postgres_container: str
    redis_container: str
    worker_hostname: str

    @classmethod
    def for_token(cls, token: str) -> OwnedResources:
        if not token.isalnum() or len(token) > 16:
            raise ValueError("resource token must be 1-16 alphanumeric characters")
        return cls(
            f"cashguard-worker-pg-{token}",
            f"cashguard-worker-redis-{token}",
            f"cashguard-worker-{token}@%h",
        )


def assert_safe_worker_log(log_text: str) -> None:
    for marker in FORBIDDEN_LOG_MARKERS:
        if marker in log_text:
            raise EvidenceFailure(f"Forbidden worker evidence found: {marker}")
    if "CASHGUARD_BROKER_TRIPWIRES_ARMED" not in log_text:
        raise EvidenceFailure("Worker broker tripwires were not armed.")
    if "CASHGUARD_NETWORK_TRIPWIRE_ARMED" not in log_text:
        raise EvidenceFailure("Worker network tripwire was not armed.")


def assert_redis_lock_evidence(monitor_text: str, lock_key: str) -> None:
    lines = monitor_text.lower().splitlines()
    quoted_key = f'"{lock_key.lower()}"'
    acquired = any(
        '"set"' in line and quoted_key in line and '"nx"' in line and '"ex"' in line
        for line in lines
    )
    released = any('"del"' in line and quoted_key in line for line in lines)
    if not acquired or not released:
        raise EvidenceFailure("Redis MONITOR did not prove SET NX EX/DEL task-lock activity")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args,
        cwd=API_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout


def _wait(label: str, timeout: int, probe: Any) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.25)
    raise EvidenceFailure(f"Timed out waiting for {label} after {timeout}s")


def _environment(pg_port: int, redis_port: int) -> dict[str, str]:
    env = dict(os.environ)
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    env.update(
        APP_MODE="mock",
        PYTHONPATH=str(API_ROOT),
        DATABASE_URL=(
            f"postgresql+asyncpg://cashguard:cashguard_smoke@127.0.0.1:{pg_port}/cashguard_worker"
        ),
        REDIS_URL=redis_url,
        CELERY_BROKER_URL=redis_url,
        CELERY_RESULT_BACKEND=redis_url,
        MARKET_DATA_PROVIDER="mock",
        MOCK_MARKET_PROFILE="orb_breakout",
        MOCK_MARKET_SEED="212",
        ADMIN_EMAIL="worker-smoke@localhost",
        SECRET_KEY="worker-smoke-secret-key-32-characters-x",
        MASTER_KEY="worker-smoke-master-key-32-characters-x",
    )
    for key in (
        "T212_API_KEY",
        "T212_API_SECRET",
        "T212_DEMO_API_KEY",
        "T212_DEMO_API_SECRET",
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "POLYGON_API_KEY",
        *DISABLED_EXTERNAL_INTEGRATIONS,
    ):
        env[key] = ""
    return env


async def _seed_database() -> None:
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import AppSettings, Strategy, User, VenueConfig
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        app_settings = await db.get(AppSettings, 1)
        if app_settings is None:
            app_settings = AppSettings(id=1)
            db.add(app_settings)
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
        venue = await db.get(VenueConfig, "t212")
        if venue is None:
            venue = VenueConfig(venue="t212")
            db.add(venue)
        venue.kill_switch_active = False
        venue.auto_trading_enabled = True
        venue.degraded_mode_active = False
        db.add(
            User(
                id=uuid.uuid4(),
                email=settings.ADMIN_EMAIL,
                hashed_password="worker-smoke-not-a-login-credential",
                is_active=True,
                is_admin=True,
            )
        )
        existing_strategy = (
            await db.execute(select(Strategy).where(Strategy.name == "Real Worker ORB Smoke"))
        ).scalar_one_or_none()
        if existing_strategy is None:
            db.add(
                Strategy(
                    id=uuid.uuid4(),
                    name="Real Worker ORB Smoke",
                    type="orb",
                    is_enabled=True,
                    is_live=True,
                    params={},
                    allowed_tickers=["NVDA"],
                    venue="t212",
                )
            )
        await db.commit()


async def _database_evidence() -> dict[str, Any]:
    from sqlalchemy import select

    from app.db.models import (
        AuditLog,
        BrokerAccountSnapshot,
        Order,
        OrderEvent,
        PositionSnapshot,
        Signal,
    )
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        orders = (
            (await db.execute(select(Order).where(Order.execution_environment == "paper_mock")))
            .scalars()
            .all()
        )
        if len(orders) != 1:
            raise EvidenceFailure(f"Expected exactly one paper fill, found {len(orders)}")
        order = orders[0]
        signals = (await db.execute(select(Signal))).scalars().all()
        actions = set((await db.execute(select(AuditLog.action))).scalars().all())
        events = set(
            (
                await db.execute(
                    select(OrderEvent.event_type).where(OrderEvent.order_id == order.id)
                )
            ).scalars()
        )
        position_count = len((await db.execute(select(PositionSnapshot.id))).scalars().all())
        account_count = len((await db.execute(select(BrokerAccountSnapshot.id))).scalars().all())
        checks = {
            "one_signal": len(signals) == 1,
            "signal_linked": bool(signals and order.signal_id == signals[0].id),
            "filled": order.status == "filled" and order.filled_quantity == order.quantity,
            "nonzero_costs": bool(
                order.avg_fill_price != order.expected_fill_price
                and order.slippage_value
                and order.slippage_value > 0
                and order.fee_amount
                and order.fee_amount > 0
            ),
            "no_broker_order": bool(
                not order.broker_order_id
                and order.broker_response
                and order.broker_response.get("no_broker_order_sent") is True
            ),
            "event_and_audits": (
                "paper_fill_simulated" in events
                and {"paper_fill_simulated", "strategy_order_placed"} <= actions
            ),
            "paper_effects": position_count == account_count == 1,
        }
        failed = [key for key, passed in checks.items() if not passed]
        if failed:
            raise EvidenceFailure(f"Database evidence failed: {', '.join(failed)}")
        return {
            "order_id": str(order.id),
            "signal_id": str(order.signal_id),
            "expected_fill_price": str(order.expected_fill_price),
            "avg_fill_price": str(order.avg_fill_price),
            "slippage_value": str(order.slippage_value),
            "fee_amount": str(order.fee_amount),
            "checks": checks,
        }


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _listed_owned_containers(names: tuple[str, ...]) -> set[str]:
    listed = set(
        _run(["docker", "container", "ls", "-a", "--format", "{{.Names}}"]).strip().splitlines()
    )
    return listed.intersection(names)


def _remove_owned_containers(names: tuple[str, ...]) -> None:
    for name in names:
        removal = subprocess.run(
            ["docker", "rm", "-f", name],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if removal.returncode != 0 and name in _listed_owned_containers(names):
            raise EvidenceFailure(
                f"Failed to remove owned container {name}: {removal.stderr.strip()}"
            )
    remaining = _listed_owned_containers(names)
    if remaining:
        raise EvidenceFailure(
            f"Owned containers remain after cleanup: {', '.join(sorted(remaining))}"
        )


def run_smoke() -> dict[str, Any]:
    if not shutil.which("docker"):
        raise EvidenceFailure("docker is required")
    _run(["docker", "info"])
    resources = OwnedResources.for_token(uuid.uuid4().hex[:10])
    pg_port, redis_port = _free_port(), _free_port()
    env = _environment(pg_port, redis_port)
    os.environ.update(env)
    worker: subprocess.Popen[str] | None = None
    monitor: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="cashguard-real-worker-") as temp:
        worker_path, monitor_path = Path(temp) / "worker.log", Path(temp) / "redis.log"
        try:
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    resources.postgres_container,
                    "-e",
                    "POSTGRES_USER=cashguard",
                    "-e",
                    "POSTGRES_PASSWORD=cashguard_smoke",
                    "-e",
                    "POSTGRES_DB=cashguard_worker",
                    "-p",
                    f"127.0.0.1:{pg_port}:5432",
                    "postgres:16-alpine",
                ]
            )
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    resources.redis_container,
                    "-p",
                    f"127.0.0.1:{redis_port}:6379",
                    "redis:7-alpine",
                ]
            )
            _wait(
                "PostgreSQL",
                CONTAINER_READY_TIMEOUT,
                lambda: (
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            resources.postgres_container,
                            "pg_isready",
                            "-U",
                            "cashguard",
                        ],
                        capture_output=True,
                    ).returncode
                    == 0
                ),
            )
            _wait(
                "Redis",
                CONTAINER_READY_TIMEOUT,
                lambda: (
                    subprocess.run(
                        ["docker", "exec", resources.redis_container, "redis-cli", "ping"],
                        capture_output=True,
                    ).returncode
                    == 0
                ),
            )
            _run([str(API_ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=env)
            asyncio.run(_seed_database())
            with worker_path.open("w") as worker_log, monitor_path.open("w") as monitor_log:
                worker = subprocess.Popen(
                    [sys.executable, str(WORKER_LAUNCHER), "--hostname", resources.worker_hostname],
                    cwd=API_ROOT,
                    env=env,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                monitor = subprocess.Popen(
                    ["docker", "exec", resources.redis_container, "redis-cli", "MONITOR"],
                    stdout=monitor_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                from app.workers.celery_app import celery_app

                _wait(
                    "Celery worker",
                    WORKER_READY_TIMEOUT,
                    lambda: bool(celery_app.control.ping(timeout=1)),
                )
                first = celery_app.send_task(TASK_NAME).get(
                    timeout=TASK_RESULT_TIMEOUT, interval=0.25
                )
                second = celery_app.send_task(TASK_NAME).get(
                    timeout=TASK_RESULT_TIMEOUT, interval=0.25
                )
                if first.get("orders_submitted") != 1 or second.get("orders_submitted") != 0:
                    raise EvidenceFailure(
                        f"Unexpected task summaries: first={first}, second={second}"
                    )
                evidence = asyncio.run(_database_evidence())
                _stop(monitor)
                monitor = None
                worker_log.flush()
                monitor_log.flush()
            worker_text, monitor_text = worker_path.read_text(), monitor_path.read_text().lower()
            assert_safe_worker_log(worker_text)
            lock_key = "celery:task_lock:run_strategy_signals"
            assert_redis_lock_evidence(monitor_text, lock_key)
            return {
                "result": "PASS",
                "first_task": first,
                "duplicate_task": second,
                "database": evidence,
                "redis_lock": "SET/DEL observed",
                "broker_tripwires": "armed and untriggered",
                "owned_resources_removed": True,
            }
        except Exception as exc:
            worker_tail = worker_path.read_text()[-6000:] if worker_path.exists() else ""
            raise EvidenceFailure(f"{exc}\nWORKER LOG TAIL:\n{worker_tail}") from exc
        finally:
            _stop(monitor)
            _stop(worker)
            _remove_owned_containers((resources.redis_container, resources.postgres_container))


def main() -> int:
    try:
        print(json.dumps(run_smoke(), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"REAL WORKER PAPER SMOKE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
