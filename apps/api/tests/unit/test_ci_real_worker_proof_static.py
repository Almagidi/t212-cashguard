"""The real-worker safety proofs must run, unconditionally, inside a required CI job."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS_DIR = REPO_ROOT / "apps" / "api" / "scripts"
# The repository ruleset requires this job by name, so a failing step blocks the merge.
REQUIRED_JOB_NAME = "Backend"
REQUIRED_PROOFS = (
    "scripts/real_worker_paper_smoke.py",
    "scripts/real_worker_paper_chaos.py",
    "scripts/real_worker_lock_recovery.py",
    "scripts/real_worker_interruption_recovery.py",
)
ALLOWED_STEP_KEYS = {"name", "run"}
FORBIDDEN_JOB_KEYS = {"if", "continue-on-error", "needs", "strategy"}
MINIMUM_JOB_TIMEOUT_MINUTES = 10
HARNESS_IMAGE = re.compile(r'"((?:postgres|redis):[\w.-]+)"')


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(CI_WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key ``on`` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    return triggers if isinstance(triggers, dict) else {}


def _required_jobs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        job
        for job in (workflow.get("jobs") or {}).values()
        if isinstance(job, dict) and job.get("name") == REQUIRED_JOB_NAME
    ]


def proof_violations(workflow: dict[str, Any]) -> list[str]:
    """Every way the workflow fails to run each proof as a merge-blocking step."""
    jobs = _required_jobs(workflow)
    if len(jobs) != 1:
        return [f"expected exactly one job named {REQUIRED_JOB_NAME}, found {len(jobs)}"]
    job = jobs[0]
    violations = [f"job sets {key}" for key in sorted(FORBIDDEN_JOB_KEYS.intersection(job))]
    timeout = job.get("timeout-minutes")
    if not isinstance(timeout, int) or timeout < MINIMUM_JOB_TIMEOUT_MINUTES:
        violations.append(f"job timeout-minutes is {timeout!r}")
    working_directory = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
    if working_directory != "apps/api":
        violations.append(f"job working directory is {working_directory!r}")

    triggers = _triggers(workflow)
    for event in ("pull_request", "push"):
        branches = (triggers.get(event) or {}).get("branches") or []
        if "main" not in branches:
            violations.append(f"workflow does not run on {event} to main")

    steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
    for script in REQUIRED_PROOFS:
        matching = [step for step in steps if script in str(step.get("run", ""))]
        if len(matching) != 1:
            violations.append(f"{script} runs in {len(matching)} steps")
            continue
        step = matching[0]
        extra_keys = sorted(set(step) - ALLOWED_STEP_KEYS)
        if extra_keys:
            violations.append(f"{script} step sets {', '.join(map(str, extra_keys))}")
        if str(step["run"]).strip() != f"python {script}":
            violations.append(f"{script} step does not run the script directly")
    return violations


def test_required_job_runs_every_real_worker_proof_unconditionally() -> None:
    assert proof_violations(_workflow()) == []


@pytest.mark.parametrize("script", REQUIRED_PROOFS)
def test_every_proof_script_exists(script: str) -> None:
    assert (REPO_ROOT / "apps" / "api" / script).is_file()


def test_harness_containers_reuse_the_images_the_job_already_pulled() -> None:
    (job,) = _required_jobs(_workflow())
    service_images = {service["image"] for service in job["services"].values()}
    harness_images = {
        image
        for script in SCRIPTS_DIR.glob("real_worker_*.py")
        for image in HARNESS_IMAGE.findall(script.read_text())
    }

    assert harness_images
    assert harness_images <= service_images


def _valid_workflow() -> dict[str, Any]:
    return {
        "on": {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}},
        "jobs": {
            "backend": {
                "name": REQUIRED_JOB_NAME,
                "timeout-minutes": 15,
                "defaults": {"run": {"working-directory": "apps/api"}},
                "steps": [
                    {"name": f"Proof {index}", "run": f"python {script}"}
                    for index, script in enumerate(REQUIRED_PROOFS)
                ],
            }
        },
    }


def _first_proof_step(workflow: dict[str, Any]) -> dict[str, Any]:
    step = workflow["jobs"]["backend"]["steps"][0]
    assert isinstance(step, dict)
    return step


def test_checker_accepts_a_minimal_valid_workflow() -> None:
    assert proof_violations(_valid_workflow()) == []


@pytest.mark.parametrize(
    "step_change",
    [
        {"if": False},
        {"if": "github.event_name == 'push'"},
        {"continue-on-error": True},
        {"shell": "echo {0}"},
        {"env": {"PATH": "/nonexistent"}},
        {"working-directory": "/tmp"},
        {"timeout-minutes": 0},
        {"run": f"python {REQUIRED_PROOFS[0]} || true"},
        {"run": f"python {REQUIRED_PROOFS[0]} || :"},
        {"run": f"set +e\npython {REQUIRED_PROOFS[0]}\n"},
        {"run": f"echo python {REQUIRED_PROOFS[0]}"},
    ],
)
def test_checker_rejects_a_skipped_or_masked_proof_step(step_change: dict[str, Any]) -> None:
    workflow = _valid_workflow()
    _first_proof_step(workflow).update(step_change)

    assert proof_violations(workflow)


@pytest.mark.parametrize(
    "job_change",
    [
        {"if": False},
        {"continue-on-error": True},
        {"needs": ["other"]},
        {"strategy": {"matrix": {"python": ["3.12"]}}},
        {"timeout-minutes": 0},
        {"defaults": {"run": {"working-directory": "apps/web"}}},
        {"name": "Backend (optional)"},
    ],
)
def test_checker_rejects_a_conditional_or_renamed_job(job_change: dict[str, Any]) -> None:
    workflow = _valid_workflow()
    workflow["jobs"]["backend"].update(job_change)

    assert proof_violations(workflow)


def test_checker_rejects_a_missing_duplicated_or_misplaced_proof() -> None:
    missing = _valid_workflow()
    del missing["jobs"]["backend"]["steps"][0]

    duplicated = _valid_workflow()
    duplicated["jobs"]["backend"]["steps"].append(copy.deepcopy(_first_proof_step(duplicated)))

    misplaced = _valid_workflow()
    moved = misplaced["jobs"]["backend"]["steps"].pop(0)
    misplaced["jobs"]["other"] = {"name": "Other", "steps": [moved]}

    second_job = _valid_workflow()
    second_job["jobs"]["shadow"] = copy.deepcopy(second_job["jobs"]["backend"])

    for workflow in (missing, duplicated, misplaced, second_job):
        assert proof_violations(workflow)


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_checker_rejects_a_workflow_that_no_longer_runs_for_main(event: str) -> None:
    workflow = _valid_workflow()
    workflow["on"][event] = {"branches": ["develop"]}

    assert proof_violations(workflow)


def test_checker_reads_job_keys_wherever_they_are_written() -> None:
    text = "\n".join(
        [
            "on:",
            "  pull_request: {branches: [main]}",
            "  push: {branches: [main]}",
            "jobs:",
            "  backend:",
            f"    name: {REQUIRED_JOB_NAME}",
            "    timeout-minutes: 15",
            "    defaults: {run: {working-directory: apps/api}}",
            "    steps:",
            *[
                line
                for script in REQUIRED_PROOFS
                for line in ("      - name: Proof", f"        run: python {script}")
            ],
            "    if: false",
        ]
    )
    first_key_if = text.replace("      - name: Proof", "      - if: false", 1).replace(
        "    if: false", ""
    )

    assert proof_violations(yaml.safe_load(text)) == ["job sets if"]
    assert any("step sets if" in item for item in proof_violations(yaml.safe_load(first_key_if)))
