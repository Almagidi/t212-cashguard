"""Policy tests for D3 chaos and final-soak developer harnesses."""

from __future__ import annotations

import inspect

import pytest
from scripts import real_worker_paper_chaos as chaos
from scripts import real_worker_paper_soak as soak
from scripts import real_worker_tripwire_worker as launcher


def test_d3_resources_are_unique_and_owned() -> None:
    resources = chaos.D3Resources.for_token("abc123")

    assert resources.postgres_container == "cashguard-d3-pg-abc123"
    assert resources.redis_container == "cashguard-d3-redis-abc123"
    assert resources.cleanup_container == "cashguard-d3-cleanup-abc123"
    assert len({worker.hostname for worker in resources.workers}) == 3
    assert len({worker.queue for worker in resources.workers}) == 3
    assert resources.workers[0].bar_offset_minutes == -1440
    assert resources.workers[1].bar_offset_minutes is None


@pytest.mark.parametrize("token", ("", "with-hyphen", "x" * 17))
def test_d3_resources_reject_unsafe_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="1-16 alphanumeric"):
        chaos.D3Resources.for_token(token)
    with pytest.raises(ValueError, match="1-16 alphanumeric"):
        soak.SoakResources.for_token(token)


@pytest.mark.parametrize(
    ("dispatches", "interval", "mode", "message"),
    (
        (29, 2.0, "mock", "30-60"),
        (61, 2.0, "mock", "30-60"),
        (30, 0.0, "mock", "finite, positive"),
        (30, float("inf"), "mock", "finite, positive"),
        (30, 2.0, "demo", "APP_MODE=mock"),
    ),
)
def test_soak_policy_rejects_unsafe_bounds(
    dispatches: int, interval: float, mode: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        soak.validate_soak_policy(dispatches, interval, mode)


def test_soak_policy_accepts_required_dispatch_range() -> None:
    soak.validate_soak_policy(30, 2.0, "mock")
    soak.validate_soak_policy(60, 120.0, "MOCK")


def test_d3_uses_real_dispatch_and_existing_safety_mechanisms() -> None:
    chaos_source = inspect.getsource(chaos)
    soak_source = inspect.getsource(soak)

    assert "run_strategy_signals.run(" not in chaos_source + soak_source
    assert "celery_app.send_task(" in chaos_source
    assert "activate_kill_switch" in chaos_source
    assert "_make_decision_probe_dry_run" in chaos_source
    assert "fill_partial_order" in chaos_source
    assert "cancel_all_pending_summary" in chaos_source
    assert "_prove_cleanup_failure" in chaos_source
    assert "injected cleanup failure" in chaos_source
    assert 'git", "rev-parse", "HEAD' in chaos_source
    assert "_tested_git_sha" in chaos_source + soak_source
    assert "baseline_database" in soak_source
    assert "workers: list" in soak_source
    assert "len(claims) != 6" in chaos_source
    assert "time.sleep(interval_seconds)" in soak_source
    assert "task_ids" in soak_source
    assert "invariant_violations" in soak_source


def test_exact_sha_evidence_rejects_dirty_harness_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dirty_run(args: list[str], **_kwargs: object) -> str:
        if args[1] == "status":
            return " M apps/api/app/execution/paper_engine.py\n"
        raise AssertionError("rev-parse must not run for a dirty tree")

    monkeypatch.setattr(chaos.smoke, "_run", dirty_run)
    with pytest.raises(chaos.smoke.EvidenceFailure, match="exact-SHA"):
        chaos._tested_git_sha()


def test_exact_sha_evidence_detects_revision_change(monkeypatch: pytest.MonkeyPatch) -> None:
    def clean_run(args: list[str], **_kwargs: object) -> str:
        return "" if args[1] == "status" else "new-sha\n"

    monkeypatch.setattr(chaos.smoke, "_run", clean_run)
    with pytest.raises(chaos.smoke.EvidenceFailure, match="changed during"):
        chaos._tested_git_sha("old-sha")


def test_bar_offset_is_harness_only_and_mock_guarded() -> None:
    source = inspect.getsource(launcher.main)

    assert "mock bar offset requires APP_MODE=mock" in source
    assert "mock bar offset must be a non-zero whole number of minutes" in source
    shifted = inspect.getsource(launcher._install_mock_bar_offset)
    assert "timedelta(minutes=offset_minutes)" in shifted
