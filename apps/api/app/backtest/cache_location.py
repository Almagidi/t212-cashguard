"""
Where the immutable backtest dataset cache lives.

The location is a deployment detail and is deliberately *not* part of any dataset
identity: request ids, content ids and manifest ids are hashes of the request and the
data only, so the same dataset has the same ids wherever the cache directory is.

Order of precedence: the ``BACKTEST_CACHE_DIR`` setting, then a platform-appropriate
per-user cache directory (never a world-writable temp directory such as ``/tmp``).  Only if no home directory
can be determined at all does it fall back to a ``cashguard-backtest-cache`` directory
under the system temp dir; resolution never raises and is never done at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

APP_DIR_NAME = "CashGuard"
CACHE_SUBDIR = "backtest_cache"


def fallback_cache_dir() -> Path:
    """Used only when no home directory can be determined (e.g. a scrubbed environment)."""
    return Path(tempfile.gettempdir()) / "cashguard-backtest-cache"


def default_cache_dir(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if home is None:
        try:
            home = Path.home()
        except (RuntimeError, KeyError, OSError):
            return fallback_cache_dir()
    if platform.startswith("win"):
        base = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return base / APP_DIR_NAME / CACHE_SUBDIR
    if platform == "darwin":
        return home / "Library" / "Caches" / APP_DIR_NAME / CACHE_SUBDIR
    base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
    return base / APP_DIR_NAME.lower() / CACHE_SUBDIR


def resolve_cache_dir(
    configured: str | None = None,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    if configured and configured.strip():
        return Path(configured.strip()).expanduser()
    return default_cache_dir(platform=platform, env=env, home=home)
