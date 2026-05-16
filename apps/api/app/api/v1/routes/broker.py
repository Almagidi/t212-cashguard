"""Broker connection management routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.api.deps import get_broker, get_current_admin, get_current_user
from app.broker.protocols import ReconciliationHistoryBrokerProtocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
from app.api.schemas import (
    BrokerConnectRequest,
    BrokerStatusOut,
    BrokerTestResult,
    DemoReconciliationSchedulerRunResult,
    DemoReconciliationSchedulerStatus,
    DemoReconciliationWorkerRunSummary,
    DemoReconciliationWorkerStatus,
)
from app.core.config import settings
from app.core.security import CredentialDecryptionError, decrypt_field, encrypt_field
from app.db.models import AuditLog, BrokerConnection, User
from app.db.session import get_db
from app.services.broker_connection_recovery import (
    BROKER_RECOVERY_HINT,
    mark_broker_connection_reconnect_required,
)
from app.services.demo_reconciliation_scheduler import (
    DemoReconciliationScheduler,
    build_scheduler_status,
)
from app.services.demo_reconciliation_worker import DemoReconciliationWorker
from app.services.safety_policy import SafetyPolicyViolation, require_broker_environment

router = APIRouter(prefix="/broker/trading212", tags=["broker"])

MOCK_CREDENTIALS_IGNORED_HINT = (
    "APP_MODE=mock is synthetic: submitted broker credentials are ignored and not stored. "
    "Switch to APP_MODE=demo or APP_MODE=live to test Trading 212 credentials."
)
DEMO_CREDENTIALS_MISSING_HINT = (
    "APP_MODE=demo requires Trading 212 demo credentials or an active demo broker connection. "
    "Live credentials are ignored in demo mode."
)


class _StatusBroker:
    environment = "demo"

    async def get_historical_orders(
        self,
        cursor: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        raise RuntimeError("Status broker does not support historical order reconciliation.")


async def _audit(
    db: AsyncSession, action: str, user_id: object | None = None, payload: dict | None = None
) -> None:
    db.add(
        AuditLog(
            action=action,
            entity_type="broker_connection",
            actor=str(user_id) if user_id else "system",
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()


def _serialize_status(
    conn: BrokerConnection,
    *,
    credential_state: str,
    recovery_hint: str | None = None,
) -> BrokerStatusOut:
    return BrokerStatusOut.model_validate(
        {
            "id": conn.id,
            "broker": conn.broker,
            "environment": conn.environment,
            "is_active": conn.is_active,
            "credential_state": credential_state,
            "recovery_hint": recovery_hint,
            "last_test_at": conn.last_test_at,
            "last_test_ok": conn.last_test_ok,
            "last_sync_at": conn.last_sync_at,
            "account_id": conn.account_id,
            "account_currency": conn.account_currency,
            "created_at": conn.created_at,
        }
    )


def _mock_connect_status() -> BrokerStatusOut:
    now = datetime.now(UTC)
    return BrokerStatusOut.model_validate(
        {
            "id": uuid.uuid4(),
            "broker": "trading212",
            "environment": "mock",
            "is_active": True,
            "credential_state": "mock",
            "recovery_hint": MOCK_CREDENTIALS_IGNORED_HINT,
            "last_test_at": now,
            "last_test_ok": True,
            "last_sync_at": now,
            "account_id": "MOCK-CREDENTIALS-IGNORED",
            "account_currency": "USD",
            "created_at": now,
        }
    )


def _demo_missing_credentials_status() -> BrokerStatusOut:
    now = datetime.now(UTC)
    return BrokerStatusOut.model_validate(
        {
            "id": uuid.uuid4(),
            "broker": "trading212",
            "environment": "demo",
            "is_active": False,
            "credential_state": "not_connected",
            "recovery_hint": DEMO_CREDENTIALS_MISSING_HINT,
            "last_test_at": None,
            "last_test_ok": False,
            "last_sync_at": None,
            "account_id": None,
            "account_currency": None,
            "created_at": now,
        }
    )


def _broker_account_id(value: object) -> str | None:
    """Normalise broker account identifiers for API schemas and DB storage.

    Trading 212 may return account identifiers as integers, while our API schema
    and broker_connections.account_id column expect strings.
    """
    if value is None:
        return None
    return str(value)


@router.post("/connect", response_model=BrokerStatusOut)
async def connect_broker(
    body: BrokerConnectRequest,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> BrokerStatusOut:
    """Test and then store encrypted broker credentials."""
    if settings.APP_MODE == "mock":
        await _audit(
            db,
            "broker_mock_connect_ignored_credentials",
            user_id=current_user.id,
            payload={
                "requested_environment": body.environment,
                "credentials_ignored": True,
                "credentials_stored": False,
            },
        )
        return _mock_connect_status()

    try:
        require_broker_environment(body.environment, action="broker credential test")
    except SafetyPolicyViolation as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc

    # Test the submitted credentials before replacing a working connection.
    from app.broker.provider import (
        BrokerProviderCredentials,
        BrokerProviderRequest,
        BrokerProviderValidationError,
        BrokerRuntimeEnvironment,
        create_trading212_provider_adapter,
    )

    try:
        trading212_adapter = create_trading212_provider_adapter(
            # Credential tests intentionally allow demo/live validation through the provider.
            BrokerProviderRequest(
                broker_id="trading212",
                environment=cast(BrokerRuntimeEnvironment, body.environment),
                purpose="credential_test",
                user_id=current_user.id,
            ),
            BrokerProviderCredentials(api_key=body.api_key, api_secret=body.api_secret),
            app_mode=settings.APP_MODE,
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
        )
    except BrokerProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with trading212_adapter as broker:
        test = await broker.test_connection()

    if not test["is_ok"]:
        detail = test["error"] or "Broker connection test failed"
        diagnostics = test.get("diagnostics")
        raise HTTPException(
            status_code=400,
            detail={"message": detail, "diagnostics": diagnostics} if diagnostics else detail,
        )

    # Upsert: deactivate existing connection for this env
    result = await db.execute(
        select(BrokerConnection).where(
            BrokerConnection.user_id == current_user.id,
            BrokerConnection.environment == body.environment,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.api_key_encrypted = encrypt_field(body.api_key)
        existing.api_secret_encrypted = encrypt_field(body.api_secret)
        existing.is_active = True
        conn = existing
    else:
        conn = BrokerConnection(
            id=uuid.uuid4(),
            user_id=current_user.id,
            broker="trading212",
            environment=body.environment,
            api_key_encrypted=encrypt_field(body.api_key),
            api_secret_encrypted=encrypt_field(body.api_secret),
            is_active=True,
        )
        db.add(conn)

    await db.flush()

    conn.last_test_at = datetime.now(UTC)
    conn.last_test_ok = test["is_ok"]
    conn.account_id = _broker_account_id(test.get("account_id"))
    conn.account_currency = test.get("currency")

    await _audit(
        db,
        "broker_connected",
        user_id=current_user.id,
        payload={"environment": body.environment, "test_ok": test["is_ok"]},
    )
    await db.refresh(conn)
    return _serialize_status(conn, credential_state="configured")


@router.post("/test", response_model=BrokerTestResult)
async def test_connection(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> BrokerTestResult:
    """Test the active broker connection."""
    if settings.APP_MODE == "mock":
        from app.broker.mock_adapter import MockBrokerAdapter

        async with MockBrokerAdapter() as broker:
            result = await broker.test_connection()
        return BrokerTestResult(**result)

    result_q = await db.execute(
        select(BrokerConnection)
        .where(
            BrokerConnection.user_id == current_user.id,
            BrokerConnection.is_active.is_(True),
            BrokerConnection.environment == settings.APP_MODE,
        )
        .limit(1)
    )
    conn = result_q.scalar_one_or_none()
    if not conn:
        return BrokerTestResult(
            is_ok=False, account_id=None, currency=None, error="No active connection"
        )

    try:
        require_broker_environment(conn.environment, action="broker connection test")
    except SafetyPolicyViolation as exc:
        raise HTTPException(status_code=403, detail=exc.reason) from exc

    try:
        api_key = decrypt_field(conn.api_key_encrypted)
        api_secret = decrypt_field(conn.api_secret_encrypted)
    except CredentialDecryptionError as exc:
        await mark_broker_connection_reconnect_required(
            db,
            conn,
            str(exc),
            actor=str(current_user.id),
            commit=True,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    from app.broker.provider import (
        BrokerProviderCredentials,
        BrokerProviderRequest,
        BrokerProviderValidationError,
        BrokerRuntimeEnvironment,
        create_trading212_provider_adapter,
    )

    try:
        trading212_adapter = create_trading212_provider_adapter(
            # Credential tests intentionally allow demo/live validation through the provider.
            BrokerProviderRequest(
                broker_id="trading212",
                environment=cast(BrokerRuntimeEnvironment, conn.environment),
                purpose="credential_test",
                user_id=current_user.id,
            ),
            BrokerProviderCredentials(api_key=api_key, api_secret=api_secret),
            app_mode=settings.APP_MODE,
            live_trading_enabled=bool(settings.LIVE_TRADING_ENABLED),
        )
    except BrokerProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with trading212_adapter as broker:
        test = await broker.test_connection()

    conn.last_test_at = datetime.now(UTC)
    conn.last_test_ok = test["is_ok"]
    test["account_id"] = _broker_account_id(test.get("account_id"))
    return BrokerTestResult(**test)


@router.delete("/disconnect")
async def disconnect_broker(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int | str]:
    result = await db.execute(
        select(BrokerConnection).where(
            BrokerConnection.user_id == current_user.id,
            BrokerConnection.is_active.is_(True),
        )
    )
    connections = result.scalars().all()
    for conn in connections:
        conn.is_active = False

    await _audit(db, "broker_disconnected", user_id=current_user.id)
    return {"message": "Broker connections deactivated", "count": len(connections)}


@router.get("/status", response_model=BrokerStatusOut | None)
async def broker_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BrokerStatusOut | None:
    if settings.APP_MODE == "mock":
        # Return a synthetic mock status.
        return _mock_connect_status()

    result = await db.execute(
        select(BrokerConnection)
        .where(
            BrokerConnection.user_id == current_user.id,
            BrokerConnection.environment == settings.APP_MODE,
        )
        .order_by(BrokerConnection.created_at.desc())
        .limit(1)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        if settings.APP_MODE == "demo":
            return _demo_missing_credentials_status()
        return None

    if conn.is_active:
        try:
            decrypt_field(conn.api_key_encrypted)
            decrypt_field(conn.api_secret_encrypted)
            return _serialize_status(conn, credential_state="configured")
        except CredentialDecryptionError as exc:
            await mark_broker_connection_reconnect_required(
                db,
                conn,
                str(exc),
                actor=str(current_user.id),
                commit=True,
            )
            return _serialize_status(
                conn,
                credential_state="reconnect_required",
                recovery_hint=BROKER_RECOVERY_HINT,
            )

    if conn.last_test_ok is False:
        try:
            decrypt_field(conn.api_key_encrypted)
            decrypt_field(conn.api_secret_encrypted)
        except CredentialDecryptionError:
            return _serialize_status(
                conn,
                credential_state="reconnect_required",
                recovery_hint=BROKER_RECOVERY_HINT,
            )

    return _serialize_status(conn, credential_state="not_connected")


@router.get(
    "/reconciliation/status",
    response_model=DemoReconciliationWorkerStatus,
)
async def demo_reconciliation_status(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DemoReconciliationWorkerStatus:
    broker = _StatusBroker()
    broker.environment = settings.T212_ENVIRONMENT
    status = await DemoReconciliationWorker(db, broker).get_worker_status()
    return DemoReconciliationWorkerStatus.model_validate(status)


@router.post(
    "/reconciliation/run-once",
    response_model=DemoReconciliationWorkerRunSummary,
)
async def run_demo_reconciliation_once(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker: object = Depends(get_broker),
) -> DemoReconciliationWorkerRunSummary:
    try:
        if hasattr(broker, "__aenter__"):
            async with broker as active_broker:  # type: ignore[attr-defined]
                history_broker = cast(ReconciliationHistoryBrokerProtocol, active_broker)
                summary = await DemoReconciliationWorker(
                    db,
                    history_broker,
                    actor=str(current_user.id),
                ).run_once()
        else:
            history_broker = cast(ReconciliationHistoryBrokerProtocol, broker)
            summary = await DemoReconciliationWorker(
                db,
                history_broker,
                actor=str(current_user.id),
            ).run_once()
    except SafetyPolicyViolation as exc:
        raise HTTPException(status_code=403, detail=exc.reason) from exc

    await db.commit()
    return DemoReconciliationWorkerRunSummary.model_validate(summary)


@router.get(
    "/reconciliation/scheduler/status",
    response_model=DemoReconciliationSchedulerStatus,
)
async def demo_reconciliation_scheduler_status(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DemoReconciliationSchedulerStatus:
    status = await build_scheduler_status(db)
    return DemoReconciliationSchedulerStatus.model_validate(status)


@router.post(
    "/reconciliation/scheduler/run-once",
    response_model=DemoReconciliationSchedulerRunResult,
)
async def run_demo_reconciliation_scheduler_once(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    broker: object = Depends(get_broker),
) -> DemoReconciliationSchedulerRunResult:
    try:
        if hasattr(broker, "__aenter__"):
            async with broker as active_broker:  # type: ignore[attr-defined]
                history_broker = cast(ReconciliationHistoryBrokerProtocol, active_broker)
                result = await DemoReconciliationScheduler(
                    db,
                    history_broker,
                    actor=str(current_user.id),
                ).run_once()
        else:
            history_broker = cast(ReconciliationHistoryBrokerProtocol, broker)
            result = await DemoReconciliationScheduler(
                db,
                history_broker,
                actor=str(current_user.id),
            ).run_once()
    except SafetyPolicyViolation as exc:
        raise HTTPException(status_code=403, detail=exc.reason) from exc

    await db.commit()
    return DemoReconciliationSchedulerRunResult.model_validate(result)
