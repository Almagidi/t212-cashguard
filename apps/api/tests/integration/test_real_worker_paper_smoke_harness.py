"""Structural and failure-policy tests for the real Celery paper smoke harness."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import real_worker_paper_smoke as smoke
from scripts import real_worker_tripwire_worker as worker_launcher


def test_harness_timeouts_are_finite_and_bounded() -> None:
    assert 0 < smoke.CONTAINER_READY_TIMEOUT <= 60
    assert 0 < smoke.WORKER_READY_TIMEOUT <= 60
    assert 0 < smoke.TASK_RESULT_TIMEOUT <= 180


def test_owned_resource_names_are_unique_and_explicit() -> None:
    resources = smoke.OwnedResources.for_token("abc123")

    assert resources.postgres_container == "cashguard-worker-pg-abc123"
    assert resources.redis_container == "cashguard-worker-redis-abc123"
    assert resources.worker_hostname == "cashguard-worker-abc123@%h"


@pytest.mark.parametrize(
    "forbidden",
    [
        "FORBIDDEN_CONSTRUCTION:Trading212Adapter",
        "FORBIDDEN_CONSTRUCTION:KrakenAdapter",
        "FORBIDDEN_NETWORK:8.8.8.8",
        "https://live.trading212.com",
        "https://demo.trading212.com",
        "https://api.kraken.com",
        "redis.task_lock_unavailable",
    ],
)
def test_worker_log_policy_rejects_forbidden_evidence(forbidden: str) -> None:
    with pytest.raises(smoke.EvidenceFailure, match="Forbidden worker evidence"):
        smoke.assert_safe_worker_log(f"normal output\n{forbidden}\n")


def test_worker_log_policy_requires_armed_tripwires() -> None:
    with pytest.raises(smoke.EvidenceFailure, match="tripwires were not armed"):
        smoke.assert_safe_worker_log("worker ready")

    with pytest.raises(smoke.EvidenceFailure, match="network tripwire was not armed"):
        smoke.assert_safe_worker_log("CASHGUARD_BROKER_TRIPWIRES_ARMED\nworker ready")

    smoke.assert_safe_worker_log(
        "CASHGUARD_BROKER_TRIPWIRES_ARMED CASHGUARD_NETWORK_TRIPWIRE_ARMED\nworker ready"
    )


def test_harness_uses_actual_registered_task_and_real_worker_launcher() -> None:
    harness_source = Path(smoke.__file__).read_text()
    launcher_source = Path(smoke.__file__).with_name("real_worker_tripwire_worker.py").read_text()

    assert "send_task(TASK_NAME)" in harness_source
    assert "run_strategy_signals.run(" not in harness_source
    assert "worker_main" in launcher_source
    assert "CASHGUARD_BROKER_TRIPWIRES_ARMED" in launcher_source
    assert "CASHGUARD_NETWORK_TRIPWIRE_ARMED" in launcher_source
    assert "FORBIDDEN_CONSTRUCTION:Trading212Adapter" in launcher_source
    assert "FORBIDDEN_CONSTRUCTION:KrakenAdapter" in launcher_source
    assert "FORBIDDEN_CONSTRUCTION:create_trading212_provider_adapter" in launcher_source
    assert "StrategyRunner =" not in launcher_source
    assert "RiskEngine =" not in launcher_source
    assert "PaperExecutionEngine =" not in launcher_source


def test_harness_neutralizes_ambient_notification_and_sentry_configuration() -> None:
    env = smoke._environment(5432, 6379)

    for key in smoke.DISABLED_EXTERNAL_INTEGRATIONS:
        assert env[key] == ""


def test_worker_network_tripwire_rejects_non_loopback_connections() -> None:
    with pytest.raises(AssertionError, match=r"FORBIDDEN_NETWORK:8\.8\.8\.8"):
        worker_launcher._guarded_connect(object(), ("8.8.8.8", 443))


def _monitor_line(*args: str) -> str:
    return " ".join(json.dumps(arg) for arg in args)


def test_redis_lock_evidence_binds_commands_to_exact_positions() -> None:
    key = "celery:task_lock:run_strategy_signals"
    release_script = smoke._release_lock_script()
    acquisition = _monitor_line("set", key, "owner-a", "ex", "270", "nx")

    invalid_releases = (
        # The expected key is present only as an unused extra argument.
        _monitor_line("eval", release_script, "1", "wrong-key", "owner-a", key),
        # The acquired token is present only as an unused extra argument.
        _monitor_line("eval", release_script, "1", key, "owner-b", "owner-a"),
        # Redis string values are case-sensitive.
        _monitor_line("eval", release_script, "1", key, "Owner-A"),
        # GET and DEL substrings alone do not prove an ownership comparison.
        _monitor_line(
            "eval",
            'redis.call("get", KEYS[1]); return redis.call("del", KEYS[1])',
            "1",
            key,
            "owner-a",
        ),
    )
    for invalid_release in invalid_releases:
        with pytest.raises(smoke.EvidenceFailure, match="same owner token"):
            smoke.assert_redis_lock_evidence(
                f"{acquisition}\n{invalid_release}\n",
                key,
            )

    smoke.assert_redis_lock_evidence(
        f"{acquisition}\n{_monitor_line('eval', release_script, '1', key, 'owner-a')}\n",
        key,
    )


def test_cleanup_fails_when_an_owned_container_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="busy"),
    )
    monkeypatch.setattr(smoke, "_listed_owned_containers", lambda _names: {"owned-pg"})

    with pytest.raises(smoke.EvidenceFailure, match="owned-pg"):
        smoke._remove_owned_containers(("owned-pg",))
