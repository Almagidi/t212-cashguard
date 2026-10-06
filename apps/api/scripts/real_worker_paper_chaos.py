"""Disposable D3 proof for paper lifecycle, safety stops, and cleanup detection."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from scripts import real_worker_interruption_recovery as interruption  # noqa: E402
from scripts import real_worker_lock_recovery as recovery  # noqa: E402
from scripts import real_worker_paper_smoke as smoke  # noqa: E402

TASK_NAME = smoke.TASK_NAME
OWNER_MARKER = recovery.OWNER_MARKER
SCENARIO_TIMEOUT = 90


def _tested_git_sha(expected: str | None = None) -> str:
    """Return the exact clean harness-scoped revision, or fail closed."""
    status = smoke._run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ":(top)Makefile",
            ":(top)apps/api",
        ]
    ).strip()
    if status:
        raise smoke.EvidenceFailure(
            "Harness-scoped files are dirty; exact-SHA evidence requires a committed tree"
        )
    actual = smoke._run(["git", "rev-parse", "HEAD"]).strip()
    if expected is not None and actual != expected:
        raise smoke.EvidenceFailure(
            f"Tested Git revision changed during the harness: {expected} -> {actual}"
        )
    return actual


@dataclass(frozen=True)
class WorkerSpec:
    label: str
    hostname: str
    queue: str
    bar_offset_minutes: int | None = None


@dataclass(frozen=True)
class D3Resources:
    postgres_container: str
    redis_container: str
    cleanup_container: str
    workers: tuple[WorkerSpec, ...]

    @classmethod
    def for_token(cls, token: str) -> D3Resources:
        if not token.isalnum() or len(token) > 16:
            raise ValueError("resource token must be 1-16 alphanumeric characters")

        def worker(label: str, offset: int | None = None) -> WorkerSpec:
            return WorkerSpec(
                label=label,
                hostname=f"cashguard-d3-{label}-{token}@%h",
                queue=f"cashguard.d3.{label}.{token}",
                bar_offset_minutes=offset,
            )

        return cls(
            postgres_container=f"cashguard-d3-pg-{token}",
            redis_container=f"cashguard-d3-redis-{token}",
            cleanup_container=f"cashguard-d3-cleanup-{token}",
            workers=(worker("same-bar", -1440), worker("later-bar"), worker("kill-switch")),
        )


def _launch_worker(
    spec: WorkerSpec,
    log_path: Path,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    args = [
        sys.executable,
        str(smoke.WORKER_LAUNCHER),
        "--hostname",
        spec.hostname,
        "--queue",
        spec.queue,
        "--worker-id",
        spec.label,
        "--lock-hold-seconds",
        "0.05",
    ]
    if spec.bar_offset_minutes is not None:
        args.extend(("--mock-bar-offset-minutes", str(spec.bar_offset_minutes)))
    handle = log_path.open("w")
    process = subprocess.Popen(
        args,
        cwd=API_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    handle.close()
    return process


def _start_worker(
    workers: list[subprocess.Popen[str]],
    spec: WorkerSpec,
    log_path: Path,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    process = _launch_worker(spec, log_path, env)
    workers.append(process)
    smoke._wait(
        f"D3 worker {spec.label}",
        smoke.WORKER_READY_TIMEOUT,
        lambda: interruption._worker_ready(log_path),
    )
    return process


def _task_value(celery_app: Any, queue: str) -> tuple[str, dict[str, Any]]:
    result = celery_app.send_task(TASK_NAME, queue=queue)
    task_id = result.id
    return task_id, interruption._result_value(result)


async def _decision_counts() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.db.models import Order, Signal
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        decision_keys = list((await db.execute(select(Signal.decision_key))).scalars())
        return {
            "signals": int(await db.scalar(select(func.count()).select_from(Signal)) or 0),
            "orders": int(await db.scalar(select(func.count()).select_from(Order)) or 0),
            "decision_keys": sorted(str(key) for key in decision_keys if key),
        }


async def _make_decision_probe_dry_run() -> None:
    """Isolate decision-key eligibility from the open-position entry gate."""
    from sqlalchemy import select

    from app.db.models import Strategy
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        strategy = (
            await db.execute(select(Strategy).where(Strategy.name == "Real Worker ORB Smoke"))
        ).scalar_one()
        strategy.is_live = False
        await db.commit()


async def _partial_fill_and_cancel() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.api.schemas import PaperOrderCreate
    from app.core.config import settings
    from app.db.models import (
        AuditLog,
        BrokerAccountSnapshot,
        OrderEvent,
        PositionSnapshot,
        Trade,
        User,
    )
    from app.db.session import AsyncSessionLocal
    from app.execution.paper_engine import PaperExecutionEngine, PaperExecutionError
    from app.services.system_control import SystemControlService

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
        ).scalar_one()
        engine = PaperExecutionEngine(db)
        order = await engine.execute(
            PaperOrderCreate(
                ticker="D3PARTIAL",
                side="buy",
                quantity=Decimal("4"),
                estimated_price=Decimal("100"),
                strategy="D3 partial lifecycle",
                source="real_worker_paper_chaos",
                venue="paper",
                paper_only=True,
                simulation_profile="partial_fill",
            ),
            user=user,
        )
        order = await engine.fill_partial_order(
            order,
            quantity=Decimal("1"),
            estimated_price=Decimal("100"),
            user=user,
        )
        overfill_blocked = False
        try:
            await engine.fill_partial_order(
                order,
                quantity=Decimal("2"),
                estimated_price=Decimal("100"),
                user=user,
            )
        except PaperExecutionError as exc:
            overfill_blocked = "remaining quantity" in str(exc)
        if not overfill_blocked:
            raise smoke.EvidenceFailure("Partial paper overfill was not explicitly blocked")

        service = SystemControlService(db)

        async def forbidden_broker(_purpose: str) -> Any:
            raise AssertionError("D3 paper cancellation constructed a broker")

        service._get_broker = forbidden_broker  # type: ignore[method-assign]
        cancellation = await service.cancel_all_pending_summary(actor="d3-chaos")
        await db.commit()
        await db.refresh(order)

        positions = (
            (await db.execute(select(PositionSnapshot).order_by(PositionSnapshot.snapshotted_at)))
            .scalars()
            .all()
        )
        accounts = (
            (
                await db.execute(
                    select(BrokerAccountSnapshot).order_by(BrokerAccountSnapshot.snapshotted_at)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await db.execute(
                    select(OrderEvent)
                    .where(OrderEvent.order_id == order.id)
                    .order_by(OrderEvent.occurred_at, OrderEvent.id)
                )
            )
            .scalars()
            .all()
        )
        audit_actions = list((await db.execute(select(AuditLog.action))).scalars())
        trade_count = int(await db.scalar(select(func.count()).select_from(Trade)) or 0)
        if not (
            cancellation.cancelled == 1
            and cancellation.failed == 0
            and order.status == "cancelled"
            and order.filled_quantity == Decimal("3")
            and order.remaining_quantity == Decimal("1")
            and order.fee_amount == Decimal("0.06006000")
            and [row.quantity for row in positions] == [Decimal("2"), Decimal("3")]
            and accounts[-1].cash == Decimal("99699.63994000")
            and trade_count == 0
        ):
            raise smoke.EvidenceFailure("Partial fill/cancellation accounting diverged")
        cancellation_event = next(event for event in events if event.event_type == "cancelled")
        if Decimal(cancellation_event.payload["cancelled_quantity"]) != Decimal("1"):
            raise smoke.EvidenceFailure("Cancellation evidence did not preserve the remainder")
        return {
            "order_id": str(order.id),
            "status": order.status,
            "requested_quantity": str(order.quantity),
            "cumulative_filled_quantity": str(order.filled_quantity),
            "remaining_quantity": str(order.remaining_quantity),
            "fee_amount": str(order.fee_amount),
            "cash": str(accounts[-1].cash),
            "position_quantity": str(positions[-1].quantity),
            "trade_count": trade_count,
            "overfill_blocked": overfill_blocked,
            "broker_calls": 0,
            "event_types": [event.event_type for event in events],
            "audit_actions": sorted(set(audit_actions)),
        }


async def _activate_and_evidence_kill_switch() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.db.models import AppSettings, AuditLog, Order, RiskEvent, Signal
    from app.db.session import AsyncSessionLocal
    from app.services.system_control import SystemControlService

    async with AsyncSessionLocal() as db:
        await SystemControlService(db).activate_kill_switch(actor="d3-chaos")
        await db.commit()
        settings_row = await db.get(AppSettings, 1)
        if settings_row is None:
            raise smoke.EvidenceFailure("Kill switch settings row disappeared")
        return {
            "kill_switch_active": settings_row.kill_switch_active,
            "auto_trading_enabled": settings_row.auto_trading_enabled,
            "signals": int(await db.scalar(select(func.count()).select_from(Signal)) or 0),
            "orders": int(await db.scalar(select(func.count()).select_from(Order)) or 0),
            "risk_events": int(
                await db.scalar(
                    select(func.count())
                    .select_from(RiskEvent)
                    .where(RiskEvent.event_type == "kill_switch_on")
                )
                or 0
            ),
            "audits": int(
                await db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "emergency_kill_switch")
                )
                or 0
            ),
        }


def _prove_cleanup_failure(container: str) -> dict[str, Any]:
    real_run = smoke.subprocess.run

    def fail_owned_remove(args: list[str], **kwargs: Any) -> Any:
        if args[:3] == ["docker", "rm", "-f"] and args[3:] == [container]:
            return SimpleNamespace(returncode=1, stdout="", stderr="injected cleanup failure")
        return real_run(args, **kwargs)

    failure = ""
    with patch.object(smoke.subprocess, "run", side_effect=fail_owned_remove):
        try:
            smoke._remove_owned_containers((container,))
        except smoke.EvidenceFailure as exc:
            failure = str(exc)
    if container not in failure or container not in smoke._listed_owned_containers((container,)):
        raise smoke.EvidenceFailure("Injected owned cleanup failure produced a false green")
    smoke._remove_owned_containers((container,))
    if smoke._listed_owned_containers((container,)):
        raise smoke.EvidenceFailure("Owned cleanup sentinel remained after targeted restoration")
    return {
        "failure_reported": True,
        "failed_container": container,
        "restored_by_exact_name": True,
    }


def run_chaos() -> dict[str, Any]:
    tested_sha = _tested_git_sha()
    if not shutil.which("docker"):
        raise smoke.EvidenceFailure("docker is required")
    smoke._run(["docker", "info"])
    resources = D3Resources.for_token(uuid.uuid4().hex[:10])
    pg_port, redis_port = smoke._free_port(), smoke._free_port()
    env = smoke._environment(pg_port, redis_port)
    os.environ.update(env)
    workers: list[subprocess.Popen[str]] = []
    created_containers: list[str] = []

    with tempfile.TemporaryDirectory(prefix="cashguard-d3-") as temp:
        logs = {spec.label: Path(temp) / f"{spec.label}.log" for spec in resources.workers}
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

            from app.workers.celery_app import celery_app

            # Same bar suppresses; a harness-shifted later bar creates one new key.
            # Keep this probe dry-run so an open paper position cannot mask the
            # decision-key result behind the production position-entry gate.
            asyncio.run(interruption._reset_database())
            asyncio.run(_make_decision_probe_dry_run())
            same_spec, later_spec, kill_spec = resources.workers
            same_worker = _start_worker(workers, same_spec, logs[same_spec.label], env)
            first_id, first = _task_value(celery_app, same_spec.queue)
            duplicate_id, duplicate = _task_value(celery_app, same_spec.queue)
            smoke._stop(same_worker)
            later_worker = _start_worker(workers, later_spec, logs[later_spec.label], env)
            later_id, later = _task_value(celery_app, later_spec.queue)
            later_duplicate_id, later_duplicate = _task_value(celery_app, later_spec.queue)
            smoke._stop(later_worker)
            decisions = asyncio.run(_decision_counts())
            if not (
                first.get("signals_generated") == 1
                and first.get("orders_submitted") == 0
                and duplicate.get("signals_generated") == 0
                and later.get("signals_generated") == 1
                and later.get("orders_submitted") == 0
                and later_duplicate.get("signals_generated") == 0
                and decisions["signals"] == 2
                and decisions["orders"] == 0
                and len(decisions["decision_keys"]) == 2
            ):
                raise smoke.EvidenceFailure("Same/new decision eligibility diverged")

            # Partial lifecycle uses real PostgreSQL and no broker construction.
            asyncio.run(interruption._reset_database())
            partial = asyncio.run(_partial_fill_and_cancel())

            # Existing kill-switch mechanism blocks the next real task and persists.
            asyncio.run(interruption._reset_database())
            kill_worker = _start_worker(workers, kill_spec, logs[kill_spec.label], env)
            baseline_id, baseline = _task_value(celery_app, kill_spec.queue)
            activated = asyncio.run(_activate_and_evidence_kill_switch())
            blocked_id, blocked = _task_value(celery_app, kill_spec.queue)
            smoke._stop(kill_worker)
            blocked_counts = asyncio.run(_decision_counts())
            if not (
                baseline.get("orders_submitted") == 1
                and blocked.get("skipped") == "kill_switch"
                and activated["kill_switch_active"] is True
                and activated["auto_trading_enabled"] is False
                and activated["risk_events"] == activated["audits"] == 1
                and blocked_counts["signals"] == blocked_counts["orders"] == 1
            ):
                raise smoke.EvidenceFailure("Kill-switch task block or persistence diverged")

            for log_path in logs.values():
                smoke.assert_safe_worker_log(log_path.read_text())
            claims = [
                claim
                for log_path in logs.values()
                for claim in interruption._claims(log_path.read_text())
            ]
            claims_by_worker = Counter(str(claim["worker"]) for claim in claims)
            if (
                len(claims) != 6
                or len({claim["token"] for claim in claims}) != 6
                or claims_by_worker != {"same-bar": 2, "later-bar": 2, "kill-switch": 2}
            ):
                raise smoke.EvidenceFailure("D3 lock-owner evidence was incomplete")

            smoke._run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    resources.cleanup_container,
                    "redis:7-alpine",
                ]
            )
            created_containers.append(resources.cleanup_container)
            cleanup_proof = _prove_cleanup_failure(resources.cleanup_container)
            _tested_git_sha(tested_sha)

            return {
                "result": "PASS",
                "git_sha": tested_sha,
                "new_decision_after_duplicate": {
                    "task_ids": [first_id, duplicate_id, later_id, later_duplicate_id],
                    "first": first,
                    "duplicate": duplicate,
                    "later_bar": later,
                    "later_bar_duplicate": later_duplicate,
                    **decisions,
                },
                "partial_fill_cancellation": partial,
                "kill_switch": {
                    "task_ids": [baseline_id, blocked_id],
                    "baseline": baseline,
                    "blocked": blocked,
                    "operator_status": activated,
                    "final_counts": blocked_counts,
                },
                "lock_evidence": {
                    "claims": len(claims),
                    "unique_tokens": len({claim["token"] for claim in claims}),
                    "claims_by_worker": dict(sorted(claims_by_worker.items())),
                },
                "cleanup_failure": cleanup_proof,
                "broker_tripwires": "armed and untriggered",
                "owned_resources_removed": True,
            }
        except Exception as exc:
            tails = "\n".join(
                f"{label}:\n{path.read_text()[-5000:]}"
                for label, path in logs.items()
                if path.exists()
            )
            raise smoke.EvidenceFailure(f"{exc}\nWORKER LOG TAILS:\n{tails}") from exc
        finally:
            for worker in workers:
                smoke._stop(worker)
            smoke._remove_owned_containers(tuple(reversed(created_containers)))


def main() -> int:
    try:
        print(json.dumps(run_chaos(), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"REAL WORKER PAPER CHAOS FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
