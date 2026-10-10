"""PostgreSQL proof that a dispatched broker request survives process death.

Each test uses separate database sessions as stand-ins for separate processes, so what the
second session sees is exactly what was committed by the first.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.models import (
    Alert,
    AppSettings,
    AuditLog,
    Order,
    OrderEvent,
    OrderSubmissionAttempt,
    RiskEvent,
)
from app.execution.engine import ExecutionEngine

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the durability proof",
    ),
    pytest.mark.asyncio,
]


class SimulatedProcessDeath(BaseException):
    """Stands in for the process being killed: nothing in the engine may catch it."""


class DurabilityBroker:
    environment = "demo"

    def __init__(self, sessions=None, *, die="never"):
        self.sessions = sessions
        self.die = die
        self.posts: list[dict] = []
        self.seen_by_other_session: dict | None = None
        self.history: list[dict] = []

    async def place_market_order(self, ticker, quantity, time_validity="DAY"):
        if self.die == "before_post":
            raise SimulatedProcessDeath
        if self.sessions is not None:
            async with self.sessions() as other:
                order = (
                    await other.execute(select(Order).where(Order.ticker == ticker))
                ).scalar_one_or_none()
                attempt = None
                if order is not None:
                    attempt = (
                        await other.execute(
                            select(OrderSubmissionAttempt).where(
                                OrderSubmissionAttempt.order_id == order.id
                            )
                        )
                    ).scalar_one_or_none()
                seen_status = order.status if order else None
                seen_outcome = attempt.outcome if attempt else "no-attempt"
                # NOWAIT fails at once if the submitting session still held a row lock.
                lockable = True
                try:
                    await other.execute(
                        text("SELECT id FROM orders WHERE ticker = :ticker FOR UPDATE NOWAIT"),
                        {"ticker": ticker},
                    )
                except Exception:
                    lockable = False
                await other.rollback()
                self.seen_by_other_session = {
                    "order_status": seen_status,
                    "attempt_outcome": seen_outcome,
                    "attempt_exists": attempt is not None,
                    "row_lockable_by_another_session": lockable,
                }
        self.posts.append({"ticker": ticker, "quantity": quantity})
        if self.die == "after_post":
            raise SimulatedProcessDeath
        return {"id": f"BROKER-{ticker}", "status": "WORKING"}

    async def get_historical_orders(self, cursor=None, ticker=None, limit=50):
        return {"items": list(self.history), "nextPagePath": None}


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    # These tests reconcile straight after the simulated crash.
    monkeypatch.setattr(ExecutionEngine, "UNKNOWN_SUBMISSION_MIN_AGE", timedelta(0))
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ticker = f"DUR{uuid.uuid4().hex[:8].upper()}"

    async with factory() as setup:
        app_settings = await setup.get(AppSettings, 1)
        created_settings = app_settings is None
        if app_settings is None:
            app_settings = AppSettings(id=1)
            setup.add(app_settings)
        previous = (app_settings.auto_trading_enabled, app_settings.kill_switch_active)
        app_settings.auto_trading_enabled = True
        app_settings.kill_switch_active = False
        await setup.commit()

    factory.ticker = ticker  # type: ignore[attr-defined]
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            order_ids = (
                (await cleanup.execute(select(Order.id).where(Order.ticker == ticker)))
                .scalars()
                .all()
            )
            if order_ids:
                text_ids = [str(order_id) for order_id in order_ids]
                await cleanup.execute(
                    delete(OrderSubmissionAttempt).where(
                        OrderSubmissionAttempt.order_id.in_(order_ids)
                    )
                )
                await cleanup.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(RiskEvent).where(RiskEvent.order_id.in_(order_ids)))
                await cleanup.execute(delete(AuditLog).where(AuditLog.entity_id.in_(text_ids)))
                await cleanup.execute(delete(Order).where(Order.id.in_(order_ids)))
            await cleanup.execute(
                delete(Alert).where(Alert.title == f"Order outcome unknown: {ticker}")
            )
            if created_settings:
                await cleanup.execute(delete(AppSettings).where(AppSettings.id == 1))
            else:
                restored = await cleanup.get(AppSettings, 1)
                restored.auto_trading_enabled, restored.kill_switch_active = previous
            await cleanup.commit()
        await engine.dispose()


async def _intent(session, broker, ticker):
    return await ExecutionEngine(session, broker).create_order_intent(
        ticker=ticker,
        side="buy",
        order_type="market",
        quantity=Decimal("2"),
        is_dry_run=False,
        estimated_price=Decimal("50"),
    )


async def _load(sessions, ticker):
    async with sessions() as session:
        order = (await session.execute(select(Order).where(Order.ticker == ticker))).scalar_one()
        attempt = (
            await session.execute(
                select(OrderSubmissionAttempt).where(OrderSubmissionAttempt.order_id == order.id)
            )
        ).scalar_one_or_none()
        return order, attempt


async def test_dispatch_evidence_is_committed_before_the_request_is_sent(sessions) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker(sessions)

    async with sessions() as session:
        order = await _intent(session, broker, ticker)
        order = await ExecutionEngine(session, broker).submit_order(order)

    # What an independent connection could read while the request was in flight.
    assert broker.seen_by_other_session == {
        "order_status": "submission_unknown",
        "attempt_outcome": None,
        "attempt_exists": True,
        "row_lockable_by_another_session": True,
    }
    order, attempt = await _load(sessions, ticker)
    assert order.status == "accepted"
    assert order.broker_order_id == f"BROKER-{ticker}"
    assert attempt.outcome == "accepted"


async def test_process_death_after_the_post_leaves_one_recoverable_order(sessions) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker(die="after_post")

    async with sessions() as first_process:
        order = await _intent(first_process, broker, ticker)
        with pytest.raises(SimulatedProcessDeath):
            await ExecutionEngine(first_process, broker).submit_order(order)
        # The process is gone: its session ends without any further commit.

    order, attempt = await _load(sessions, ticker)
    assert order.status == "submission_unknown"
    assert order.broker_order_id is None
    assert attempt is not None
    assert attempt.outcome is None
    assert len(broker.posts) == 1

    # A second process finds the original order and cannot send it again.
    broker.die = "never"
    async with sessions() as second_process:
        engine = ExecutionEngine(second_process, broker)
        same = await _intent(second_process, broker, ticker)
        assert same.id == order.id
        with pytest.raises(ValueError, match="submission_unknown"):
            await engine.submit_order(same)
        assert len(broker.posts) == 1

        # Reconciliation resolves it from a later broker history record.
        broker.history = [
            {
                "id": "BROKER-RECOVERED",
                "ticker": ticker,
                "status": "FILLED",
                "type": "MARKET",
                "quantity": 2.0,
                "filledQuantity": 2.0,
                "filledPrice": 50.25,
                "createdAt": attempt.dispatch_committed_at.isoformat(),
            }
        ]
        resolved = await engine.reconcile_unknown_submission(same)
        await second_process.commit()
        assert resolved.status == "filled"

    order, attempt = await _load(sessions, ticker)
    assert order.status == "filled"
    assert order.broker_order_id == "BROKER-RECOVERED"
    assert order.filled_quantity == Decimal("2")
    assert len(broker.posts) == 1
    async with sessions() as verify:
        orders = (await verify.execute(select(Order).where(Order.ticker == ticker))).scalars().all()
        assert len(orders) == 1


async def test_process_death_before_the_broker_call_stays_blocked_and_is_not_resent(
    sessions,
) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker(die="before_post")

    async with sessions() as first_process:
        order = await _intent(first_process, broker, ticker)
        with pytest.raises(SimulatedProcessDeath):
            await ExecutionEngine(first_process, broker).submit_order(order)

    order, attempt = await _load(sessions, ticker)
    assert order.status == "submission_unknown"
    assert attempt is not None
    assert broker.posts == []

    broker.die = "never"
    async with sessions() as second_process:
        engine = ExecutionEngine(second_process, broker)
        same = await _intent(second_process, broker, ticker)
        assert same.id == order.id
        with pytest.raises(ValueError, match="submission_unknown"):
            await engine.submit_order(same)
        # No broker record exists, so reconciliation leaves it unresolved.
        unresolved = await engine.reconcile_unknown_submission(same)
        await second_process.commit()
        assert unresolved.status == "submission_unknown"

    assert broker.posts == []


async def test_failure_before_the_durability_boundary_sends_nothing(sessions, monkeypatch) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker()

    async with sessions() as session:
        order = await _intent(session, broker, ticker)

        async def failing_commit():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(session, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await ExecutionEngine(session, broker).submit_order(order)
        await session.rollback()

    assert broker.posts == []
    async with sessions() as verify:
        assert (
            await verify.execute(select(Order).where(Order.ticker == ticker))
        ).scalar_one_or_none() is None


async def test_database_allows_one_dispatch_per_order_and_keeps_its_evidence(sessions) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker(die="after_post")

    async with sessions() as session:
        order = await _intent(session, broker, ticker)
        with pytest.raises(SimulatedProcessDeath):
            await ExecutionEngine(session, broker).submit_order(order)

    order, attempt = await _load(sessions, ticker)

    async with sessions() as second:
        second.add(
            OrderSubmissionAttempt(
                id=uuid.uuid4(),
                order_id=order.id,
                request_fingerprint="0" * 64,
                dispatch_committed_at=attempt.dispatch_committed_at,
            )
        )
        with pytest.raises(IntegrityError):
            await second.commit()
        await second.rollback()

    async with sessions() as third:
        await third.execute(delete(OrderEvent).where(OrderEvent.order_id == order.id))
        with pytest.raises(IntegrityError):
            await third.execute(delete(Order).where(Order.id == order.id))
        await third.rollback()


async def test_two_processes_submitting_the_same_intent_send_one_request(sessions) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker()

    async with sessions() as setup:
        await _intent(setup, broker, ticker)
        await setup.commit()

    async with sessions() as first, sessions() as second:
        # Both processes read the order while it is still a pending intent.
        first_order = (
            await first.execute(select(Order).where(Order.ticker == ticker))
        ).scalar_one()
        second_order = (
            await second.execute(select(Order).where(Order.ticker == ticker))
        ).scalar_one()
        assert first_order.status == second_order.status == "pending_intent"

        results = await asyncio.gather(
            ExecutionEngine(first, broker).submit_order(first_order),
            ExecutionEngine(second, broker).submit_order(second_order),
            return_exceptions=True,
        )
        await first.rollback()
        await second.rollback()

    # The database lets one dispatch through; the other fails before any request is sent.
    assert len(broker.posts) == 1
    assert sum(isinstance(result, BaseException) for result in results) == 1
    order, attempt = await _load(sessions, ticker)
    assert order.status == "accepted"
    assert attempt.outcome == "accepted"


async def test_process_death_is_reported_once_by_reconciliation(sessions) -> None:
    ticker = sessions.ticker
    broker = DurabilityBroker(die="after_post")

    async with sessions() as first_process:
        order = await _intent(first_process, broker, ticker)
        with pytest.raises(SimulatedProcessDeath):
            await ExecutionEngine(first_process, broker).submit_order(order)

    async with sessions() as verify:
        assert (
            await verify.execute(select(RiskEvent).where(RiskEvent.ticker == ticker))
        ).scalars().all() == []

    for _ in range(2):
        async with sessions() as reconciler:
            unknown = (
                await reconciler.execute(select(Order).where(Order.ticker == ticker))
            ).scalar_one()
            await ExecutionEngine(reconciler, broker).reconcile_unknown_submission(unknown)
            await reconciler.commit()

    async with sessions() as verify:
        events = (
            (await verify.execute(select(RiskEvent).where(RiskEvent.ticker == ticker)))
            .scalars()
            .all()
        )
        assert [event.event_type for event in events] == ["submission_unknown"]
        assert events[0].payload["severity"] == "critical"
