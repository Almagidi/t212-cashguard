"""Real Redis/Celery proof of competing-worker exclusion and death recovery."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
smoke = importlib.import_module("scripts.real_worker_paper_smoke")

COMPETITION_HOLD_SECONDS = 2.0
HARNESS_LOCK_TTL_SECONDS = 5
RECOVERY_TIMEOUT = 10
TASK_TIME_LIMIT_SECONDS = 240
PRODUCTION_LOCK_TTL_SECONDS = 270
OWNER_MARKER = "CASHGUARD_HARNESS_LOCK_OWNER "
LOCK_NAME = "run_strategy_signals"
LOCK_KEY = f"celery:task_lock:{LOCK_NAME}"
TASK_NAME = smoke.TASK_NAME


@dataclass(frozen=True)
class WorkerResource:
    worker_id: str
    hostname: str
    queue: str


@dataclass(frozen=True)
class D1Resources:
    postgres_container: str
    redis_container: str
    worker_a: WorkerResource
    worker_b: WorkerResource
    replacement: WorkerResource

    @classmethod
    def for_token(cls, token: str) -> D1Resources:
        if not token.isalnum() or len(token) > 16:
            raise ValueError("resource token must be 1-16 alphanumeric characters")

        def worker(label: str) -> WorkerResource:
            return WorkerResource(
                worker_id=f"worker-{label}",
                hostname=f"cashguard-d1-{label}-{token}@%h",
                queue=f"cashguard-d1-{label}-{token}",
            )

        return cls(
            postgres_container=f"cashguard-d1-pg-{token}",
            redis_container=f"cashguard-d1-redis-{token}",
            worker_a=worker("a"),
            worker_b=worker("b"),
            replacement=worker("r"),
        )


def _owner_claims(log_text: str) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for line in log_text.splitlines():
        if OWNER_MARKER not in line:
            continue
        try:
            claim = json.loads(line.split(OWNER_MARKER, 1)[1])
        except json.JSONDecodeError as exc:
            raise smoke.EvidenceFailure("Malformed worker lock-owner evidence") from exc
        if not all(isinstance(claim.get(key), str) for key in ("task", "worker", "token")):
            raise smoke.EvidenceFailure("Incomplete worker lock-owner evidence")
        claims.append(claim)
    return claims


def assert_lock_winner(
    monitor_text: str,
    worker_logs: dict[str, str],
    lock_key: str,
    *,
    expected_ttl_seconds: int,
) -> dict[str, str]:
    claims: list[dict[str, str]] = []
    for expected_worker, log_text in worker_logs.items():
        for claim in _owner_claims(log_text):
            if claim["task"] != LOCK_NAME or claim["worker"] != expected_worker:
                raise smoke.EvidenceFailure("Worker lock-owner evidence identity mismatch")
            claims.append(claim)
    if len(claims) != 1:
        raise smoke.EvidenceFailure(f"Expected exactly one lock owner, observed {len(claims)}")

    claim = claims[0]
    acquisitions = [
        args
        for args in (smoke._monitor_command_args(line) for line in monitor_text.splitlines())
        if (
            len(args) == 6
            and args[0].upper() == "SET"
            and args[1] == lock_key
            and args[2] == claim["token"]
            and args[3].upper() == "EX"
            and args[4].isdigit()
            and int(args[4]) > 0
            and args[5].upper() == "NX"
        )
    ]
    if not acquisitions:
        raise smoke.EvidenceFailure(
            "Worker owner claim was not bound to the exact Redis acquisition"
        )
    acquired_with_expected_lease = any(
        len(args) == 6 and int(args[4]) == expected_ttl_seconds for args in acquisitions
    )
    if not acquired_with_expected_lease:
        raise smoke.EvidenceFailure("Redis acquisition did not use the expected lock lease")
    return {"worker": claim["worker"], "token": claim["token"]}


def _worker_ready(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    text = log_path.read_text()
    return "CASHGUARD_NETWORK_TRIPWIRE_ARMED" in text and " ready." in text


def _launch_worker(
    worker: WorkerResource,
    log_path: Path,
    env: dict[str, str],
    *,
    consume_queues: str | None = None,
    inject_hold: bool,
    ttl_seconds: int | None = None,
) -> subprocess.Popen[str]:
    args = [
        sys.executable,
        str(smoke.WORKER_LAUNCHER),
        "--hostname",
        worker.hostname,
        "--queue",
        consume_queues or worker.queue,
    ]
    if inject_hold:
        args.extend(
            [
                "--worker-id",
                worker.worker_id,
                "--lock-hold-seconds",
                str(COMPETITION_HOLD_SECONDS),
            ]
        )
        if ttl_seconds is not None:
            args.extend(["--lock-ttl-seconds", str(ttl_seconds)])
    log_handle = log_path.open("w")
    try:
        return subprocess.Popen(
            args,
            cwd=API_ROOT,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log_handle.close()


def _start_monitor(container: str, path: Path) -> subprocess.Popen[str]:
    log_handle = path.open("w")
    try:
        return subprocess.Popen(
            ["docker", "exec", container, "redis-cli", "MONITOR"],
            cwd=API_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log_handle.close()


async def _redis_value(redis_url: str, key: str) -> str | None:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        value: str | None = await client.get(key)
        return value
    finally:
        await client.aclose()


async def _prove_expired_owner_cannot_delete_successor() -> dict[str, str]:
    from app.core.redis import task_lock

    redis_url = os.environ["REDIS_URL"]
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    old_lock = task_lock("d1_expired_owner", ttl_seconds=1)
    new_lock = task_lock("d1_expired_owner", ttl_seconds=10)
    old_acquired = await old_lock.__aenter__()
    if not old_acquired:
        raise smoke.EvidenceFailure("Old real-Redis proof owner did not acquire")
    old_token = await client.get("celery:task_lock:d1_expired_owner")
    try:
        deadline = time.monotonic() + 3
        while await client.get("celery:task_lock:d1_expired_owner") is not None:
            if time.monotonic() >= deadline:
                raise smoke.EvidenceFailure("Old real-Redis proof lease did not expire")
            await asyncio.sleep(0.05)
        new_acquired = await new_lock.__aenter__()
        if not new_acquired:
            raise smoke.EvidenceFailure("Successor real-Redis proof owner did not acquire")
        new_token = await client.get("celery:task_lock:d1_expired_owner")
        await old_lock.__aexit__(None, None, None)
        after_old_exit = await client.get("celery:task_lock:d1_expired_owner")
        if not old_token or not new_token or old_token == new_token:
            raise smoke.EvidenceFailure("Real Redis did not show unique lock-owner tokens")
        if after_old_exit != new_token:
            raise smoke.EvidenceFailure("Expired owner deleted the successor lock")
        await new_lock.__aexit__(None, None, None)
        return {
            "old_token": old_token,
            "successor_token": new_token,
            "successor_preserved": "true",
        }
    finally:
        await client.aclose()


def _task_is_effective(result: dict[str, Any]) -> bool:
    return result.get("orders_submitted") == 1


def run_recovery() -> dict[str, Any]:
    if not shutil.which("docker"):
        raise smoke.EvidenceFailure("docker is required")
    smoke._run(["docker", "info"])
    resources = D1Resources.for_token(uuid.uuid4().hex[:10])
    pg_port, redis_port = smoke._free_port(), smoke._free_port()
    env = smoke._environment(pg_port, redis_port)
    os.environ.update(env)
    redis_url = env["REDIS_URL"]
    workers: list[subprocess.Popen[str]] = []
    created_containers: list[str] = []
    monitor: subprocess.Popen[str] | None = None

    with tempfile.TemporaryDirectory(prefix="cashguard-d1-") as temp:
        temp_path = Path(temp)
        logs = {
            resources.worker_a.worker_id: temp_path / "worker-a.log",
            resources.worker_b.worker_id: temp_path / "worker-b.log",
            resources.replacement.worker_id: temp_path / "worker-r.log",
        }
        competition_monitor = temp_path / "competition-redis.log"
        death_monitor = temp_path / "death-redis.log"
        death_worker_log = temp_path / "death-worker-a.log"
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
            smoke._wait(
                "PostgreSQL",
                smoke.CONTAINER_READY_TIMEOUT,
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
            smoke._wait(
                "Redis",
                smoke.CONTAINER_READY_TIMEOUT,
                lambda: (
                    subprocess.run(
                        ["docker", "exec", resources.redis_container, "redis-cli", "ping"],
                        capture_output=True,
                    ).returncode
                    == 0
                ),
            )
            smoke._run([str(API_ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=env)
            asyncio.run(smoke._seed_database())
            ownership = asyncio.run(_prove_expired_owner_cannot_delete_successor())

            worker_a = _launch_worker(
                resources.worker_a,
                logs[resources.worker_a.worker_id],
                env,
                inject_hold=True,
            )
            workers.append(worker_a)
            worker_b = _launch_worker(
                resources.worker_b,
                logs[resources.worker_b.worker_id],
                env,
                inject_hold=True,
            )
            workers.append(worker_b)
            smoke._wait(
                "two Celery workers",
                smoke.WORKER_READY_TIMEOUT,
                lambda: (
                    _worker_ready(logs[resources.worker_a.worker_id])
                    and _worker_ready(logs[resources.worker_b.worker_id])
                ),
            )

            from app.workers.celery_app import celery_app

            monitor = _start_monitor(resources.redis_container, competition_monitor)
            time.sleep(0.25)
            task_a = celery_app.send_task(TASK_NAME, queue=resources.worker_a.queue)
            task_b = celery_app.send_task(TASK_NAME, queue=resources.worker_b.queue)
            result_a = task_a.get(timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25)
            result_b = task_b.get(timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25)
            competing_task_ids = [task_a.id, task_b.id]
            task_a.forget()
            task_b.forget()
            del task_a, task_b
            if sum(_task_is_effective(result) for result in (result_a, result_b)) != 1:
                raise smoke.EvidenceFailure(
                    f"Competing tasks did not yield exactly one effect: {result_a}, {result_b}"
                )
            if not any(
                result.get("reason") == "already_running" for result in (result_a, result_b)
            ):
                raise smoke.EvidenceFailure(
                    f"Competing tasks did not demonstrate lock contention: {result_a}, {result_b}"
                )
            smoke._stop(monitor)
            monitor = None
            competition_logs = {
                resource.worker_id: logs[resource.worker_id].read_text()
                for resource in (resources.worker_a, resources.worker_b)
            }
            winner = assert_lock_winner(
                competition_monitor.read_text(),
                competition_logs,
                LOCK_KEY,
                expected_ttl_seconds=PRODUCTION_LOCK_TTL_SECONDS,
            )
            duplicate = celery_app.send_task(TASK_NAME, queue=resources.worker_b.queue).get(
                timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25
            )
            if duplicate.get("orders_submitted") != 0:
                raise smoke.EvidenceFailure(f"Duplicate dispatch was effective: {duplicate}")

            smoke._stop(worker_a)
            death_worker = _launch_worker(
                resources.worker_a,
                death_worker_log,
                env,
                inject_hold=True,
                ttl_seconds=HARNESS_LOCK_TTL_SECONDS,
            )
            workers.append(death_worker)
            smoke._wait(
                "death-test Celery worker",
                smoke.WORKER_READY_TIMEOUT,
                lambda: _worker_ready(death_worker_log),
            )
            monitor = _start_monitor(resources.redis_container, death_monitor)
            time.sleep(0.25)
            killed_task = celery_app.send_task(TASK_NAME, queue=resources.worker_a.queue)
            smoke._wait(
                "worker-a lock ownership marker",
                HARNESS_LOCK_TTL_SECONDS,
                lambda: len(_owner_claims(death_worker_log.read_text())) == 1,
            )
            killed_claim = _owner_claims(death_worker_log.read_text())[0]
            killed_task_id = killed_task.id
            death_worker.kill()
            death_worker.wait(timeout=5)
            token_after_kill = asyncio.run(_redis_value(redis_url, LOCK_KEY))
            if token_after_kill != killed_claim["token"]:
                raise smoke.EvidenceFailure("Killed worker's live lease was released incorrectly")
            killed_task.forget()
            del killed_task
            expiry_started = time.monotonic()
            smoke._wait(
                "killed worker lock expiry",
                RECOVERY_TIMEOUT,
                lambda: asyncio.run(_redis_value(redis_url, LOCK_KEY)) is None,
            )
            expiry_seconds = time.monotonic() - expiry_started

            replacement = _launch_worker(
                resources.replacement,
                logs[resources.replacement.worker_id],
                env,
                inject_hold=False,
            )
            workers.append(replacement)
            smoke._wait(
                "replacement Celery worker",
                smoke.WORKER_READY_TIMEOUT,
                lambda: _worker_ready(logs[resources.replacement.worker_id]),
            )
            later = celery_app.send_task(TASK_NAME, queue=resources.replacement.queue).get(
                timeout=smoke.TASK_RESULT_TIMEOUT, interval=0.25
            )
            if later.get("orders_submitted") != 0:
                raise smoke.EvidenceFailure(f"Recovery dispatch duplicated work: {later}")
            evidence = asyncio.run(smoke._database_evidence())
            smoke._stop(monitor)
            monitor = None

            for path in logs.values():
                if path.exists():
                    smoke.assert_safe_worker_log(path.read_text())
            smoke.assert_safe_worker_log(death_worker_log.read_text())
            death_winner = assert_lock_winner(
                death_monitor.read_text(),
                {
                    resources.worker_a.worker_id: OWNER_MARKER
                    + json.dumps(killed_claim, sort_keys=True)
                },
                LOCK_KEY,
                expected_ttl_seconds=HARNESS_LOCK_TTL_SECONDS,
            )
            return {
                "result": "PASS",
                "git_sha": smoke._run(["git", "rev-parse", "HEAD"]).strip(),
                "competition": {
                    "task_ids": competing_task_ids,
                    "results": [result_a, result_b],
                    "winner": winner,
                    "duplicate": duplicate,
                },
                "lock_ownership": ownership,
                "worker_death": {
                    "task_id": killed_task_id,
                    "delivery_recovery": "deferred_to_D2",
                    "owner": death_winner,
                    "token_preserved_after_kill": True,
                    "expiry_seconds": round(expiry_seconds, 3),
                    "replacement": resources.replacement.worker_id,
                    "later_result": later,
                },
                "database": evidence,
                "broker_tripwires": "armed and untriggered",
                "owned_resources_removed": True,
            }
        except Exception as exc:
            tails = "\n".join(
                f"{worker}:\n{path.read_text()[-5000:]}"
                for worker, path in logs.items()
                if path.exists()
            )
            raise smoke.EvidenceFailure(f"{exc}\nWORKER LOG TAILS:\n{tails}") from exc
        finally:
            smoke._stop(monitor)
            for worker in workers:
                smoke._stop(worker)
            smoke._remove_owned_containers(tuple(created_containers))


def main() -> int:
    try:
        print(json.dumps(run_recovery(), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"REAL WORKER LOCK RECOVERY FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
