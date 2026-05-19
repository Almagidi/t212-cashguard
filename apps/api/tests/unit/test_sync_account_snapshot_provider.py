from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.broker.provider import (
    BrokerProviderCredentials,
    BrokerProviderRequest,
    BrokerProviderValidationError,
)
from app.core.config import settings
from app.core.security import CredentialDecryptionError
from app.workers import tasks


class ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class FakeSession:
    def __init__(self, conn: Any | None) -> None:
        self.conn = conn
        self.added: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> ScalarResult:
        return ScalarResult(self.conn)

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


class RecordingBroker:
    entered: ClassVar[int] = 0
    exited: ClassVar[int] = 0
    summary_calls: ClassVar[int] = 0
    write_calls: ClassVar[list[str]] = []

    environment = "demo"

    async def __aenter__(self) -> RecordingBroker:
        type(self).entered += 1
        return self

    async def __aexit__(self, *_args: Any) -> None:
        type(self).exited += 1

    async def get_account_summary(self) -> dict[str, Any]:
        type(self).summary_calls += 1
        return {
            "total": "1234.56",
            "cash": "1000.00",
            "free": "900.00",
            "invested": "234.56",
            "result": "12.34",
        }

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"broker write method must not be called: {name}")
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _reset_task_state(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingBroker.entered = 0
    RecordingBroker.exited = 0
    RecordingBroker.summary_calls = 0
    RecordingBroker.write_calls.clear()
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(tasks, "_LOOP", None)


def _active_conn(*, environment: str = "live", account_currency: str | None = "GBP") -> Any:
    return type(
        "FakeConnection",
        (),
        {
            "id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "environment": environment,
            "api_key_encrypted": "encrypted-active-key",
            "api_secret_encrypted": "encrypted-active-secret",
            "account_currency": account_currency,
        },
    )()


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: FakeSession,
    summaries: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: fake_db)

    async def complete_task(
        _db: FakeSession, _task_name: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        summaries.append(summary)
        await _db.commit()
        return summary

    monkeypatch.setattr(tasks, "_complete_task", complete_task)


def test_provider_not_called_in_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=None)
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr(settings, "APP_MODE", "mock")

    class MockBroker:
        async def __aenter__(self) -> MockBroker:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get_account_summary(self) -> dict[str, Any]:
            return {"total": 1}

    monkeypatch.setattr("app.broker.mock_adapter.MockBrokerAdapter", MockBroker)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.sync_account_snapshot.run() == {
        "synced": True,
        "mode": "mock",
        "persisted": False,
    }

    assert provider_calls == []
    assert fake_db.added == []
    assert summaries == [{"synced": True, "mode": "mock", "persisted": False}]


def test_provider_not_called_when_no_active_connection_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=None)
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.sync_account_snapshot.run() == {"synced": False, "skipped": "no_connection"}

    assert provider_calls == []
    assert fake_db.added == []
    assert summaries == [{"synced": False, "skipped": "no_connection"}]


def test_provider_not_called_when_credential_decryption_fails_and_reconnect_required_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _active_conn()
    provider_calls: list[Any] = []
    reconnect_calls: list[tuple[FakeSession, Any, str, str]] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=conn)
    _install_session(monkeypatch, fake_db, summaries)

    def fail_decrypt(_value: str) -> str:
        raise CredentialDecryptionError("cannot decrypt")

    async def mark_reconnect(db: FakeSession, marked_conn: Any, reason: str, *, actor: str) -> None:
        reconnect_calls.append((db, marked_conn, reason, actor))

    monkeypatch.setattr("app.core.security.decrypt_field", fail_decrypt)
    monkeypatch.setattr(tasks, "_mark_connection_reconnect_required", mark_reconnect)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.sync_account_snapshot.run() == {"synced": False, "skipped": "credential_error"}

    assert provider_calls == []
    assert reconnect_calls == [(fake_db, conn, "cannot decrypt", "worker:sync_account_snapshot")]
    assert fake_db.added == []


def test_provider_not_called_before_live_disabled_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=_active_conn(environment="live"))
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.sync_account_snapshot.run() == {
        "synced": False,
        "skipped": "live_flag_disabled",
        "reason": "worker account sync blocked: LIVE_TRADING_ENABLED must be true before live broker calls.",
    }

    assert provider_calls == []
    assert fake_db.added == []


def test_provider_created_broker_reads_summary_and_persists_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _active_conn(environment="live", account_currency="GBP")
    provider_calls: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=conn)
    _install_session(monkeypatch, fake_db, summaries)

    def decrypt(value: str) -> str:
        return {
            "encrypted-active-key": "decrypted-active-key",
            "encrypted-active-secret": "decrypted-active-secret",
        }[value]

    def recording_provider(
        request: BrokerProviderRequest,
        credentials: BrokerProviderCredentials,
        *,
        app_mode: str,
        live_trading_enabled: bool,
    ) -> RecordingBroker:
        provider_calls.append(
            {
                "request": request,
                "credentials": credentials,
                "app_mode": app_mode,
                "live_trading_enabled": live_trading_enabled,
            }
        )
        return RecordingBroker()

    monkeypatch.setattr("app.core.security.decrypt_field", decrypt)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        recording_provider,
    )

    assert tasks.sync_account_snapshot.run() == {"synced": True}

    assert provider_calls == [
        {
            "request": BrokerProviderRequest(
                broker_id="trading212",
                environment="live",
                purpose="worker_account_sync",
                user_id=conn.user_id,
            ),
            "credentials": BrokerProviderCredentials(
                api_key="decrypted-active-key",
                api_secret="decrypted-active-secret",
            ),
            "app_mode": "live",
            "live_trading_enabled": True,
        }
    ]
    assert RecordingBroker.entered == 1
    assert RecordingBroker.exited == 1
    assert RecordingBroker.summary_calls == 1
    assert RecordingBroker.write_calls == []
    snapshots = [
        added for added in fake_db.added if added.__class__.__name__ == "BrokerAccountSnapshot"
    ]
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.connection_id == conn.id
    assert snapshot.total_value == Decimal("1234.56")
    assert snapshot.cash == Decimal("1000.00")
    assert snapshot.free_funds == Decimal("900.00")
    assert snapshot.invested == Decimal("234.56")
    assert snapshot.result == Decimal("12.34")
    assert snapshot.currency == "GBP"
    assert snapshot.raw == {
        "total": "1234.56",
        "cash": "1000.00",
        "free": "900.00",
        "invested": "234.56",
        "result": "12.34",
    }
    assert summaries == [{"synced": True}]


def test_provider_validation_error_uses_worker_summary_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _active_conn(environment="live")
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(conn=conn)
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr("app.core.security.decrypt_field", lambda value: f"decrypted-{value}")

    def fail_provider(*_args: Any, **_kwargs: Any) -> RecordingBroker:
        raise BrokerProviderValidationError("provider refused construction")

    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", fail_provider)

    assert tasks.sync_account_snapshot.run() == {
        "synced": False,
        "skipped": "provider_validation_error",
        "reason": "provider refused construction",
    }

    assert RecordingBroker.summary_calls == 0
    assert fake_db.added == []
