"""Settings loading contract.

pydantic-settings loads SECRET_KEY, APP_MODE and LIVE_TRADING_ENABLED. These tests pin the
source precedence and parsing behaviour the application relies on (init > OS environment >
.env file > field default), so a pydantic-settings upgrade that changes it fails loudly.
They use an explicit temporary env file and a scrubbed environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings

if TYPE_CHECKING:
    from pathlib import Path

_SCRUBBED_KEYS = (
    "APP_MODE",
    "LIVE_TRADING_ENABLED",
    "CASH_ONLY_MODE",
    "SECRET_KEY",
    "MASTER_KEY",
    "MARKET_DATA_PROVIDER",
)


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SCRUBBED_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)


def _write_env(tmp_path: Path, body: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_dotenv_values_are_loaded(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "APP_MODE=paper\nLIVE_TRADING_ENABLED=false\n")

    loaded = Settings(_env_file=env_file)

    assert loaded.APP_MODE == "paper"
    assert loaded.LIVE_TRADING_ENABLED is False


def test_os_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = _write_env(tmp_path, "APP_MODE=paper\n")
    monkeypatch.setenv("APP_MODE", "demo")

    assert Settings(_env_file=env_file).APP_MODE == "demo"


def test_init_arguments_override_environment_and_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = _write_env(tmp_path, "APP_MODE=paper\n")
    monkeypatch.setenv("APP_MODE", "demo")

    assert Settings(_env_file=env_file, APP_MODE="mock").APP_MODE == "mock"


def test_names_are_case_insensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = _write_env(tmp_path, "app_mode=paper\n")
    assert Settings(_env_file=env_file).APP_MODE == "paper"

    monkeypatch.setenv("app_mode", "demo")
    assert Settings(_env_file=env_file).APP_MODE == "demo"


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "SOME_UNKNOWN_KEY=1\nAPP_MODE=paper\n")

    assert Settings(_env_file=env_file).APP_MODE == "paper"


def test_invalid_app_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "yolo")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("no", False)],
)
def test_live_trading_flag_parses_boolean_strings(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", raw)

    assert Settings(_env_file=None).LIVE_TRADING_ENABLED is expected


def test_unparseable_live_trading_flag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "maybe")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secret_key_from_dotenv_replaces_the_placeholder_default(tmp_path: Path) -> None:
    placeholder = Settings(_env_file=None).SECRET_KEY
    env_file = _write_env(tmp_path, "SECRET_KEY=unit-test-dotenv-key\n")

    loaded = Settings(_env_file=env_file)

    assert loaded.SECRET_KEY == "unit-test-dotenv-key"
    assert placeholder != loaded.SECRET_KEY


def test_class_level_env_file_is_honoured(tmp_path: Path) -> None:
    # The application loads through model_config["env_file"], not an init argument.
    env_file = _write_env(tmp_path, "APP_MODE=paper\nSECRET_KEY=unit-test-dotenv-key\n")

    class ScopedSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=str(env_file),
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
        )

    loaded = ScopedSettings()

    assert loaded.APP_MODE == "paper"
    assert loaded.SECRET_KEY == "unit-test-dotenv-key"


def test_defaults_apply_when_nothing_is_configured() -> None:
    loaded = Settings(_env_file=None)

    assert loaded.APP_MODE == "mock"
    assert loaded.LIVE_TRADING_ENABLED is False
