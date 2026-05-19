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

    def all(self) -> Any:
        return self.value


class ExecuteResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> ScalarResult:
        return ScalarResult(self.value)


class FakeSession:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.added: list[Any] = []
        self.commits = 0
        self.flushes = 0
        self.executed = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> ExecuteResult:
        self.executed += 1
        if not self.results:
            raise AssertionError("unexpected execute call")
        return ExecuteResult(self.results.pop(0))

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


class RecordingBroker:
    entered: ClassVar[int] = 0
    exited: ClassVar[int] = 0
    positions_calls: ClassVar[int] = 0
    write_calls: ClassVar[list[str]] = []

    environment = "demo"

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self.positions = (
            [
                {
                    "ticker": "AAPL",
                    "quantity": "2",
                    "currentPrice": "150.00",
                    "overnightFee": "7.2",
                    "currency": "GBP",
                }
            ]
            if positions is None
            else positions
        )

    async def __aenter__(self) -> RecordingBroker:
        type(self).entered += 1
        return self

    async def __aexit__(self, *_args: Any) -> None:
        type(self).exited += 1

    async def get_positions(self) -> list[dict[str, Any]]:
        type(self).positions_calls += 1
        return self.positions

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"broker write method must not be called: {name}")
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _reset_task_state(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingBroker.entered = 0
    RecordingBroker.exited = 0
    RecordingBroker.positions_calls = 0
    RecordingBroker.write_calls.clear()
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(tasks, "_LOOP", None)


def _active_conn(*, environment: str = "live") -> Any:
    return type(
        "FakeConnection",
        (),
        {
            "id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "environment": environment,
            "api_key_encrypted": "encrypted-active-key",
            "api_secret_encrypted": "encrypted-active-secret",
        },
    )()


def _strategy(*, ticker: str = "AAPL") -> Any:
    return type(
        "FakeStrategy",
        (),
        {
            "id": uuid.uuid4(),
            "allowed_tickers": [ticker],
            "is_enabled": True,
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


def _install_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    def decrypt(value: str) -> str:
        return {
            "encrypted-active-key": "decrypted-active-key",
            "encrypted-active-secret": "decrypted-active-secret",
        }[value]

    monkeypatch.setattr("app.core.security.decrypt_field", decrypt)


def test_provider_not_called_in_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[])
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr(settings, "APP_MODE", "mock")

    monkeypatch.setattr("app.broker.mock_adapter.MockBrokerAdapter", lambda: RecordingBroker([]))
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.track_cfd_funding.run() == {"recorded": 0}

    assert provider_calls == []
    assert RecordingBroker.positions_calls == 1
    assert fake_db.added == []
    assert summaries == [{"recorded": 0}]


def test_provider_not_called_when_no_active_connection_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[None])
    _install_session(monkeypatch, fake_db, summaries)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.track_cfd_funding.run() == {"recorded": 0}

    assert provider_calls == []
    assert RecordingBroker.positions_calls == 0
    assert fake_db.added == []
    assert summaries == [{"recorded": 0}]


def test_provider_not_called_when_decryption_fails_and_reconnect_required_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _active_conn()
    provider_calls: list[Any] = []
    reconnect_calls: list[tuple[FakeSession, Any, str, str]] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[conn])
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

    assert tasks.track_cfd_funding.run() == {"recorded": 0, "skipped": "credential_error"}

    assert provider_calls == []
    assert reconnect_calls == [(fake_db, conn, "cannot decrypt", "worker:track_cfd_funding")]
    assert RecordingBroker.positions_calls == 0
    assert fake_db.added == []


def test_provider_not_called_before_live_disabled_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[Any] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[_active_conn(environment="live")])
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    assert tasks.track_cfd_funding.run() == {
        "recorded": 0,
        "skipped": "live_flag_disabled",
        "reason": "worker cfd funding blocked: LIVE_TRADING_ENABLED must be true before live broker calls.",
    }

    assert provider_calls == []
    assert RecordingBroker.positions_calls == 0
    assert fake_db.added == []


def test_provider_created_broker_reads_positions_and_persists_funding_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _active_conn(environment="live")
    provider_calls: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[conn, [_strategy(ticker="AAPL")]])
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)

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

    monkeypatch.setattr(
        "app.broker.provider.create_trading212_provider_adapter",
        recording_provider,
    )

    assert tasks.track_cfd_funding.run() == {"recorded": 1}

    assert provider_calls == [
        {
            "request": BrokerProviderRequest(
                broker_id="trading212",
                environment="live",
                purpose="worker_cfd_funding",
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
    assert RecordingBroker.positions_calls == 1
    assert RecordingBroker.write_calls == []
    records = [added for added in fake_db.added if added.__class__.__name__ == "CFDFundingCost"]
    assert len(records) == 1
    record = records[0]
    assert record.ticker == "AAPL"
    assert record.strategy_id is not None
    assert record.quantity == Decimal("2")
    assert record.price_at_close == Decimal("150.00")
    assert record.notional == Decimal("300.00")
    assert record.annual_rate_pct == Decimal("7.2")
    assert record.daily_charge == Decimal("0.060000")
    assert record.currency == "GBP"
    assert fake_db.flushes == 1
    assert summaries == [{"recorded": 1}]


def test_provider_validation_error_uses_worker_summary_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries: list[dict[str, Any]] = []
    fake_db = FakeSession(results=[_active_conn(environment="live")])
    _install_session(monkeypatch, fake_db, summaries)
    _install_decrypt(monkeypatch)

    def fail_provider(*_args: Any, **_kwargs: Any) -> RecordingBroker:
        raise BrokerProviderValidationError("provider refused construction")

    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", fail_provider)

    assert tasks.track_cfd_funding.run() == {
        "recorded": 0,
        "skipped": "provider_validation_error",
        "reason": "provider refused construction",
    }

    assert RecordingBroker.positions_calls == 0
    assert fake_db.added == []
