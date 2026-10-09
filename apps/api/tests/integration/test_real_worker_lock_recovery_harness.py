"""Policy tests for the real-worker contention and recovery harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import real_worker_lock_recovery as recovery
from scripts import real_worker_paper_smoke as smoke
from scripts import real_worker_tripwire_worker as worker_launcher


def _monitor_line(*args: str) -> str:
    return " ".join(json.dumps(arg) for arg in args)


def _owner_line(worker: str, token: str) -> str:
    return recovery.OWNER_MARKER + json.dumps(
        {"task": recovery.LOCK_NAME, "worker": worker, "token": token},
        sort_keys=True,
    )


def test_d1_timeouts_and_fault_injection_are_finite() -> None:
    assert 0 < recovery.COMPETITION_HOLD_SECONDS < recovery.HARNESS_LOCK_TTL_SECONDS
    assert 0 < recovery.HARNESS_LOCK_TTL_SECONDS <= 10
    assert 0 < recovery.RECOVERY_TIMEOUT <= 30
    assert (
        recovery.COMPETITION_HOLD_SECONDS + recovery.TASK_TIME_LIMIT_SECONDS
        < recovery.PRODUCTION_LOCK_TTL_SECONDS
    )


def test_d1_resources_name_two_workers_and_a_replacement() -> None:
    resources = recovery.D1Resources.for_token("abc123")

    assert resources.postgres_container == "cashguard-d1-pg-abc123"
    assert resources.redis_container == "cashguard-d1-redis-abc123"
    assert resources.worker_a.hostname == "cashguard-d1-a-abc123@%h"
    assert resources.worker_b.hostname == "cashguard-d1-b-abc123@%h"
    assert resources.replacement.hostname == "cashguard-d1-r-abc123@%h"
    assert (
        len({resources.worker_a.queue, resources.worker_b.queue, resources.replacement.queue}) == 3
    )


def test_winner_evidence_binds_exact_worker_token_to_real_redis_set() -> None:
    key = "celery:task_lock:run_strategy_signals"
    token = "Owner-Case-Sensitive"
    monitor = _monitor_line("set", key, token, "ex", "5", "nx")
    logs = {
        "worker-a": _owner_line("worker-a", token),
        "worker-b": "CASHGUARD_BROKER_TRIPWIRES_ARMED",
    }

    assert recovery.assert_lock_winner(monitor, logs, key, expected_ttl_seconds=5) == {
        "worker": "worker-a",
        "token": token,
    }

    with pytest.raises(smoke.EvidenceFailure, match="exact Redis acquisition"):
        recovery.assert_lock_winner(
            monitor,
            {"worker-a": _owner_line("worker-a", token.lower())},
            key,
            expected_ttl_seconds=5,
        )

    with pytest.raises(smoke.EvidenceFailure, match="expected lock lease"):
        recovery.assert_lock_winner(
            monitor,
            logs,
            key,
            expected_ttl_seconds=270,
        )


def test_winner_evidence_rejects_multiple_owner_claims() -> None:
    key = "celery:task_lock:run_strategy_signals"
    monitor = "\n".join(
        (
            _monitor_line("set", key, "owner-a", "ex", "5", "nx"),
            _monitor_line("set", key, "owner-b", "ex", "5", "nx"),
        )
    )
    logs = {
        "worker-a": _owner_line("worker-a", "owner-a"),
        "worker-b": _owner_line("worker-b", "owner-b"),
    }

    with pytest.raises(smoke.EvidenceFailure, match="exactly one lock owner"):
        recovery.assert_lock_winner(monitor, logs, key, expected_ttl_seconds=5)


@pytest.mark.parametrize("value", ["", "0", "-1", "nan", "inf", "not-a-number"])
def test_worker_launcher_rejects_unbounded_hold_values(value: str) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        worker_launcher._finite_positive(value, "hold")


def test_worker_launcher_accepts_bounded_positive_hold_values() -> None:
    assert worker_launcher._finite_positive("0.25", "hold") == 0.25


def test_worker_launcher_fault_injection_is_mock_only_and_lease_outlives_hold() -> None:
    worker_launcher._validate_lock_hold("mock", hold_seconds=2.0, ttl_seconds=None)
    worker_launcher._validate_lock_hold("mock", hold_seconds=2.0, ttl_seconds=5)

    with pytest.raises(ValueError, match="APP_MODE=mock"):
        worker_launcher._validate_lock_hold("paper", hold_seconds=2.0, ttl_seconds=5)
    with pytest.raises(ValueError, match="outlive"):
        worker_launcher._validate_lock_hold("mock", hold_seconds=5.0, ttl_seconds=5)


def test_worker_launcher_captures_a_process_bound_unique_token() -> None:
    generated = iter(("random-a", "random-b"))
    token_factory, token_state = worker_launcher._owned_token_factory(
        "worker-a", lambda _size: next(generated)
    )

    first = token_factory(32)
    second = token_factory(32)

    assert first == "worker-a:random-a"
    assert second == "worker-a:random-b"
    assert token_state["current"] == second


def test_d1_harness_dispatches_real_tasks_to_distinct_worker_queues() -> None:
    source = Path(recovery.__file__).read_text()

    assert source.count("celery_app.send_task(") >= 4
    assert "run_strategy_signals.run(" not in source
    assert "worker_a.queue" in source
    assert "worker_b.queue" in source
    assert "replacement.queue" in source
    assert ".kill()" in source
    assert "_prove_expired_owner_cannot_delete_successor" in source
    assert "assert_safe_worker_log" in source
    assert "created_containers: list[str]" in source
    assert "_remove_owned_containers(tuple(created_containers))" in source
    assert '"delivery_recovery": "deferred_to_D2"' in source
    assert 'consume_queues=f"{resources.replacement.queue}' not in source
