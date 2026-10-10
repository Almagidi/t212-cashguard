"""Local setup paths must install and accept the Node.js major the web app requires."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SETUP_LAUNCHER = REPO_ROOT / "launcher" / "1. Setup (Run First).command"
QUICKSTART = REPO_ROOT / "infra" / "scripts" / "quickstart.sh"
LOCAL_SETUP_GUIDE = REPO_ROOT / "docs" / "local-setup.md"
BREW_NODE_FORMULA = re.compile(r"\bnode@(\d+)\b")


def _required_node_major() -> str:
    engines = json.loads((REPO_ROOT / "apps" / "web" / "package.json").read_text())["engines"]
    return str(engines["node"]).split(".")[0]


def test_version_files_agree_with_the_web_app_engine() -> None:
    required = _required_node_major()

    assert required.isdigit()
    for version_file in (".nvmrc", ".node-version"):
        assert (REPO_ROOT / version_file).read_text().strip().split(".")[0] == required


def test_setup_launcher_installs_only_the_required_node_major() -> None:
    required = _required_node_major()
    launcher = SETUP_LAUNCHER.read_text()

    assert set(BREW_NODE_FORMULA.findall(launcher)) == {required, "20"}
    assert "brew unlink node@20" in launcher
    assert BREW_NODE_FORMULA.findall(launcher).count("20") == 1
    assert f"brew install node@{required}" in launcher
    assert f"brew link node@{required} --force --overwrite" in launcher
    assert f"REQUIRED_NODE_MAJOR={required}" in launcher
    # Once to decide whether to install, once to verify the install took effect.
    assert launcher.count('[ "$(node_major)" = "$REQUIRED_NODE_MAJOR" ]') == 2
    assert '-ge "20"' not in launcher


def test_quickstart_rejects_a_different_node_major() -> None:
    required = _required_node_major()
    quickstart = QUICKSTART.read_text()

    assert f"REQUIRED_NODE_MAJOR={required}" in quickstart
    assert re.search(
        r'\[ "\$\{NODE_VERSION%%\.\*\}" = "\$REQUIRED_NODE_MAJOR" \] \|\| error ', quickstart
    )
    assert "Node.js 20+" not in quickstart


def _stub(directory: Path, name: str, output: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho {output}\n")
    path.chmod(0o755)


@pytest.mark.parametrize("active_version", ["v20.11.1", "v22.14.0", "v25.0.0"])
def test_quickstart_stops_at_the_version_check_for_another_major(
    tmp_path: Path, active_version: str
) -> None:
    required = _required_node_major()
    for name, output in (("docker", "ok"), ("python3", "3.12"), ("node", active_version)):
        _stub(tmp_path, name, output)

    result = subprocess.run(
        ["/bin/bash", str(QUICKSTART)],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert f"Node.js {required}.x is required, found {active_version[1:]}" in result.stdout
    assert "Docker:" not in result.stdout


def test_local_setup_guide_states_the_required_node_major() -> None:
    required = _required_node_major()
    node_rows = [
        line for line in LOCAL_SETUP_GUIDE.read_text().splitlines() if line.startswith("| Node.js")
    ]

    assert len(node_rows) == 1
    assert f"| {required}.x" in node_rows[0]


def test_local_setup_guide_states_the_required_npm_major() -> None:
    engines = json.loads((REPO_ROOT / "apps" / "web" / "package.json").read_text())["engines"]
    npm_rows = [
        line for line in LOCAL_SETUP_GUIDE.read_text().splitlines() if line.startswith("| npm")
    ]

    assert len(npm_rows) == 1
    assert f"| {engines['npm']} |" in npm_rows[0]
