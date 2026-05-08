"""Paper-only execution lifecycle.

This module deliberately has no dependency on broker adapters. It persists a
local paper order, simulates a fill, updates a local paper position snapshot,
and writes audit entries. The global kill switch is still enforced.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select

from app.core.config import settings
from app.db.models import AuditLog, BrokerConnection, Order, OrderEvent, PositionSnapshot
from app.risk.engine import RiskEngine, RiskViolation
from app.services.execution_quality import apply_order_execution_quality

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.schemas import PaperOrderCreate
    from app.db.models import User


PAPER_BROKER = "paper"
PAPER_ENVIRONMENT = "mock"
PAPER_ORDER_STATUS = "filled"
PAPER_EXECUTION_ENVIRONMENT = "paper_mock"
PAPER_SUPPORTED_VENUES = {"paper", "mock"}


class PaperExecutionError(Exception):
    """Raised when paper execution is blocked before order creation."""

    def __init__(self, reason: str, status_code: int = 422):
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


class PaperExecutionEngine:
    """Local-only paper execution service."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _audit(
        self,
        action: str,
        *,
        actor: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                payload={
                    "paper_only": True,
                    "mode": settings.APP_MODE,
                    **(payload or {}),
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    async def _order_event(
        self,
        order_id: uuid.UUID,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            OrderEvent(
                id=uuid.uuid4(),
                order_id=order_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                payload={"paper_only": True, **(payload or {})},
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.flush()

    async def _paper_connection(self, user: User) -> BrokerConnection:
        result = await self.db.execute(
            select(BrokerConnection).where(
                BrokerConnection.user_id == user.id,
                BrokerConnection.broker == PAPER_BROKER,
                BrokerConnection.environment == PAPER_ENVIRONMENT,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        connection = BrokerConnection(
            id=uuid.uuid4(),
            user_id=user.id,
            broker=PAPER_BROKER,
            environment=PAPER_ENVIRONMENT,
            api_key_encrypted="paper-only-no-real-credential",
            api_secret_encrypted="paper-only-no-real-credential",
            is_active=True,
            last_test_at=datetime.now(UTC),
            last_test_ok=True,
            account_id="paper-local",
            account_currency="USD",
        )
        self.db.add(connection)
        await self.db.flush()
        return connection

    async def _latest_paper_positions(
        self,
        user: User,
    ) -> dict[str, PositionSnapshot]:
        connection = await self._paper_connection(user)
        result = await self.db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.connection_id == connection.id)
            .order_by(desc(PositionSnapshot.snapshotted_at))
        )
        latest: dict[str, PositionSnapshot] = {}
        for snapshot in result.scalars().all():
            ticker = snapshot.ticker.upper()
            latest.setdefault(ticker, snapshot)
        return latest

    async def current_open_paper_positions_count(self, user: User) -> int:
        latest = await self._latest_paper_positions(user)
        return sum(1 for position in latest.values() if position.quantity > 0)

    def _client_order_key(self, body: PaperOrderCreate, quantity: Decimal) -> str:
        raw = (
            f"paper:{body.source}:{body.strategy or 'manual'}:"
            f"{body.ticker.upper()}:{body.side}:{quantity}:{uuid.uuid4()}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    async def _run_risk_check(
        self,
        body: PaperOrderCreate,
        quantity: Decimal,
        *,
        actor: str,
        user: User,
    ) -> None:
        open_positions = await self.current_open_paper_positions_count(user)
        risk = RiskEngine(self.db)
        try:
            await risk.run_all_checks(
                ticker=body.ticker.upper(),
                side=body.side,
                quantity=quantity,
                estimated_price=body.estimated_price,
                available_cash=Decimal("100000"),
                account_value=Decimal("100000"),
                realized_pnl_today=Decimal("0"),
                current_open_positions=open_positions,
                skip_auto_trading_check=True,
            )
        except RiskViolation as exc:
            await self._audit(
                "paper_signal_rejected",
                actor=actor,
                user_id=user.id,
                payload={
                    "ticker": body.ticker.upper(),
                    "side": body.side,
                    "venue": body.venue,
                    "source": body.source,
                    "strategy": body.strategy,
                    "reason": exc.reason,
                    "decision_code": exc.event_type,
                },
            )
            await self._audit(
                "paper_risk_check_result",
                actor=actor,
                user_id=user.id,
                payload={
                    "ticker": body.ticker.upper(),
                    "result": "blocked",
                    "reason": exc.reason,
                    "decision_code": exc.event_type,
                },
            )
            raise PaperExecutionError(exc.reason) from exc

        await self._audit(
            "paper_risk_check_result",
            actor=actor,
            user_id=user.id,
            payload={
                "ticker": body.ticker.upper(),
                "result": "passed",
                "decision_code": "PAPER_RISK_PASSED",
            },
        )

    async def _check_sell_quantity_available(
        self,
        body: PaperOrderCreate,
        quantity: Decimal,
        *,
        actor: str,
        user: User,
    ) -> None:
        if body.side != "sell":
            return

        latest_positions = await self._latest_paper_positions(user)
        available_quantity = Decimal("0")
        current_position = latest_positions.get(body.ticker.upper())
        if current_position is not None:
            available_quantity = current_position.quantity_available or current_position.quantity

        if quantity <= available_quantity:
            return

        reason = (
            f"Paper sell quantity {quantity} exceeds available paper quantity "
            f"{available_quantity} for {body.ticker.upper()}."
        )
        await self._audit(
            "paper_signal_rejected",
            actor=actor,
            user_id=user.id,
            payload={
                "ticker": body.ticker.upper(),
                "side": body.side,
                "quantity": str(quantity),
                "available_quantity": str(available_quantity),
                "venue": body.venue,
                "source": body.source,
                "strategy": body.strategy,
                "reason": reason,
                "decision_code": "paper_oversell_block",
                "no_broker_order_sent": True,
            },
        )
        await self._audit(
            "paper_risk_check_result",
            actor=actor,
            user_id=user.id,
            payload={
                "ticker": body.ticker.upper(),
                "side": body.side,
                "quantity": str(quantity),
                "available_quantity": str(available_quantity),
                "result": "blocked",
                "reason": reason,
                "decision_code": "paper_oversell_block",
                "no_broker_order_sent": True,
            },
        )
        raise PaperExecutionError(reason)

    async def _update_position(
        self,
        *,
        user: User,
        order: Order,
        price: Decimal,
        actor: str,
    ) -> PositionSnapshot:
        connection = await self._paper_connection(user)
        latest_positions = await self._latest_paper_positions(user)
        previous = latest_positions.get(order.ticker)
        previous_quantity = previous.quantity if previous is not None else Decimal("0")
        previous_avg = previous.avg_price if previous is not None else price

        if order.side == "buy":
            new_quantity = previous_quantity + order.quantity
            new_avg = (
                ((previous_quantity * previous_avg) + (order.quantity * price)) / new_quantity
                if new_quantity > 0
                else price
            )
        else:
            new_quantity = max(Decimal("0"), previous_quantity - order.quantity)
            new_avg = previous_avg

        snapshot = PositionSnapshot(
            id=uuid.uuid4(),
            connection_id=connection.id,
            ticker=order.ticker,
            quantity=new_quantity,
            avg_price=new_avg,
            current_price=price,
            unrealized_pnl=Decimal("0"),
            quantity_available=new_quantity,
            raw={
                "paper_only": True,
                "source_order_id": str(order.id),
                "side": order.side,
                "simulated_fill_price": str(price),
                "previous_quantity": str(previous_quantity),
                "new_quantity": str(new_quantity),
            },
            snapshotted_at=datetime.now(UTC),
        )
        self.db.add(snapshot)
        await self.db.flush()
        await self._audit(
            "paper_position_updated",
            actor=actor,
            entity_type="position",
            entity_id=f"paper:{order.ticker}",
            user_id=user.id,
            payload={
                "ticker": order.ticker,
                "quantity": str(new_quantity),
                "avg_price": str(new_avg),
                "order_id": str(order.id),
            },
        )
        return snapshot

    async def execute(self, body: PaperOrderCreate, *, user: User) -> Order:
        actor = user.email
        if settings.APP_MODE != "mock":
            await self._audit(
                "paper_signal_rejected",
                actor=actor,
                user_id=user.id,
                payload={
                    "ticker": body.ticker.upper(),
                    "side": body.side,
                    "venue": body.venue,
                    "source": body.source,
                    "reason": "Paper execution is available only in APP_MODE=mock.",
                    "decision_code": "PAPER_MODE_BLOCK",
                },
            )
            raise PaperExecutionError(
                "Paper execution is available only in APP_MODE=mock.",
                status_code=403,
            )

        if body.venue not in PAPER_SUPPORTED_VENUES:
            raise PaperExecutionError("Unsupported paper venue.")

        if body.quantity is not None:
            quantity = body.quantity
        else:
            if body.notional is None:
                raise PaperExecutionError("quantity or notional is required")
            quantity = body.notional / body.estimated_price
        await self._check_sell_quantity_available(body, quantity, actor=actor, user=user)
        await self._audit(
            "paper_signal_accepted",
            actor=actor,
            user_id=user.id,
            payload={
                "ticker": body.ticker.upper(),
                "side": body.side,
                "quantity": str(quantity),
                "notional": str(body.notional) if body.notional is not None else None,
                "estimated_price": str(body.estimated_price),
                "venue": body.venue,
                "source": body.source,
                "strategy": body.strategy,
                "decision_code": "PAPER_SIGNAL_ACCEPTED",
            },
        )
        await self._run_risk_check(body, quantity, actor=actor, user=user)

        now = datetime.now(UTC)
        cash_used = quantity * body.estimated_price if body.side == "buy" else None
        order = Order(
            id=uuid.uuid4(),
            client_order_key=self._client_order_key(body, quantity),
            ticker=body.ticker.upper(),
            side=body.side,
            order_type=body.order_type,
            quantity=quantity,
            status=PAPER_ORDER_STATUS,
            filled_quantity=quantity,
            avg_fill_price=body.estimated_price,
            execution_environment=PAPER_EXECUTION_ENVIRONMENT,
            expected_fill_price=body.estimated_price,
            submitted_at=now,
            first_ack_at=now,
            filled_at=now,
            broker_latency_ms=0,
            fill_latency_ms=0,
            reconciliation_latency_ms=0,
            venue=body.venue,
            is_dry_run=True,
            cash_used=cash_used,
            available_cash_at_submission=Decimal("100000"),
            broker_request={
                "paper_only": True,
                "no_broker_order_sent": True,
                "ticker": body.ticker.upper(),
                "side": body.side,
                "quantity": str(quantity),
                "estimated_price": str(body.estimated_price),
                "source": body.source,
                "strategy": body.strategy,
            },
            broker_response={
                "paper_only": True,
                "mock_execution": True,
                "no_broker_order_sent": True,
                "status": "PAPER_FILLED",
            },
        )
        apply_order_execution_quality(order)
        self.db.add(order)
        await self.db.flush()
        await self._order_event(
            order.id,
            "paper_order_created",
            to_status=order.status,
            payload={"ticker": order.ticker, "side": order.side, "quantity": str(quantity)},
        )
        await self._audit(
            "paper_order_created",
            actor=actor,
            entity_type="order",
            entity_id=str(order.id),
            user_id=user.id,
            payload={
                "ticker": order.ticker,
                "side": order.side,
                "quantity": str(quantity),
                "venue": body.venue,
                "no_broker_order_sent": True,
            },
        )
        await self._order_event(
            order.id,
            "paper_fill_simulated",
            from_status="pending_intent",
            to_status=order.status,
            payload={"fill_price": str(body.estimated_price), "filled_quantity": str(quantity)},
        )
        await self._audit(
            "paper_fill_simulated",
            actor=actor,
            entity_type="order",
            entity_id=str(order.id),
            user_id=user.id,
            payload={
                "ticker": order.ticker,
                "fill_price": str(body.estimated_price),
                "filled_quantity": str(quantity),
                "no_broker_order_sent": True,
            },
        )
        await self._update_position(user=user, order=order, price=body.estimated_price, actor=actor)
        await self.db.flush()
        return order


async def paper_execution_summary(db: AsyncSession) -> dict[str, Any]:
    latest_order = (
        await db.execute(
            select(Order)
            .where(
                Order.is_dry_run.is_(True),
                Order.execution_environment == PAPER_EXECUTION_ENVIRONMENT,
            )
            .order_by(desc(Order.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    total_orders = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.is_dry_run == True,  # noqa: E712
                    Order.execution_environment == PAPER_EXECUTION_ENVIRONMENT,
                )
            )
        ).scalar_one()
    )
    latest_position_rows = (
        (
            await db.execute(
                select(PositionSnapshot)
                .join(BrokerConnection, PositionSnapshot.connection_id == BrokerConnection.id)
                .where(BrokerConnection.broker == PAPER_BROKER)
                .order_by(desc(PositionSnapshot.snapshotted_at))
            )
        )
        .scalars()
        .all()
    )
    latest_positions: dict[str, PositionSnapshot] = {}
    for row in latest_position_rows:
        latest_positions.setdefault(row.ticker.upper(), row)

    return {
        "paper_only": True,
        "enabled_in_mode": PAPER_ENVIRONMENT,
        "total_paper_orders": total_orders,
        "latest_paper_order_timestamp": latest_order.created_at if latest_order else None,
        "last_paper_execution_status": latest_order.status if latest_order else None,
        "open_paper_positions_count": sum(
            1 for row in latest_positions.values() if row.quantity > 0
        ),
        "safety_notes": [
            "Paper execution is local/mock only.",
            "No broker order sent.",
            "Global kill switch blocks paper simulation in this endpoint.",
        ],
    }
