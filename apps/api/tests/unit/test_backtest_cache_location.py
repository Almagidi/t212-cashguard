from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.backtest import data_fetcher
from app.backtest.cache_location import default_cache_dir, resolve_cache_dir
from app.backtest.data_fetcher import BacktestDataFetcher
from app.backtest.dataset_cache import ImmutableDatasetCache

HOME = Path("/home/trader")
ROWS = [
    {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000, "t": 1767623400000},
    {"o": 101, "h": 102, "l": 100, "c": 101.5, "v": 1200, "t": 1767623700000},
]


def test_default_dir_is_a_per_user_cache_not_tmp_on_every_platform() -> None:
    windows = default_cache_dir(
        platform="win32", env={"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, home=HOME
    )
    assert windows == Path("C:/Users/me/AppData/Local/CashGuard/backtest_cache")
    assert default_cache_dir(platform="win32", env={}, home=HOME) == (
        HOME / "AppData" / "Local" / "CashGuard" / "backtest_cache"
    )
    assert default_cache_dir(platform="darwin", env={}, home=HOME) == (
        HOME / "Library" / "Caches" / "CashGuard" / "backtest_cache"
    )
    assert default_cache_dir(platform="linux", env={}, home=HOME) == (
        HOME / ".cache" / "cashguard" / "backtest_cache"
    )
    assert default_cache_dir(platform="linux", env={"XDG_CACHE_HOME": "/xdg"}, home=HOME) == (
        Path("/xdg/cashguard/backtest_cache")
    )
    for platform in ("win32", "darwin", "linux"):
        assert "tmp" not in default_cache_dir(platform=platform, env={}, home=HOME).parts


def test_configured_directory_wins_and_blank_falls_back() -> None:
    assert resolve_cache_dir("/data/bt", platform="linux", env={}, home=HOME) == Path("/data/bt")
    assert resolve_cache_dir("  ", platform="linux", env={}, home=HOME) == default_cache_dir(
        platform="linux", env={}, home=HOME
    )
    assert resolve_cache_dir("~/bt", platform="linux", env={}, home=HOME).name == "bt"


def test_module_default_is_not_tmp() -> None:
    configured = data_fetcher.settings.BACKTEST_CACHE_DIR
    if not configured:
        assert "tmp" not in Path(data_fetcher.CACHE_DIR).parts
        assert Path(data_fetcher.CACHE_DIR).name == "backtest_cache"


def test_manifest_and_content_ids_do_not_depend_on_the_cache_location(tmp_path: Path) -> None:
    request = BacktestDataFetcher._request(
        "AAPL", date(2026, 1, 5), date(2026, 1, 5), 5, "minute", "single_symbol_request", None
    )
    handles = []
    for name in ("alpha", "deeply/nested/beta"):
        cache = ImmutableDatasetCache(tmp_path / name)
        handles.append(
            cache.publish(
                request,
                ROWS,
                retrieved_at=datetime(2026, 1, 6, 12, tzinfo=UTC),
                code_sha="a" * 40,
            )
        )
        assert cache.reference_path(request).is_relative_to(tmp_path / name)

    first, second = handles
    assert first.manifest_id == second.manifest_id
    assert first.content_id == second.content_id
    assert first.manifest == second.manifest
    # the cache roots appear nowhere inside the hashed manifest
    serialized = json.dumps(first.manifest)
    assert str(tmp_path) not in serialized
    assert "alpha" not in serialized
    # and a fresh cache at a third location can load by the same ids
    reload_cache = ImmutableDatasetCache(tmp_path / "alpha")
    assert reload_cache.load(request).manifest_id == second.manifest_id


@pytest.mark.asyncio
async def test_fetcher_uses_the_configured_directory_and_returns_location_free_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    class Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"status": "OK", "resultsCount": 2, "results": ROWS}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            del args

        async def get(self, *args: Any, **kwargs: Any) -> Response:
            del args, kwargs
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setenv("T212_CODE_SHA", "b" * 40)
    target = tmp_path / "not" / "yet" / "created"
    monkeypatch.setattr(data_fetcher, "CACHE_DIR", target)

    fetcher = BacktestDataFetcher("secret-test-key")
    await fetcher.fetch_bars("AAPL", date(2026, 1, 5), date(2026, 1, 5))

    assert any(target.rglob("*.json"))  # created on first publish, no import-time mkdir needed
    assert str(target) not in json.dumps(fetcher.manifest_for("AAPL"))
