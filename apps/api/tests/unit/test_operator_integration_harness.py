"""The operator browser fixture must exercise the admitted PostgreSQL lifecycle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_operator_integration.py"


def load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("operator_harness", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:////tmp/old.db",
        "postgresql+asyncpg://user:pass@external.invalid/db",
        "postgresql+asyncpg://user:pass@127.0.0.1/db?host=external.invalid",
        "",
    ],
)
def test_only_explicit_loopback_postgres_is_accepted(url: str) -> None:
    with pytest.raises(ValueError):
        load_harness().postgres_url(url)


def test_fixture_never_uses_existing_database_or_inherits_permissions() -> None:
    module = load_harness()
    existing = "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/existing"
    child = module.fixture_environment(
        {
            "DATABASE_URL": existing,
            "APP_MODE": "live",
            "LIVE_TRADING_ENABLED": "true",
            "SENTRY_DSN": "synthetic-unusable-value",
            "NEXT_PUBLIC_SENTRY_DSN": "synthetic-unusable-value",
            "T212_DEMO_API_KEY": "synthetic-demo-key",
            "T212_DEMO_API_SECRET": "synthetic-demo-secret",
            "T212_LIVE_API_KEY": "synthetic-live-key",
            "T212_LIVE_API_SECRET": "synthetic-live-secret",
            "t212_live_api_key": "synthetic-lowercase-key",
            "ALPACA_API_KEY": "synthetic-other-key",
            "SMTP_PASSWORD": "synthetic-notification-password",
            "TELEGRAM_BOT_TOKEN": "synthetic-notification-token",
            "ADMIN_EMAIL": "unrelated@example.invalid",
            "sentry_dsn": "synthetic-lowercase-value",
            "CELERY_BROKER_URL": "https://external.invalid",
            "API_URL": "https://external.invalid",
        },
        "cashguard_operator_" + "a" * 32,
    )
    assert child["DATABASE_URL"] != existing
    assert child["DATABASE_URL"].endswith("/cashguard_operator_" + "a" * 32)
    assert child["APP_MODE"] == "mock"
    assert child["LIVE_TRADING_ENABLED"] == "false"
    assert child["SENTRY_DSN"] == child["NEXT_PUBLIC_SENTRY_DSN"] == ""
    for name in (
        "T212_DEMO_API_KEY",
        "T212_DEMO_API_SECRET",
        "T212_LIVE_API_KEY",
        "T212_LIVE_API_SECRET",
        "ALPACA_API_KEY",
        "SMTP_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert child[name] == ""
    assert "t212_live_api_key" not in child
    assert child["ADMIN_EMAIL"] == "admin@localhost"
    assert child["CASHGUARD_ENV_FILE"] == ""
    assert "sentry_dsn" not in child
    assert child["CELERY_BROKER_URL"] == "memory://"
    assert child["API_URL"] == "http://127.0.0.1:8001"


def test_readiness_rejects_blocked_api(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_harness()
    seen = []

    class Process:
        def poll(self) -> None:
            return None

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def get(self, url: str) -> Any:
            seen.append(url)
            return module.httpx.Response(503)

    monkeypatch.setattr(module.httpx, "Client", Client)
    with pytest.raises(RuntimeError, match="refused startup"):
        module.wait_for_ready(Process())
    assert seen == ["http://127.0.0.1:8001/v1/health/ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_oid", [42, 99, None])
async def test_cleanup_is_owned_and_runs_on_test_failure(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_oid: int | None,
) -> None:
    module = load_harness()
    connection = AsyncMock()
    connection.scalar.side_effect = [42, cleanup_oid]
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    engine.dispose = AsyncMock()
    monkeypatch.setattr(module, "create_async_engine", MagicMock(return_value=engine))
    name = "cashguard_operator_" + "b" * 32
    expected = "synthetic test failure" if cleanup_oid == 42 else "identity changed"
    with pytest.raises(RuntimeError, match=expected):
        async with module.disposable_database(
            module.postgres_url("postgresql+asyncpg://test:test@127.0.0.1/existing"),
            name,
        ):
            raise RuntimeError("synthetic test failure")
    commands = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert commands[0] == f'CREATE DATABASE "{name}" TEMPLATE template0'
    assert commands[1:] == ([f'DROP DATABASE "{name}"'] if cleanup_oid == 42 else [])
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["create", "identity"])
async def test_uncertain_creation_never_drops_existing_database(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_harness()
    connection = AsyncMock()
    if failure_stage == "create":
        connection.execute.side_effect = RuntimeError("CREATE outcome uncertain")
    else:
        connection.scalar.side_effect = RuntimeError("identity outcome uncertain")
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    engine.dispose = AsyncMock()
    monkeypatch.setattr(module, "create_async_engine", MagicMock(return_value=engine))
    name = "cashguard_operator_" + "c" * 32
    with pytest.raises(RuntimeError, match="outcome uncertain"):
        async with module.disposable_database(
            module.postgres_url("postgresql+asyncpg://test:test@127.0.0.1/existing"),
            name,
        ):
            pytest.fail("must not reuse an existing database")
    commands = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert len(commands) == 1
    assert commands[0].startswith("CREATE DATABASE")
    assert name in capsys.readouterr().out
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "filename", [".env", ".env.local", ".env.development", ".env.development.local"]
)
def test_framework_dotenv_is_refused_without_reading_or_modifying(
    tmp_path: Path,
    filename: str,
) -> None:
    module = load_harness()
    module.require_clean_web_env(tmp_path)
    local = tmp_path / filename
    local.write_text("SENTRY_DSN=synthetic-only-do-not-read\n")
    before = local.read_bytes()
    with pytest.raises(ValueError, match="without runtime dotenv"):
        module.require_clean_web_env(tmp_path)
    assert local.read_bytes() == before


def test_cleanup_signals_group_even_when_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_harness()
    signals = []

    class Process:
        pid = 123456789

        def poll(self) -> int:
            return 0

        def wait(self, timeout: int) -> int:
            return 0

    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    module.stop_owned(Process())
    assert signals == [(Process.pid, module.signal.SIGINT), (Process.pid, module.signal.SIGKILL)]


def test_owned_command_failure_always_finalizes_its_group(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_harness()
    calls = []
    stopped: list[Any] = []

    class Process:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        def wait(self) -> int:
            return 7

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "stop_owned", stopped.append)
    with pytest.raises(module.subprocess.CalledProcessError):
        module.run_owned(["synthetic-command"], SCRIPT.parent, {})
    assert calls[0]["start_new_session"] is True
    assert len(stopped) == 1


def test_fixture_credentials_are_ephemeral_independent_and_never_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import re

    module = load_harness()
    source = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1/existing",
        "SECRET_KEY": "ignored-inherited-value",
        "MASTER_KEY": "ignored-inherited-value",
        "ADMIN_PASSWORD": "ignored-inherited-value",
    }
    first = module.fixture_environment(source, "cashguard_operator_" + "d" * 32)
    second = module.fixture_environment(source, "cashguard_operator_" + "e" * 32)
    keys_are_random_hex = all(
        re.fullmatch(r"[0-9a-f]{64}", environment[key]) is not None
        for environment in (first, second)
        for key in ("SECRET_KEY", "MASTER_KEY")
    )
    assert keys_are_random_hex, "Fixture signing and encryption keys must be fresh 32-byte tokens."
    passwords_are_strong = all(
        len(environment["ADMIN_PASSWORD"]) >= 32 for environment in (first, second)
    )
    assert passwords_are_strong, "Fixture passwords must be generated at runtime."
    independently_generated = (
        len(
            {
                environment[key]
                for environment in (first, second)
                for key in ("SECRET_KEY", "MASTER_KEY", "ADMIN_PASSWORD")
            }
        )
        == 6
    )
    assert independently_generated, "Credentials must differ by purpose and fixture run."
    browser_matches_api = all(
        environment["E2E_ADMIN_PASSWORD"] == environment["ADMIN_PASSWORD"]
        for environment in (first, second)
    )
    assert browser_matches_api, "Only the same isolated run shares its API/browser login."
    assert capsys.readouterr().out == "", "Credential material must never be printed."
