"""The real-worker safety proofs must run, unconditionally, inside a required CI job."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
# The repository ruleset requires this job by name, so a failing step blocks the merge.
REQUIRED_JOB_NAME = "Backend"
REQUIRED_PROOFS = (
    "scripts/real_worker_paper_smoke.py",
    "scripts/real_worker_paper_chaos.py",
    "scripts/real_worker_lock_recovery.py",
)
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
STEP_START = re.compile(r"^      - ")
FAILURE_MASKS = ("continue-on-error", "|| true", "||true", "set +e", "|| echo", "|| exit 0")


def _job_blocks(workflow_text: str) -> dict[str, list[str]]:
    """Lines of each job under ``jobs:``, keyed by job id."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_jobs = False
    for line in workflow_text.splitlines():
        if line.rstrip() == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        job_key = JOB_KEY.match(line)
        if job_key:
            current = blocks.setdefault(job_key.group(1), [])
        elif current is not None:
            current.append(line)
    return blocks


def _steps(job_lines: list[str]) -> list[str]:
    """Text of every step of a job, in order."""
    steps: list[list[str]] = []
    in_steps = False
    for line in job_lines:
        if line.rstrip() == "    steps:":
            in_steps = True
        elif in_steps and STEP_START.match(line):
            steps.append([line])
        elif in_steps and steps:
            steps[-1].append(line)
    return ["\n".join(step) for step in steps]


def _required_job_lines(workflow_text: str) -> list[str]:
    matches = [
        lines
        for lines in _job_blocks(workflow_text).values()
        if f"    name: {REQUIRED_JOB_NAME}" in lines
    ]
    assert len(matches) == 1, f"expected exactly one job named {REQUIRED_JOB_NAME}"
    return matches[0]


def _proof_steps(workflow_text: str, script: str) -> list[str]:
    return [step for step in _steps(_required_job_lines(workflow_text)) if script in step]


@pytest.mark.parametrize("script", REQUIRED_PROOFS)
def test_required_job_runs_each_real_worker_proof_exactly_once(script: str) -> None:
    steps = _proof_steps(CI_WORKFLOW.read_text(), script)

    assert len(steps) == 1, f"{script} must run in exactly one step of {REQUIRED_JOB_NAME}"
    assert re.search(rf"^\s+run: python {re.escape(script)}\s*$", steps[0], re.MULTILINE)


@pytest.mark.parametrize("script", REQUIRED_PROOFS)
def test_real_worker_proof_steps_cannot_be_skipped_or_have_failures_masked(script: str) -> None:
    (step,) = _proof_steps(CI_WORKFLOW.read_text(), script)

    assert not re.search(r"^\s+if:", step, re.MULTILINE)
    assert not any(mask in step for mask in FAILURE_MASKS)


def test_required_job_itself_is_unconditional() -> None:
    job_header = "\n".join(_required_job_lines(CI_WORKFLOW.read_text())).split("    steps:")[0]

    assert not re.search(r"^    if:", job_header, re.MULTILINE)
    assert "continue-on-error" not in job_header


@pytest.mark.parametrize("script", REQUIRED_PROOFS)
def test_every_proof_script_exists(script: str) -> None:
    assert (REPO_ROOT / "apps" / "api" / script).is_file()


def test_parser_reports_a_masked_or_conditional_step() -> None:
    workflow = "\n".join(
        [
            "jobs:",
            "  backend:",
            f"    name: {REQUIRED_JOB_NAME}",
            "    steps:",
            "      - name: Proof",
            "        if: github.event_name == 'push'",
            "        run: python scripts/real_worker_paper_smoke.py || true",
            "  other:",
            "    name: Other",
            "    steps:",
            "      - name: Elsewhere",
            "        run: python scripts/real_worker_paper_chaos.py",
        ]
    )

    (step,) = _proof_steps(workflow, "scripts/real_worker_paper_smoke.py")

    assert re.search(r"^\s+if:", step, re.MULTILINE)
    assert any(mask in step for mask in FAILURE_MASKS)
    assert _proof_steps(workflow, "scripts/real_worker_paper_chaos.py") == []
