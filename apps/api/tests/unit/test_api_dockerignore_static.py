from __future__ import annotations

import fnmatch
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = API_ROOT / ".dockerignore"

# Paths a developer may have locally that must never be copied into the API image.
MUST_BE_EXCLUDED = (
    ".env",
    ".env.local",
    ".env.production",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
    "integration_test.db",
    "scripts/.env",
    "scripts/.env.local",
    "app/services/__pycache__/x.cpython-312.pyc",
    "app/stale.pyc",
    "tests/.pytest_cache",
    "tests/data/local.db",
)
# Paths the image needs at run time or for migrations.
MUST_BE_INCLUDED = (
    "app",
    "app/core/config.py",
    "app/db/migrations/env.py",
    "alembic.ini",
    "requirements.txt",
    "scripts",
    "Dockerfile",
    ".coveragerc",
)


def _patterns() -> list[str]:
    lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _matches(path: str, pattern: str) -> bool:
    """Docker semantics for the forms used here: `**/x` matches at any depth, `x` at the root."""
    parts = path.split("/")
    if pattern.startswith("**/"):
        tail = pattern.removeprefix("**/")
        return any(fnmatch.fnmatchcase(part, tail) for part in parts)
    return fnmatch.fnmatchcase(parts[0], pattern)


def _is_excluded(path: str) -> bool:
    excluded = False
    for pattern in _patterns():
        if _matches(path, pattern.lstrip("!")):
            excluded = not pattern.startswith("!")
    return excluded


def test_api_build_context_excludes_local_secrets_environments_and_caches() -> None:
    assert [path for path in MUST_BE_EXCLUDED if not _is_excluded(path)] == []


def test_api_build_context_keeps_what_the_image_needs() -> None:
    assert [path for path in MUST_BE_INCLUDED if _is_excluded(path)] == []
