"""Disposable real-worker proof for D2 infrastructure interruption recovery."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
smoke = importlib.import_module("scripts.real_worker_paper_smoke")
recovery = importlib.import_module("scripts.real_worker_lock_recovery")

TASK_NAME = smoke.TASK_NAME
LOCK_KEY = recovery.LOCK_KEY
OWNER_MARKER = recovery.OWNER_MARKER
PRECOMMIT_MARKER = "CASHGUARD_HARNESS_PRECOMMIT_FAULT "
FAULT_HOLD_SECONDS = 4.0
INTERRUPTION_LOCK_TTL_SECONDS = 10
SCENARIO_TIMEOUT = 90


@dataclass(frozen=True)
class WorkerSpec:
    label: str
    hostname: str
    queue: str


@dataclass(frozen=True)
class D2Resources:
    postgres_container: str
    redis_container: str
    workers: tuple[WorkerSpec, ...]

    @classmethod
    def for_token(cls, token: str) -> D2Resources:
        if not token.isalnum() or len(token) > 16:
            raise ValueError("resource token must be 1-16 alphanumeric characters")
        labels = (
            "redis-before",
            "redis-during",
            "pg-before",
            "fault",
            "recovery",
            "death",
            "death-replacement",
        )
        workers = tuple(
            WorkerSpec(
                label=label,
                hostname=f"cashguard-d2-{label}-{token}@%h",
                queue=f"cashguard-d2-{label}-{token}",
            )
            for label in labels
        )
        return cls(
            postgres_container=f"cashguard-d2-pg-{token}",
            redis_container=f"cashguard-d2-redis-{token}",
            workers=workers,
        )

    def worker(self, label: str) -> WorkerSpec:
        return next(worker for worker in self.workers if worker.label == label)


def _claims(log_text: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        if OWNER_MARKER not in line:
            continue
        try:
            claim = json.loads(line.split(OWNER_MARKER, 1)[1])
        except json.JSONDecodeError as exc:
            raise smoke.EvidenceFailure("Malformed D2 lock-owner evidence") from exc
        if not all(isinstance(claim.get(key), str) for key in ("task", "token", "worker")):
            raise smoke.EvidenceFailure("Incomplete D2 lock-owner evidence")
        if not isinstance(claim.get("pid"), int) or claim["pid"] <= 1:
            raise smoke.EvidenceFailure("D2 owner evidence lacks a safe child PID")
        claims.append(claim)
    return claims


def _worker_ready(path: Path) -> bool:
    return (
        path.exists()
        and "CASHGUARD_NETWORK_TRIPWIRE_ARMED" in path.read_text()
        and " ready." in path.read_text()
    )


def _launch_worker(
    spec: WorkerSpec,
    path: Path,
    env: dict[str, str],
    *,
    hold: bool = False,
    pool: str = "solo",
    precommit_fault: bool = False,
    ttl_seconds: int | None = None,
    consume_queue: str | None = None,
) -> subprocess.Popen[str]:
    args = [
        sys.executable,
        str(smoke.WORKER_LAUNCHER),
        "--hostname",
        spec.hostname,
        "--queue",
        consume_queue or spec.queue,
        "--pool",
        pool,
    ]
    if hold:
        args.extend(
            [
                "--worker-id",
                spec.label,
                "--lock-hold-seconds",
                str(FAULT_HOLD_SECONDS),
            ]
        )
        if ttl_seconds is not None:
            args.extend(["--lock-ttl-seconds", str(ttl_seconds)])
    if precommit_fault:
        args.append("--precommit-fault-once")
    handle = path.open("w")
    try:
        return subprocess.Popen(
            args,
            cwd=API_ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        handle.close()


def _container(container: str, action: str) -> None:
    smoke._run(["docker", action, container])


def _wait_redis(container: str) -> None:
    smoke._wait(
        "Redis restart",
        smoke.CONTAINER_READY_TIMEOUT,
        lambda: (
            subprocess.run(
                ["docker", "exec", container, "redis-cli", "ping"], capture_output=True
            ).returncode
            == 0
        ),
    )


def _wait_postgres(container: str) -> None:
    smoke._wait(
        "PostgreSQL restart",
        smoke.CONTAINER_READY_TIMEOUT,
        lambda: (
            subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "cashguard"],
                capture_output=True,
            ).returncode
            == 0
        ),
    )


async def _reset_database() -> None:
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public' AND tablename <> 'alembic_version'"
                    )
                )
            )
            .scalars()
            .all()
        )
        if rows:
            quoted = ", ".join(f'"{name}"' for name in rows)
            await db.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        await db.commit()
    await smoke._seed_database()


async def _execution_counts() -> dict[str, int]:
    from sqlalchemy import func, select

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

    models = (
        Signal,
        Order,
        OrderEvent,
        BrokerAccountSnapshot,
        PositionSnapshot,
        Trade,
        AuditLog,
    )
    async with AsyncSessionLocal() as db:
        counts = {
            model.__tablename__: int(
                (await db.execute(select(func.count()).select_from(model))).scalar_one()
            )
            for model in models
        }
        return counts


def _assert_zero_execution_counts(counts: dict[str, int]) -> None:
    nonzero = {name: count for name, count in counts.items() if count != 0}
    if nonzero:
        raise smoke.EvidenceFailure(f"Partial execution effects survived failure: {nonzero}")


def _release_result(result: Any) -> None:
    """Release backend state while the harness-owned Redis is still available."""
    backend = result.backend
    result.forget()
    backend.remove_pending_result(result)
    result.backend = None


def _result_failure(result: Any) -> dict[str, str]:
    try:
        result.get(timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25)
    except Exception as exc:
        failure = {
            "state": str(result.state),
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }
        _release_result(result)
        return failure
    _release_result(result)
    raise smoke.EvidenceFailure("Expected a visible task failure, but the task succeeded")


def _result_value(result: Any) -> dict[str, Any]:
    try:
        return cast(
            "dict[str, Any]",
            result.get(timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25),
        )
    finally:
        _release_result(result)


def _assert_effective(result: dict[str, Any], label: str) -> None:
    if result.get("orders_submitted") != 1:
        raise smoke.EvidenceFailure(f"{label} did not produce one effective order: {result}")


def _assert_duplicate_noop(result: dict[str, Any], label: str) -> None:
    if result.get("orders_submitted") != 0:
        raise smoke.EvidenceFailure(f"{label} duplicated an order: {result}")


def _flush_redis(container: str) -> None:
    smoke._run(["docker", "exec", container, "redis-cli", "FLUSHALL"])


def _restore_unacked_delivery(redis_url: str, task_id: str) -> str:
    """Invoke Kombu's real Redis restore primitive for one exact delivery."""
    from kombu import Connection
    from kombu.utils.json import loads

    with Connection(redis_url) as connection:
        channel = connection.channel()
        try:
            matches: list[bytes | str] = []
            for delivery_tag, encoded in channel.client.hgetall(channel.qos.unacked_key).items():
                message, _exchange, _routing_key = loads(encoded)
                if message.get("headers", {}).get("id") == task_id:
                    matches.append(delivery_tag)
            if len(matches) != 1:
                raise smoke.EvidenceFailure(
                    f"Expected one unacked delivery for {task_id}, found {len(matches)}"
                )
            delivery_tag = matches[0]
            channel.qos.restore_by_tag(delivery_tag)
            return delivery_tag.decode() if isinstance(delivery_tag, bytes) else str(delivery_tag)
        finally:
            channel.close()


def _start_and_wait(
    workers: list[subprocess.Popen[str]],
    spec: WorkerSpec,
    path: Path,
    env: dict[str, str],
    **kwargs: Any,
) -> subprocess.Popen[str]:
    worker = _launch_worker(spec, path, env, **kwargs)
    workers.append(worker)
    smoke._wait(spec.label, smoke.WORKER_READY_TIMEOUT, lambda: _worker_ready(path))
    return worker


def _stop_worker(worker: subprocess.Popen[str]) -> None:
    smoke._stop(worker)


def run_recovery() -> dict[str, Any]:
    if not shutil.which("docker"):
        raise smoke.EvidenceFailure("docker is required")
    smoke._run(["docker", "info"])
    resources = D2Resources.for_token(uuid.uuid4().hex[:10])
    pg_port, redis_port = smoke._free_port(), smoke._free_port()
    env = smoke._environment(pg_port, redis_port)
    os.environ.update(env)
    workers: list[subprocess.Popen[str]] = []
    created_containers: list[str] = []

    with tempfile.TemporaryDirectory(prefix="cashguard-d2-") as temp:
        temp_path = Path(temp)
        logs = {worker.label: temp_path / f"{worker.label}.log" for worker in resources.workers}
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
            _wait_postgres(resources.postgres_container)
            _wait_redis(resources.redis_container)
            smoke._run([str(API_ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=env)

            from app.workers.celery_app import celery_app

            # Redis unavailable before delivery: the queued task cannot enter
            # its body while the broker/lock store is down; restart drains it once.
            asyncio.run(_reset_database())
            before_spec = resources.worker("redis-before")
            queued = celery_app.send_task(TASK_NAME, queue=before_spec.queue)
            queued_task_id = queued.id
            _container(resources.redis_container, "stop")
            before_worker = _launch_worker(before_spec, logs[before_spec.label], env, hold=True)
            workers.append(before_worker)
            time.sleep(2)
            before_down_log = logs[before_spec.label].read_text()
            if OWNER_MARKER in before_down_log or "tasks.signals_complete" in before_down_log:
                raise smoke.EvidenceFailure("Task body ran while Redis was unavailable")
            _container(resources.redis_container, "start")
            _wait_redis(resources.redis_container)
            smoke._wait(
                "redis-before worker recovery",
                smoke.WORKER_READY_TIMEOUT,
                lambda: _worker_ready(logs[before_spec.label]),
            )
            queued_result = _result_value(queued)
            _assert_effective(queued_result, "queued Redis recovery")
            later_before = _result_value(celery_app.send_task(TASK_NAME, queue=before_spec.queue))
            _assert_duplicate_noop(later_before, "later Redis recovery")
            redis_before_db = asyncio.run(smoke._database_evidence())
            _stop_worker(before_worker)
            _flush_redis(resources.redis_container)

            # Redis interruption after the exact lock-owned marker. The in-flight
            # delivery may finish or be redelivered, but the ledger remains one.
            asyncio.run(_reset_database())
            during_spec = resources.worker("redis-during")
            during_worker = _start_and_wait(
                workers,
                during_spec,
                logs[during_spec.label],
                env,
                hold=True,
                pool="prefork",
                ttl_seconds=INTERRUPTION_LOCK_TTL_SECONDS,
            )
            during = celery_app.send_task(TASK_NAME, queue=during_spec.queue)
            during_task_id = during.id
            smoke._wait(
                "redis-during owner marker",
                SCENARIO_TIMEOUT,
                lambda: bool(_claims(logs[during_spec.label].read_text())),
            )
            _release_result(during)
            del during
            _container(resources.redis_container, "stop")
            time.sleep(FAULT_HOLD_SECONDS + 1)
            _container(resources.redis_container, "start")
            _wait_redis(resources.redis_container)
            recovery_spec = resources.worker("recovery")
            _stop_worker(during_worker)
            smoke._wait(
                "interrupted lock lease expiry",
                INTERRUPTION_LOCK_TTL_SECONDS + 5,
                lambda: asyncio.run(recovery._redis_value(env["REDIS_URL"], LOCK_KEY)) is None,
            )
            during_recovery_worker = _start_and_wait(
                workers,
                recovery_spec,
                logs[recovery_spec.label],
                env,
            )
            during_recovery = _result_value(
                celery_app.send_task(TASK_NAME, queue=recovery_spec.queue)
            )
            if during_recovery.get("orders_submitted") not in (0, 1):
                raise smoke.EvidenceFailure(
                    f"Unexpected Redis interruption recovery: {during_recovery}"
                )
            later_during = _result_value(celery_app.send_task(TASK_NAME, queue=recovery_spec.queue))
            _assert_duplicate_noop(later_during, "post-interruption Redis dispatch")
            redis_during_db = asyncio.run(smoke._database_evidence())
            during_log = logs[during_spec.label].read_text()
            if not any(
                marker in during_log
                for marker in (
                    "Connection closed",
                    "Connection refused",
                    "ConnectionError",
                    "connection lost",
                )
            ):
                raise smoke.EvidenceFailure("Redis interruption was not visible in worker evidence")
            _stop_worker(during_recovery_worker)
            _flush_redis(resources.redis_container)

            # PostgreSQL unavailable before task: visible failure, zero effects,
            # schema still at head after restart, and later work succeeds once.
            asyncio.run(_reset_database())
            pg_spec = resources.worker("pg-before")
            pg_task = celery_app.send_task(TASK_NAME, queue=pg_spec.queue)
            pg_task_id = pg_task.id
            _container(resources.postgres_container, "stop")
            pg_worker = _start_and_wait(workers, pg_spec, logs[pg_spec.label], env, hold=True)
            pg_failure = _result_failure(pg_task)
            pg_log = logs[pg_spec.label].read_text()
            if "tasks.failed" not in pg_log:
                raise smoke.EvidenceFailure("PostgreSQL outage was not visible as task failure")
            _container(resources.postgres_container, "start")
            _wait_postgres(resources.postgres_container)
            heads = smoke._run([str(API_ROOT / ".venv/bin/alembic"), "heads"], env=env).split()[0]
            current = smoke._run([str(API_ROOT / ".venv/bin/alembic"), "current"], env=env)
            if heads not in current:
                raise smoke.EvidenceFailure(
                    "PostgreSQL schema was not at Alembic head after restart"
                )
            _stop_worker(pg_worker)
            recovery_worker = _start_and_wait(
                workers, recovery_spec, logs[recovery_spec.label], env
            )
            pg_later = _result_value(celery_app.send_task(TASK_NAME, queue=recovery_spec.queue))
            _assert_effective(pg_later, "PostgreSQL later dispatch")
            pg_db = asyncio.run(smoke._database_evidence())
            _stop_worker(recovery_worker)
            _flush_redis(resources.redis_container)

            # Deterministic exception after effects are staged but before the
            # task-boundary commit: failure is visible and every effect rolls back.
            asyncio.run(_reset_database())
            fault_spec = resources.worker("fault")
            fault_worker = _start_and_wait(
                workers,
                fault_spec,
                logs[fault_spec.label],
                env,
                precommit_fault=True,
            )
            fault_task = celery_app.send_task(TASK_NAME, queue=fault_spec.queue)
            fault_task_id = fault_task.id
            fault_failure = _result_failure(fault_task)
            fault_log = logs[fault_spec.label].read_text()
            if PRECOMMIT_MARKER not in fault_log or "tasks.failed" not in fault_log:
                raise smoke.EvidenceFailure("Pre-commit failure was not visibly evidenced")
            zero_after_fault = asyncio.run(_execution_counts())
            _assert_zero_execution_counts(zero_after_fault)
            _stop_worker(fault_worker)
            recovery_worker = _start_and_wait(
                workers, recovery_spec, logs[recovery_spec.label], env
            )
            fault_later = _result_value(celery_app.send_task(TASK_NAME, queue=recovery_spec.queue))
            _assert_effective(fault_later, "pre-commit fault recovery")
            fault_db = asyncio.run(smoke._database_evidence())
            _stop_worker(recovery_worker)
            _flush_redis(resources.redis_container)

            # Kill the exact solo worker holding a bounded harness-only lease,
            # then invoke Kombu's real Redis unacked-delivery restore primitive.
            # A clean replacement process receives the same redelivered task ID.
            asyncio.run(_reset_database())
            death_spec = resources.worker("death")
            death_worker = _start_and_wait(
                workers,
                death_spec,
                logs[death_spec.label],
                env,
                hold=True,
                ttl_seconds=INTERRUPTION_LOCK_TTL_SECONDS,
            )
            death_task_id = f"d2-redelivery-{uuid.uuid4()}"
            death_task = celery_app.send_task(
                TASK_NAME, queue=death_spec.queue, task_id=death_task_id
            )
            smoke._wait(
                "first worker lock owner",
                SCENARIO_TIMEOUT,
                lambda: len(_claims(logs[death_spec.label].read_text())) >= 1,
            )
            first_claim = _claims(logs[death_spec.label].read_text())[0]
            if first_claim["pid"] != death_worker.pid:
                raise smoke.EvidenceFailure("Solo lock claim did not bind to the worker PID")
            os.kill(death_worker.pid, signal.SIGKILL)
            death_worker.wait(timeout=5)
            preserved_token = asyncio.run(recovery._redis_value(env["REDIS_URL"], LOCK_KEY))
            if preserved_token != first_claim["token"]:
                raise smoke.EvidenceFailure("Killed worker did not leave its lease intact")
            expiry_started = time.monotonic()
            smoke._wait(
                "killed worker lease expiry",
                INTERRUPTION_LOCK_TTL_SECONDS + 5,
                lambda: asyncio.run(recovery._redis_value(env["REDIS_URL"], LOCK_KEY)) is None,
            )
            expiry_seconds = time.monotonic() - expiry_started
            delivery_tag = _restore_unacked_delivery(env["REDIS_URL"], death_task_id)
            replacement_spec = resources.worker("death-replacement")
            replacement_worker = _start_and_wait(
                workers,
                replacement_spec,
                logs[replacement_spec.label],
                env,
                hold=True,
                consume_queue=death_spec.queue,
            )
            smoke._wait(
                "same-delivery replacement claim after lease expiry",
                SCENARIO_TIMEOUT,
                lambda: len(_claims(logs[replacement_spec.label].read_text())) >= 1,
            )
            second_claim = _claims(logs[replacement_spec.label].read_text())[0]
            if first_claim["pid"] == second_claim["pid"]:
                raise smoke.EvidenceFailure("Replacement did not use a new worker process")
            if first_claim["token"] == second_claim["token"]:
                raise smoke.EvidenceFailure("Redelivery reused a process-owner token")
            smoke._wait(
                "redelivery database effect",
                SCENARIO_TIMEOUT,
                lambda: asyncio.run(_execution_counts())["orders"] == 1,
            )
            death_result = _result_value(death_task)
            _assert_effective(death_result, "same-delivery lease-expiry recovery")
            death_later = _result_value(celery_app.send_task(TASK_NAME, queue=death_spec.queue))
            _assert_duplicate_noop(death_later, "post-death later dispatch")
            death_db = asyncio.run(smoke._database_evidence())
            _stop_worker(replacement_worker)

            for path in logs.values():
                if path.exists():
                    smoke.assert_safe_worker_log(path.read_text())

            return {
                "result": "PASS",
                "git_sha": smoke._run(["git", "rev-parse", "HEAD"]).strip(),
                "redis_unavailable_before_delivery": {
                    "task_id": queued_task_id,
                    "body_absent_while_down": True,
                    "result": queued_result,
                    "later_result": later_before,
                    "database": redis_before_db,
                },
                "redis_interrupted_while_locked": {
                    "interrupted_task_id": during_task_id,
                    "visible_connection_loss": True,
                    "lease_expired_bounded": True,
                    "recovery_result": during_recovery,
                    "later_result": later_during,
                    "database": redis_during_db,
                },
                "postgres_unavailable_before_task": {
                    "task_id": pg_task_id,
                    "failure": pg_failure,
                    "schema_head": heads,
                    "later_result": pg_later,
                    "database": pg_db,
                },
                "precommit_rollback": {
                    "task_id": fault_task_id,
                    "failure": fault_failure,
                    "zero_counts": zero_after_fault,
                    "later_result": fault_later,
                    "database": fault_db,
                },
                "same_delivery_worker_loss": {
                    "task_id": death_task_id,
                    "delivery_tag": delivery_tag,
                    "killed_worker_pid": first_claim["pid"],
                    "replacement_worker_pid": second_claim["pid"],
                    "old_token_preserved": True,
                    "lease_expiry_seconds": round(expiry_seconds, 3),
                    "same_task_id_restored": True,
                    "result": death_result,
                    "later_result": death_later,
                    "database": death_db,
                },
                "retry_policy": {"max_retries": 0, "unchanged": True},
                "broker_tripwires": "armed and untriggered",
                "owned_resources_removed": True,
            }
        except Exception as exc:
            tails = "\n".join(
                f"{label}:\n{path.read_text()[-6000:]}"
                for label, path in logs.items()
                if path.exists()
            )
            raise smoke.EvidenceFailure(f"{exc}\nWORKER LOG TAILS:\n{tails}") from exc
        finally:
            for worker in workers:
                smoke._stop(worker)
            smoke._remove_owned_containers(tuple(created_containers))


def main() -> int:
    try:
        print(json.dumps(run_recovery(), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"REAL WORKER INTERRUPTION RECOVERY FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
