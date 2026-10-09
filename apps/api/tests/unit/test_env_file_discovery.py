from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.core import config

API_ROOT = Path(__file__).resolve().parents[2]


def _fake_config_module(project_root: Path) -> Path:
    """Path of a config module laid out as in the repository, under project_root."""
    module = project_root / "apps" / "api" / "app" / "core" / "config.py"
    module.parent.mkdir(parents=True)
    module.touch()
    return module


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A directory above the project holding a stray .env, and a project root marked by .git."""
    outside = tmp_path / "home"
    project_root = outside / "projects" / "cashguard"
    project_root.mkdir(parents=True)
    (project_root / ".git").mkdir()
    (outside / ".env").write_text("APP_MODE=paper\n", encoding="utf-8")
    (outside / "projects" / ".env").write_text("APP_MODE=demo\n", encoding="utf-8")
    return outside, project_root


def test_env_file_above_the_project_root_is_never_used(tmp_path: Path) -> None:
    _, project_root = _checkout(tmp_path)

    assert config._find_env_file(_fake_config_module(project_root)) is None


def test_project_root_env_file_is_used(tmp_path: Path) -> None:
    _, project_root = _checkout(tmp_path)
    expected = project_root / ".env"
    expected.write_text("APP_MODE=mock\n", encoding="utf-8")

    assert config._find_env_file(_fake_config_module(project_root)) == expected


def test_nearest_env_file_inside_the_project_wins(tmp_path: Path) -> None:
    _, project_root = _checkout(tmp_path)
    (project_root / ".env").write_text("APP_MODE=mock\n", encoding="utf-8")
    module = _fake_config_module(project_root)
    nearest = project_root / "apps" / "api" / ".env"
    nearest.write_text("APP_MODE=mock\n", encoding="utf-8")

    assert config._find_env_file(module) == nearest


def test_compose_file_also_marks_the_project_root(tmp_path: Path) -> None:
    outside = tmp_path / "home"
    project_root = outside / "cashguard"
    project_root.mkdir(parents=True)
    (project_root / "docker-compose.yml").touch()
    (outside / ".env").write_text("APP_MODE=paper\n", encoding="utf-8")
    expected = project_root / ".env"
    expected.write_text("APP_MODE=mock\n", encoding="utf-8")

    assert config._find_env_file(_fake_config_module(project_root)) == expected


def test_search_stops_at_the_api_root_when_no_project_marker_exists(tmp_path: Path) -> None:
    # The container image has no .git or Compose file: /app/app/core/config.py, API root /app.
    image_root = tmp_path / "image"
    module = image_root / "app" / "app" / "core" / "config.py"
    module.parent.mkdir(parents=True)
    module.touch()
    (image_root / ".env").write_text("APP_MODE=paper\n", encoding="utf-8")

    assert config._find_env_file(module) is None

    inside = image_root / "app" / ".env"
    inside.write_text("APP_MODE=mock\n", encoding="utf-8")

    assert config._find_env_file(module) == inside


def test_override_variable_selects_one_file_or_disables_loading(tmp_path: Path) -> None:
    _, project_root = _checkout(tmp_path)
    (project_root / ".env").write_text("APP_MODE=mock\n", encoding="utf-8")
    module = _fake_config_module(project_root)
    chosen = tmp_path / "chosen.env"
    chosen.write_text("APP_MODE=mock\n", encoding="utf-8")

    assert config._resolve_env_file(module, {}) == project_root / ".env"
    assert config._resolve_env_file(module, {config.ENV_FILE_VARIABLE: str(chosen)}) == chosen
    assert config._resolve_env_file(module, {config.ENV_FILE_VARIABLE: ""}) is None
    assert config._resolve_env_file(module, {config.ENV_FILE_VARIABLE: "   "}) is None


def test_override_pointing_at_a_missing_file_is_an_error(tmp_path: Path) -> None:
    _, project_root = _checkout(tmp_path)
    module = _fake_config_module(project_root)

    with pytest.raises(FileNotFoundError, match=config.ENV_FILE_VARIABLE):
        config._resolve_env_file(module, {config.ENV_FILE_VARIABLE: str(tmp_path / "typo.env")})


def test_git_worktree_marker_file_also_bounds_the_search(tmp_path: Path) -> None:
    # A linked worktree has a .git file, not a directory, and does not inherit an outer .env.
    outer = tmp_path / "outer-checkout"
    worktree = outer / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (outer / ".git").mkdir()
    (outer / ".env").write_text("APP_MODE=paper\n", encoding="utf-8")
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    assert config._find_env_file(_fake_config_module(worktree)) is None


def test_the_test_suite_itself_loads_no_env_file() -> None:
    assert config._ENV_FILE_PATH is None
    assert config.Settings.model_config.get("env_file") is None


@pytest.mark.parametrize(
    ("override", "expected_mode"),
    [("", "mock"), ("FILE", "paper")],
)
def test_settings_in_a_fresh_process_follow_the_override(
    tmp_path: Path, override: str, expected_mode: str
) -> None:
    chosen = tmp_path / "chosen.env"
    chosen.write_text("APP_MODE=paper\n", encoding="utf-8")
    # A stray file in the working directory must not be picked up either.
    (tmp_path / ".env").write_text("APP_MODE=demo\n", encoding="utf-8")
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(API_ROOT),
        config.ENV_FILE_VARIABLE: str(chosen) if override == "FILE" else override,
    }

    result = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.APP_MODE)"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
    )

    assert result.stdout.strip() == expected_mode
