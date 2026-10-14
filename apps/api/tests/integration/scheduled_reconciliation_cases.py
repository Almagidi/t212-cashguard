"""Shared pieces for the scheduled reconciler's tests: rows, fake brokers and a task harness.

The harness runs the real ``reconcile_pending_orders`` task body against the test's own
database. Only the things outside the process are replaced: the Redis lock, credential
decryption and the broker adapter the provider would build.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.broker.provider import trading212_account_scope
from app.core.config import settings
from app.db.models import BrokerConnection, Order, User
from app.workers import tasks

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.broker.provider import BrokerProviderRequest

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
WORKING = {"status": "WORKING"}


def filled(quantity: str = "1", price: str = "10") -> dict[str, Any]:
    return {"status": "FILLED", "filledQuantity": quantity, "filledPrice": price}


class FakeBroker:
    """A broker adapter that answers reads from a script and refuses every write."""

    def __init__(self, name: str, environment: str = "demo") -> None:
        self.name = name
        self.environment = environment
        self.account_scope: str | None = None
        self.responses: dict[str, Any] = {}
        self.default_response: Any = dict(WORKING)
        self.history: Any = {"items": []}
        self.order_reads: list[str] = []
        self.history_reads: list[str] = []
        self.write_calls: list[str] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeBroker:
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.exited += 1

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self.order_reads.append(broker_order_id)
        answer = self.responses.get(broker_order_id, self.default_response)
        if callable(answer):
            answer = await answer(broker_order_id)
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    async def get_historical_orders(self, **kwargs: Any) -> Any:
        self.history_reads.append(str(kwargs.get("ticker")))
        if isinstance(self.history, BaseException):
            raise self.history
        if callable(self.history):
            page = self.history(**kwargs)
            return await page if inspect.isawaitable(page) else page
        return self.history

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("place_", "cancel_", "modify_", "submit_")):
            self.write_calls.append(name)
            raise AssertionError(f"reconciliation must never call the broker's {name}")
        raise AttributeError(name)


class Brokers:
    """The adapters the provider hands out, one per user, with the real scope rule."""

    def __init__(self) -> None:
        self.by_user: dict[uuid.UUID, FakeBroker] = {}
        self.requests: list[BrokerProviderRequest] = []

    def for_user(self, user_id: uuid.UUID) -> FakeBroker:
        return self.by_user.setdefault(user_id, FakeBroker(name=str(user_id)))

    def create(self, request: BrokerProviderRequest, _credentials: Any, **_kwargs: Any) -> Any:
        self.requests.append(request)
        if request.user_id is None:
            raise AssertionError("the reconciler must name the user whose account it reads")
        broker = self.for_user(request.user_id)
        broker.environment = request.environment
        broker.account_scope = trading212_account_scope(request)
        return broker

    @property
    def all_order_reads(self) -> list[str]:
        return [read for broker in self.by_user.values() for read in broker.order_reads]

    @property
    def all_write_calls(self) -> list[str]:
        return [call for broker in self.by_user.values() for call in broker.write_calls]


async def add_connection(
    db: AsyncSession,
    *,
    environment: str = "demo",
    account_id: str | None = "ACC-1",
    is_active: bool = True,
    broker: str = "trading212",
    created_at: datetime | None = None,
) -> BrokerConnection:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.test", hashed_password="x")
    db.add(user)
    await db.flush()
    connection = BrokerConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        broker=broker,
        environment=environment,
        api_key_encrypted=f"key-{user.id}",
        api_secret_encrypted=f"secret-{user.id}",
        is_active=is_active,
        account_id=account_id,
        created_at=created_at or T0,
    )
    db.add(connection)
    await db.commit()
    # Detached, so the values stay readable whatever the session does next.
    db.expunge(connection)
    db.expunge(user)
    return connection


def scope_of(connection: BrokerConnection) -> str | None:
    """The account scope the provider derives for a connection."""
    from app.broker.provider import BrokerProviderRequest

    return trading212_account_scope(
        BrokerProviderRequest(
            broker_id="trading212",
            environment=connection.environment,  # type: ignore[arg-type]
            purpose="worker_reconcile",
            user_id=connection.user_id,
            account_id=connection.account_id,
        )
    )


async def add_order(
    db: AsyncSession,
    *,
    account_scope: str | None,
    broker_order_id: str | None = None,
    status: str = "accepted",
    environment: str | None = "demo",
    venue: str = "t212",
    is_dry_run: bool = False,
    created_at: datetime | None = None,
    last_reconciled_at: datetime | None = None,
    retry_count: int = 0,
    order_id: uuid.UUID | None = None,
    ticker: str = "AAPL_US_EQ",
    side: str = "buy",
    quantity: str = "1",
    commit: bool = True,
) -> Order:
    identifier = order_id or uuid.uuid4()
    if broker_order_id is None and status != "submission_unknown" and status != "submitted":
        broker_order_id = f"b-{identifier.hex[:12]}"
    order = Order(
        id=identifier,
        client_order_key=f"key-{identifier.hex}",
        ticker=ticker,
        side=side,
        order_type="limit",
        quantity=Decimal(quantity),
        limit_price=Decimal("10"),
        status=status,
        broker_order_id=broker_order_id,
        execution_environment=environment,
        broker_account_scope=account_scope,
        venue=venue,
        is_dry_run=is_dry_run,
        submitted_at=(created_at or T0),
        created_at=created_at or T0,
        last_reconciled_at=last_reconciled_at,
        retry_count=retry_count,
    )
    db.add(order)
    if commit:
        await db.commit()
        # Detached: tests read the saved row through ``stored``, never this copy.
        db.expunge(order)
    return order


async def connection_is_active(db: AsyncSession, connection: BrokerConnection) -> bool:
    result = await db.execute(
        select(BrokerConnection.is_active).where(BrokerConnection.id == connection.id)
    )
    return bool(result.scalar_one())


async def stored(db: AsyncSession, order_id: uuid.UUID) -> Order:
    """The order as the database has it now, not as this session remembers it."""
    result = await db.execute(
        select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def rewind_last_attempt(db: AsyncSession, order_id: uuid.UUID, seconds: float) -> None:
    """Make the order's last recorded attempt look ``seconds`` older than it is."""
    order = await stored(db, order_id)
    assert order.last_reconciled_at is not None
    order.last_reconciled_at = order.last_reconciled_at - timedelta(seconds=seconds)
    await db.commit()


class _BorrowedSession:
    """Lends the test's session to the task, and drops unsaved work when the task lets go,
    as closing a real session would."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        await self._session.rollback()


@asynccontextmanager
async def _granted_lock(*_args: Any, **_kwargs: Any) -> Any:
    yield True


def install_task_environment(
    monkeypatch: pytest.MonkeyPatch,
    brokers: Brokers,
    session_factory: Any,
    *,
    app_mode: str = "demo",
) -> None:
    """Point the task at the given sessions and brokers; grant it the owner lock."""
    monkeypatch.setattr(settings, "APP_MODE", app_mode)
    monkeypatch.setattr(settings, "T212_ENVIRONMENT", app_mode)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", app_mode == "live")
    monkeypatch.setattr("app.core.redis.task_lock", _granted_lock)
    monkeypatch.setattr("app.core.redis.exclusive_task_lock", _granted_lock)
    monkeypatch.setattr("app.core.security.decrypt_field", lambda value: f"plain-{value}")
    monkeypatch.setattr("app.broker.provider.create_trading212_provider_adapter", brokers.create)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", session_factory)
    # The task body is a coroutine; hand it back so the test can await it in its own loop.
    monkeypatch.setattr(tasks, "run_monitored_task", lambda _name, factory, **_kw: factory())


def install_borrowed_session(
    monkeypatch: pytest.MonkeyPatch, brokers: Brokers, db: AsyncSession, **kwargs: Any
) -> None:
    install_task_environment(monkeypatch, brokers, lambda: _BorrowedSession(db), **kwargs)


async def run_task() -> dict[str, Any]:
    """One run of the scheduled reconciler, exactly as Celery would start it."""
    summary: dict[str, Any] = await tasks.reconcile_pending_orders.run()
    return summary
