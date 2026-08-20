"""Policy tests for the D2 real-worker interruption recovery harness."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from scripts import real_worker_interruption_recovery as interruption
from scripts import real_worker_tripwire_worker as launcher


def test_d2_resources_are_unique_and_owned() -> None:
    resources = interruption.D2Resources.for_token("abc123")

    assert resources.postgres_container == "cashguard-d2-pg-abc123"
    assert resources.redis_container == "cashguard-d2-redis-abc123"
    assert len(resources.workers) == 7
    assert len({worker.hostname for worker in resources.workers}) == 7
    assert len({worker.queue for worker in resources.workers}) == 7


@pytest.mark.parametrize("token", ("with-hyphen", "x" * 17, ""))
def test_d2_resources_reject_unsafe_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="1-16 alphanumeric"):
        interruption.D2Resources.for_token(token)


def test_d2_faults_are_bounded_and_mock_only() -> None:
    assert (
        0
        < interruption.FAULT_HOLD_SECONDS
        < interruption.INTERRUPTION_LOCK_TTL_SECONDS
        < interruption.SCENARIO_TIMEOUT
    )
    launcher._validate_lock_hold(
        "mock", hold_seconds=interruption.FAULT_HOLD_SECONDS, ttl_seconds=None
    )
    source = inspect.getsource(launcher.main)
    assert "pre-commit fault requires APP_MODE=mock" in source
    assert 'choices=("solo", "prefork")' in source


def test_d2_uses_real_queue_dispatch_and_exact_owned_faults() -> None:
    source = Path(interruption.__file__).read_text()

    assert "run_strategy_signals.run(" not in source
    assert source.count("celery_app.send_task(") >= 9
    assert "signal.SIGKILL" in source
    assert "_restore_unacked_delivery" in source
    assert "restore_by_tag" in source
    assert 'docker", action, container' in source
    assert "_remove_owned_containers(tuple(created_containers))" in source
    assert "_assert_zero_execution_counts" in source
    assert "_database_evidence" in source
    assert "Trade," in source
    assert "AuditLog," in source
    assert ".where(AuditLog.action" not in source
    assert '"max_retries": 0' in source


def test_d2_owner_parser_requires_child_pid_and_exact_fields() -> None:
    line = interruption.OWNER_MARKER + (
        '{"pid": 1234, "task": "run_strategy_signals", "token": "death:random", "worker": "death"}'
    )

    assert interruption._claims(line) == [
        {
            "pid": 1234,
            "task": "run_strategy_signals",
            "token": "death:random",
            "worker": "death",
        }
    ]

    with pytest.raises(RuntimeError, match="safe child PID"):
        interruption._claims(line.replace('"pid": 1234', '"pid": 1'))


def test_precommit_fault_is_immediately_before_commit_boundary() -> None:
    source = inspect.getsource(launcher._install_precommit_fault)

    marker_index = source.index("CASHGUARD_HARNESS_PRECOMMIT_FAULT")
    raise_index = source.index("CASHGUARD_DETERMINISTIC_PRECOMMIT_FAULT")
    original_index = source.index("original_complete_task(db, task_name, summary)")
    assert marker_index < raise_index < original_index
