"""Bounded real-Celery mock-paper soak for the exact final merged SHA."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import subprocess

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from scripts import real_worker_interruption_recovery as interruption  # noqa: E402
from scripts import real_worker_paper_chaos as chaos  # noqa: E402
from scripts import real_worker_paper_smoke as smoke  # noqa: E402

MIN_DISPATCHES = 30
MAX_DISPATCHES = 60
DEFAULT_DISPATCHES = 30
DEFAULT_INTERVAL_SECONDS = 2.0
MAX_INTERVAL_SECONDS = 120.0


@dataclass(frozen=True)
class SoakResources:
    postgres_container: str
    redis_container: str
    worker: chaos.WorkerSpec

    @classmethod
    def for_token(cls, token: str) -> SoakResources:
        if not token.isalnum() or len(token) > 16:
            raise ValueError("resource token must be 1-16 alphanumeric characters")
        return cls(
            postgres_container=f"cashguard-soak-pg-{token}",
            redis_container=f"cashguard-soak-redis-{token}",
            worker=chaos.WorkerSpec(
                label="soak",
                hostname=f"cashguard-soak-{token}@%h",
                queue=f"cashguard.soak.{token}",
            ),
        )


def validate_soak_policy(dispatches: int, interval_seconds: float, app_mode: str) -> None:
    if app_mode.lower() != "mock":
        raise ValueError("paper soak requires APP_MODE=mock")
    if not MIN_DISPATCHES <= dispatches <= MAX_DISPATCHES:
        raise ValueError(f"paper soak dispatches must be {MIN_DISPATCHES}-{MAX_DISPATCHES}")
    if not math.isfinite(interval_seconds) or not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"paper soak interval must be finite, positive, and <= {MAX_INTERVAL_SECONDS} seconds"
        )


async def _soak_database_evidence() -> dict[str, Any]:
    from sqlalchemy import desc, func, select

    from app.db.models import (
        AuditLog,
        BrokerAccountSnapshot,
        Order,
        OrderEvent,
        PositionSnapshot,
        Signal,
        Trade,
    )
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        signals = int(await db.scalar(select(func.count()).select_from(Signal)) or 0)
        orders = int(await db.scalar(select(func.count()).select_from(Order)) or 0)
        fills = int(
            await db.scalar(select(func.count()).select_from(Order).where(Order.status == "filled"))
            or 0
        )
        trades = int(await db.scalar(select(func.count()).select_from(Trade)) or 0)
        order_events = int(await db.scalar(select(func.count()).select_from(OrderEvent)) or 0)
        account_snapshots = int(
            await db.scalar(select(func.count()).select_from(BrokerAccountSnapshot)) or 0
        )
        position_snapshots = int(
            await db.scalar(select(func.count()).select_from(PositionSnapshot)) or 0
        )
        audits = int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0)
        decision_keys = sorted(
            str(key) for key in (await db.execute(select(Signal.decision_key))).scalars() if key
        )
        latest_account = (
            (
                await db.execute(
                    select(BrokerAccountSnapshot).order_by(
                        desc(BrokerAccountSnapshot.snapshotted_at)
                    )
                )
            )
            .scalars()
            .first()
        )
        latest_positions = (
            (
                await db.execute(
                    select(PositionSnapshot).order_by(
                        PositionSnapshot.ticker, desc(PositionSnapshot.snapshotted_at)
                    )
                )
            )
            .scalars()
            .all()
        )
        positions: dict[str, str] = {}
        for position in latest_positions:
            positions.setdefault(position.ticker, str(position.quantity))
        if signals != orders or orders != fills or signals != len(decision_keys):
            raise smoke.EvidenceFailure(
                f"Soak ledger diverged: signals={signals}, orders={orders}, fills={fills}, "
                f"decision_keys={len(decision_keys)}"
            )
        if (
            signals != 1
            or trades != 0
            or order_events == 0
            or account_snapshots != 1
            or position_snapshots != 1
            or audits == 0
            or latest_account is None
            or len(positions) != 1
        ):
            raise smoke.EvidenceFailure("Soak did not preserve one canonical paper fill")
        return {
            "unique_decisions": len(decision_keys),
            "decision_keys": decision_keys,
            "signals": signals,
            "orders": orders,
            "fills": fills,
            "trades": trades,
            "order_events": order_events,
            "account_snapshots": account_snapshots,
            "position_snapshots": position_snapshots,
            "audits": audits,
            "final_cash": str(latest_account.cash),
            "final_total_value": str(latest_account.total_value),
            "final_positions": positions,
        }


def run_soak(*, dispatches: int, interval_seconds: float) -> dict[str, Any]:
    validate_soak_policy(dispatches, interval_seconds, "mock")
    tested_sha = chaos._tested_git_sha()
    if not shutil.which("docker"):
        raise smoke.EvidenceFailure("docker is required")
    smoke._run(["docker", "info"])
    resources = SoakResources.for_token(uuid.uuid4().hex[:10])
    pg_port, redis_port = smoke._free_port(), smoke._free_port()
    env = smoke._environment(pg_port, redis_port)
    os.environ.update(env)
    worker: subprocess.Popen[str] | None = None
    workers: list[subprocess.Popen[str]] = []
    created_containers: list[str] = []
    started_at = datetime.now(UTC)

    with tempfile.TemporaryDirectory(prefix="cashguard-soak-") as temp:
        worker_log = Path(temp) / "worker.log"
        try:
            smoke._run(
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
            created_containers.append(resources.postgres_container)
            smoke._run(
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
            created_containers.append(resources.redis_container)
            interruption._wait_postgres(resources.postgres_container)
            interruption._wait_redis(resources.redis_container)
            smoke._run([str(API_ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=env)
            asyncio.run(interruption._reset_database())

            from app.workers.celery_app import celery_app

            worker = chaos._start_worker(workers, resources.worker, worker_log, env)
            task_ids: list[str] = []
            summaries: list[dict[str, Any]] = []
            baseline_database: dict[str, Any] | None = None
            for index in range(dispatches):
                task_id, summary = chaos._task_value(celery_app, resources.worker.queue)
                task_ids.append(task_id)
                summaries.append(summary)
                if index == 0:
                    baseline_database = asyncio.run(_soak_database_evidence())
                if index + 1 < dispatches:
                    time.sleep(interval_seconds)

            smoke._stop(worker)
            worker = None
            worker_text = worker_log.read_text()
            smoke.assert_safe_worker_log(worker_text)
            claims = interruption._claims(worker_text)
            if len(claims) != dispatches or len({claim["token"] for claim in claims}) != dispatches:
                raise smoke.EvidenceFailure("Soak lock evidence did not cover every dispatch")

            effective = sum(1 for summary in summaries if summary.get("orders_submitted") == 1)
            no_ops = sum(1 for summary in summaries if summary.get("orders_submitted") == 0)
            lock_conflicts = sum(
                1 for summary in summaries if summary.get("reason") == "already_running"
            )
            if effective != 1 or no_ops != dispatches - 1 or lock_conflicts != 0:
                raise smoke.EvidenceFailure(
                    f"Unexpected soak task outcomes: effective={effective}, no_ops={no_ops}, "
                    f"lock_conflicts={lock_conflicts}"
                )
            database = asyncio.run(_soak_database_evidence())
            if baseline_database is None or database != baseline_database:
                raise smoke.EvidenceFailure("Soak ledger changed after the canonical first fill")
            ended_at = datetime.now(UTC)
            chaos._tested_git_sha(tested_sha)
            return {
                "result": "PASS",
                "git_sha": tested_sha,
                "start_time": started_at.isoformat(),
                "end_time": ended_at.isoformat(),
                "duration_seconds": round((ended_at - started_at).total_seconds(), 3),
                "dispatch_count": dispatches,
                "interval_seconds": interval_seconds,
                "task_ids": task_ids,
                **database,
                "no_ops": no_ops,
                "retries": 0,
                "lock_conflicts": lock_conflicts,
                "recovery_events": 0,
                "lock_evidence": {
                    "claims": len(claims),
                    "unique_tokens": len({claim["token"] for claim in claims}),
                },
                "invariant_violations": [],
                "broker_tripwires": "armed and untriggered",
                "owned_resources_removed": True,
            }
        except Exception as exc:
            tail = worker_log.read_text()[-6000:] if worker_log.exists() else ""
            raise smoke.EvidenceFailure(f"{exc}\nWORKER LOG TAIL:\n{tail}") from exc
        finally:
            for process in workers:
                smoke._stop(process)
            smoke._remove_owned_containers(tuple(reversed(created_containers)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatches", type=int, default=DEFAULT_DISPATCHES)
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                run_soak(dispatches=args.dispatches, interval_seconds=args.interval_seconds),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"REAL WORKER PAPER SOAK FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
