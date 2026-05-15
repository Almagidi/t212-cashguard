from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import deps
from app.api.deps import get_broker
from app.broker.mock_adapter import MockBrokerAdapter
from app.core.config import settings
from app.core.security import encrypt_field
from app.db.models import BrokerConnection, User

RECORDED_TRADING212_CALLS: list[tuple[str, str, str]] = []


class RecordingTrading212Adapter:
    def __init__(self, api_key: str, api_secret: str, environment: str = "demo") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        RECORDED_TRADING212_CALLS.append((api_key, api_secret, environment))


@pytest.fixture(autouse=True)
def _reset_settings_and_recording_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    RECORDED_TRADING212_CALLS.clear()
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)


async def _user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"broker-equivalence-{uuid.uuid4()}@test.local",
        hashed_password="not-used",
        is_active=True,
        is_admin=True,
    )
    db.add(user)
    await db.commit()
    return user


async def _add_connection(
    db,
    user: User,
    *,
    environment: str,
    api_key: str,
    api_secret: str,
    is_active: bool = True,
) -> BrokerConnection:
    conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="trading212",
        environment=environment,
        api_key_encrypted=encrypt_field(api_key),
        api_secret_encrypted=encrypt_field(api_secret),
        is_active=is_active,
    )
    db.add(conn)
    await db.commit()
    return conn


@pytest.mark.asyncio
async def test_mock_mode_returns_mock_adapter_without_constructing_trading212(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    monkeypatch.setattr(settings, "APP_MODE", "mock")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, MockBrokerAdapter)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_demo_mode_prefers_active_stored_credentials_over_environment_fallback(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="demo",
        api_key="stored-demo-key",
        api_secret="stored-demo-secret",
    )
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "fallback-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "fallback-demo-secret")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, RecordingTrading212Adapter)
    assert broker.environment == "demo"
    assert RECORDED_TRADING212_CALLS == [("stored-demo-key", "stored-demo-secret", "demo")]


@pytest.mark.asyncio
async def test_demo_mode_uses_demo_environment_fallback_without_active_connection(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "fallback-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "fallback-demo-secret")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, RecordingTrading212Adapter)
    assert broker.environment == "demo"
    assert RECORDED_TRADING212_CALLS == [("fallback-demo-key", "fallback-demo-secret", "demo")]


@pytest.mark.asyncio
async def test_demo_mode_ignores_inactive_demo_connection_and_uses_demo_fallback(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="demo",
        api_key="inactive-demo-key",
        api_secret="inactive-demo-secret",
        is_active=False,
    )
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "fallback-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "fallback-demo-secret")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, RecordingTrading212Adapter)
    assert broker.environment == "demo"
    assert RECORDED_TRADING212_CALLS == [("fallback-demo-key", "fallback-demo-secret", "demo")]


@pytest.mark.asyncio
async def test_demo_mode_missing_demo_fallback_does_not_use_live_credentials(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    monkeypatch.setattr(settings, "T212_LIVE_API_KEY", "live-key-must-not-be-used")
    monkeypatch.setattr(settings, "T212_LIVE_API_SECRET", "live-secret-must-not-be-used")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 400
    assert "Demo credentials are not configured" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_live_mode_blocks_broker_dependency_when_live_trading_flag_is_false(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="live",
        api_key="stored-live-key",
        api_secret="stored-live-secret",
    )
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 403
    assert "LIVE_TRADING_ENABLED must be true" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_live_mode_constructs_live_adapter_only_when_live_trading_flag_is_true(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="live",
        api_key="stored-live-key",
        api_secret="stored-live-secret",
    )
    monkeypatch.setattr(settings, "APP_MODE", "live")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    # Trading212Adapter is monkeypatched here, so this proves dependency behaviour without a real adapter or network call.
    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, RecordingTrading212Adapter)
    assert broker.environment == "live"
    assert RECORDED_TRADING212_CALLS == [("stored-live-key", "stored-live-secret", "live")]


@pytest.mark.asyncio
async def test_paper_mode_rejects_real_broker_dependency_before_connection_lookup(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="demo",
        api_key="stored-demo-key",
        api_secret="stored-demo-secret",
    )
    monkeypatch.setattr(settings, "APP_MODE", "paper")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 403
    assert "APP_MODE=paper must not call real broker endpoints" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_unknown_app_mode_rejects_real_broker_dependency(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    monkeypatch.setattr(settings, "APP_MODE", "staging")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 403
    assert "APP_MODE is not recognized" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_demo_mode_ignores_active_live_connection_and_fails_without_demo_credentials(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    await _add_connection(
        db,
        user,
        environment="live",
        api_key="stored-live-key",
        api_secret="stored-live-secret",
    )
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 400
    assert "Demo credentials are not configured" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []


@pytest.mark.asyncio
async def test_stored_credential_decryption_failure_returns_reconnect_required(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    user = await _user(db)
    conn = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker="trading212",
        environment="demo",
        api_key_encrypted="invalid-token",
        api_secret_encrypted="invalid-token",
        is_active=True,
        last_test_ok=True,
    )
    db.add(conn)
    await db.commit()
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    with pytest.raises(HTTPException) as exc_info:
        await get_broker(current_user=user, db=db)

    assert exc_info.value.status_code == 409
    assert "Stored broker credentials could not be decrypted" in str(exc_info.value.detail)
    assert RECORDED_TRADING212_CALLS == []

    refreshed = (
        await db.execute(select(BrokerConnection).where(BrokerConnection.id == conn.id))
    ).scalar_one()
    assert refreshed.is_active is False
    assert refreshed.last_test_ok is False


@pytest.mark.asyncio
async def test_provider_helper_remains_unwired_from_get_broker(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.provider as provider
    import app.broker.trading212 as trading212

    user = await _user(db)
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "T212_DEMO_API_KEY", "fallback-demo-key")
    monkeypatch.setattr(settings, "T212_DEMO_API_SECRET", "fallback-demo-secret")
    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)

    def forbidden_provider_helper(*args: object, **kwargs: object) -> object:
        raise AssertionError("get_broker must remain unwired from the provider helper")

    monkeypatch.setattr(
        provider,
        "create_trading212_provider_adapter",
        forbidden_provider_helper,
    )

    broker = await get_broker(current_user=user, db=db)

    assert isinstance(broker, RecordingTrading212Adapter)
    assert RECORDED_TRADING212_CALLS == [("fallback-demo-key", "fallback-demo-secret", "demo")]
    assert "create_trading212_provider_adapter" not in inspect.getsource(deps.get_broker)
