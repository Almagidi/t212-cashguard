"""Paper-only execution lifecycle.

This module deliberately has no dependency on broker adapters. It persists a
local paper order, simulates a fill, updates a local paper position snapshot,
and writes audit entries. The global kill switch is still enforced.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select

from app.core.config import settings
from app.db.models import (
    AuditLog,
    BrokerAccountSnapshot,
    BrokerConnection,
    Order,
    OrderEvent,
    PositionSnapshot,
    Trade,
    User,
)
from app.execution.paper_policy import PaperFillDecision, evaluate_paper_fill
from app.execution.state_machine import transition_order_status_with_evidence
from app.risk.engine import RiskEngine, RiskViolation
from app.services.execution_quality import apply_order_execution_quality, milliseconds_between

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.schemas import PaperOrderCreate


PAPER_BROKER = "paper"
PAPER_ENVIRONMENT = "mock"
PAPER_EXECUTION_ENVIRONMENT = "paper_mock"
PAPER_SUPPORTED_VENUES = {"paper", "mock"}
PAPER_STARTING_CASH = Decimal("100000")
PAPER_CASH_QUANTUM = Decimal("0.00000001")
PAPER_MAX_QUANTITY = Decimal("999999999999.99999999")


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
                    "no_broker_order_sent": True,
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
                payload={
                    "paper_only": True,
                    "no_broker_order_sent": True,
                    **(payload or {}),
                },
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

        # The User row exists before any paper ledger state and is therefore
        # the stable first-use mutex. Recheck after acquiring it so every
        # create-if-missing path (including portfolio reads) uses one order.
        await self.db.execute(select(User.id).where(User.id == user.id).with_for_update())
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

    async def _lock_paper_connection(self, user: User) -> BrokerConnection:
        """Serialize ledger mutations for one user's local paper account."""
        connection = await self._paper_connection(user)
        return (
            await self.db.execute(
                select(BrokerConnection)
                .where(BrokerConnection.id == connection.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _latest_paper_positions(
        self,
        user: User,
    ) -> dict[str, PositionSnapshot]:
        connection = await self._paper_connection(user)
        result = await self.db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.connection_id == connection.id)
            .order_by(desc(PositionSnapshot.snapshotted_at), desc(PositionSnapshot.id))
        )
        latest: dict[str, PositionSnapshot] = {}
        for snapshot in result.scalars().all():
            ticker = snapshot.ticker.upper()
            latest.setdefault(ticker, snapshot)
        return latest

    async def _latest_paper_account(self, user: User) -> BrokerAccountSnapshot | None:
        connection = await self._paper_connection(user)
        result = await self.db.execute(
            select(BrokerAccountSnapshot)
            .where(BrokerAccountSnapshot.connection_id == connection.id)
            .order_by(desc(BrokerAccountSnapshot.snapshotted_at), desc(BrokerAccountSnapshot.id))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def current_open_paper_positions_count(self, user: User) -> int:
        latest = await self._latest_paper_positions(user)
        return sum(1 for position in latest.values() if position.quantity > 0)

    async def portfolio_state(self, user: User) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
        account = await self._latest_paper_account(user)
        latest_positions = await self._latest_paper_positions(user)
        positions = [
            {
                "ticker": position.ticker.upper(),
                "quantity": position.quantity,
                "averagePrice": position.avg_price,
                "currentPrice": position.current_price,
                "maxSell": position.quantity_available or position.quantity,
            }
            for position in latest_positions.values()
            if position.quantity > 0
        ]
        return (
            {
                "free": account.cash if account is not None else PAPER_STARTING_CASH,
                "total": account.total_value if account is not None else PAPER_STARTING_CASH,
            },
            positions,
        )

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
        estimated_price: Decimal,
        available_cash: Decimal,
        account_value: Decimal,
    ) -> None:
        open_positions = await self.current_open_paper_positions_count(user)
        risk = RiskEngine(self.db)
        try:
            await risk.run_all_checks(
                ticker=body.ticker.upper(),
                side=body.side,
                quantity=quantity,
                estimated_price=estimated_price,
                available_cash=available_cash,
                account_value=account_value,
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

    async def _apply_fill_effects(
        self,
        *,
        user: User,
        order: Order,
        decision: PaperFillDecision,
        actor: str,
    ) -> PositionSnapshot:
        if decision.fill_price is None or decision.filled_quantity <= 0:
            raise ValueError("filled paper effects require a positive fill")

        connection = await self._paper_connection(user)
        latest_positions = await self._latest_paper_positions(user)
        previous = latest_positions.get(order.ticker)
        previous_quantity = previous.quantity if previous is not None else Decimal("0")
        previous_avg = previous.avg_price if previous is not None else decision.fill_price
        filled_quantity = decision.filled_quantity
        gross_value = decision.fill_price * filled_quantity
        previous_account = await self._latest_paper_account(user)
        cash_before = previous_account.cash if previous_account else PAPER_STARTING_CASH
        cash_delta = (
            -(gross_value + decision.fee_amount)
            if order.side == "buy"
            else gross_value - decision.fee_amount
        )
        cash = (cash_before + cash_delta).quantize(PAPER_CASH_QUANTUM)
        if cash < 0:
            raise PaperExecutionError("Paper cash guard blocks a negative cash balance.")

        position_opened_at = datetime.now(UTC)
        position_open_order_id: uuid.UUID | None = None
        if previous is not None and previous_quantity > 0:
            raw = previous.raw or {}
            try:
                position_opened_at = datetime.fromisoformat(
                    str(raw.get("position_opened_at", previous.snapshotted_at.isoformat()))
                )
                if raw.get("position_open_order_id"):
                    existing_open_order_id = uuid.UUID(str(raw["position_open_order_id"]))
                    if order.side == "sell" or existing_open_order_id == order.id:
                        position_open_order_id = existing_open_order_id
            except ValueError:
                position_opened_at = previous.snapshotted_at
        elif order.side == "buy":
            position_open_order_id = order.id

        if order.side == "buy":
            new_quantity = previous_quantity + filled_quantity
            new_avg = (
                ((previous_quantity * previous_avg) + gross_value + decision.fee_amount)
                / new_quantity
                if new_quantity > 0
                else decision.fill_price
            )
        else:
            new_quantity = max(Decimal("0"), previous_quantity - filled_quantity)
            new_avg = previous_avg

            trade = Trade(
                id=uuid.uuid4(),
                ticker=order.ticker,
                side="sell",
                open_order_id=position_open_order_id,
                close_order_id=order.id,
                quantity=filled_quantity,
                open_price=previous_avg,
                close_price=decision.fill_price,
                realized_pnl=(
                    (decision.fill_price - previous_avg) * filled_quantity - decision.fee_amount
                ).quantize(PAPER_CASH_QUANTUM),
                opened_at=position_opened_at,
                closed_at=datetime.now(UTC),
                is_dry_run=True,
            )
            self.db.add(trade)
            await self._audit(
                "paper_trade_closed",
                actor=actor,
                entity_type="trade",
                entity_id=str(trade.id),
                user_id=user.id,
                payload={"ticker": order.ticker, "order_id": str(order.id)},
            )

        unrealized = ((decision.quote_price - new_avg) * new_quantity).quantize(PAPER_CASH_QUANTUM)

        snapshot = PositionSnapshot(
            id=uuid.uuid4(),
            connection_id=connection.id,
            ticker=order.ticker,
            quantity=new_quantity,
            avg_price=new_avg,
            current_price=decision.quote_price,
            unrealized_pnl=unrealized,
            quantity_available=new_quantity,
            raw={
                "paper_only": True,
                "no_broker_order_sent": True,
                "source_order_id": str(order.id),
                "position_open_order_id": (
                    str(position_open_order_id) if position_open_order_id else None
                ),
                "position_opened_at": position_opened_at.isoformat(),
                "side": order.side,
                "simulated_fill_price": str(decision.fill_price),
                "fee_amount": str(decision.fee_amount),
                "simulation_profile": decision.profile,
                "previous_quantity": str(previous_quantity),
                "new_quantity": str(new_quantity),
            },
            snapshotted_at=datetime.now(UTC),
        )
        self.db.add(snapshot)
        await self.db.flush()

        current_positions = await self._latest_paper_positions(user)
        invested = sum(
            (
                row.quantity
                * (row.current_price if row.current_price is not None else row.avg_price)
                for row in current_positions.values()
            ),
            Decimal("0"),
        ).quantize(PAPER_CASH_QUANTUM)
        total_value = (cash + invested).quantize(PAPER_CASH_QUANTUM)
        account = BrokerAccountSnapshot(
            id=uuid.uuid4(),
            connection_id=connection.id,
            total_value=total_value,
            cash=cash,
            free_funds=cash,
            invested=invested,
            result=(total_value - PAPER_STARTING_CASH).quantize(PAPER_CASH_QUANTUM),
            currency="USD",
            raw={
                "paper_only": True,
                "no_broker_order_sent": True,
                "source_order_id": str(order.id),
                "cash_delta": str(cash_delta),
                "simulation_profile": decision.profile,
            },
            snapshotted_at=datetime.now(UTC),
        )
        self.db.add(account)
        await self._audit(
            "paper_account_updated",
            actor=actor,
            entity_type="broker_account",
            entity_id=str(account.id),
            user_id=user.id,
            payload={"cash": str(cash), "invested": str(invested), "order_id": str(order.id)},
        )
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
                "unrealized_pnl": str(unrealized),
                "order_id": str(order.id),
            },
        )
        return snapshot

    async def execute(
        self,
        body: PaperOrderCreate,
        *,
        user: User,
        signal_id: uuid.UUID | None = None,
    ) -> Order:
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

        paper_connection = await self._lock_paper_connection(user)

        if body.quantity is not None:
            quantity = body.quantity
        else:
            if body.notional is None:
                raise PaperExecutionError("quantity or notional is required")
            quantity = body.notional / body.estimated_price
        quantity = quantity.quantize(PAPER_CASH_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= 0 or quantity > PAPER_MAX_QUANTITY:
            raise PaperExecutionError("Paper quantity is outside persistence range.")
        await self._check_sell_quantity_available(body, quantity, actor=actor, user=user)
        decision = evaluate_paper_fill(
            side=body.side,
            quantity=quantity,
            quote_price=body.estimated_price,
            profile=body.simulation_profile,
        )
        latest_account = await self._latest_paper_account(user)
        available_cash = latest_account.cash if latest_account else PAPER_STARTING_CASH
        account_value = latest_account.total_value if latest_account else PAPER_STARTING_CASH
        gross_value = (
            decision.fill_price * decision.filled_quantity
            if decision.fill_price is not None
            else Decimal("0")
        )
        risk_price = decision.fill_price or decision.quote_price
        if body.side == "buy" and decision.filled_quantity > 0:
            risk_price = (gross_value + decision.fee_amount) / decision.filled_quantity
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
        await self._run_risk_check(
            body,
            quantity,
            actor=actor,
            user=user,
            estimated_price=risk_price,
            available_cash=available_cash,
            account_value=account_value,
        )

        now = datetime.now(UTC)
        cash_used = (
            (gross_value + decision.fee_amount).quantize(PAPER_CASH_QUANTUM)
            if body.side == "buy"
            else None
        )
        order = Order(
            id=uuid.uuid4(),
            signal_id=signal_id,
            client_order_key=self._client_order_key(body, quantity),
            ticker=body.ticker.upper(),
            side=body.side,
            order_type=body.order_type,
            quantity=quantity,
            status="pending_intent",
            execution_environment=PAPER_EXECUTION_ENVIRONMENT,
            broker_account_scope=f"paper:mock:user:{user.id}",
            expected_fill_price=body.estimated_price,
            fee_amount=decision.fee_amount,
            venue=body.venue,
            is_dry_run=True,
            cash_used=cash_used,
            available_cash_at_submission=available_cash,
            broker_request={
                "paper_only": True,
                "no_broker_order_sent": True,
                "ticker": body.ticker.upper(),
                "side": body.side,
                "quantity": str(quantity),
                "estimated_price": str(body.estimated_price),
                "source": body.source,
                "strategy": body.strategy,
                "simulation_profile": decision.profile,
                "paper_connection_id": str(paper_connection.id),
            },
        )
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
        transition_order_status_with_evidence(
            self.db,
            order,
            "submitted",
            event_type="paper_order_submitted",
            reason="paper order accepted locally",
            actor=actor,
            correlation_id=order.client_order_key,
        )
        order.submitted_at = now

        if decision.outcome == "rejected":
            transition_order_status_with_evidence(
                self.db,
                order,
                "rejected",
                event_type="paper_execution_rejected",
                reason="paper execution policy rejected the order",
                actor=actor,
                correlation_id=order.client_order_key,
                payload={
                    "rejection_code": decision.rejection_code,
                    "simulation_profile": decision.profile,
                },
            )
            order.filled_quantity = Decimal("0")
            order.error_message = decision.rejection_code
            order.rejected_at = now
            order.first_ack_at = now + timedelta(milliseconds=decision.fill_latency_ms)
            order.broker_latency_ms = decision.fill_latency_ms
            order.reconciliation_latency_ms = decision.fill_latency_ms
            order.broker_response = {
                "paper_only": True,
                "mock_execution": True,
                "no_broker_order_sent": True,
                "status": "PAPER_REJECTED",
                "rejection_code": decision.rejection_code,
                "simulation_profile": decision.profile,
            }
            apply_order_execution_quality(order)
            await self._audit(
                "paper_execution_rejected",
                actor=actor,
                entity_type="order",
                entity_id=str(order.id),
                user_id=user.id,
                payload={
                    "ticker": order.ticker,
                    "rejection_code": decision.rejection_code,
                    "simulation_profile": decision.profile,
                },
            )
            await self.db.flush()
            return order

        terminal_status = decision.outcome
        transition_order_status_with_evidence(
            self.db,
            order,
            terminal_status,
            event_type="paper_fill_simulated",
            reason="paper fill simulated locally",
            actor=actor,
            correlation_id=order.client_order_key,
            payload={
                "fill_price": str(decision.fill_price),
                "filled_quantity": str(decision.filled_quantity),
                "fee_amount": str(decision.fee_amount),
                "simulation_profile": decision.profile,
            },
        )
        order.filled_quantity = decision.filled_quantity
        order.avg_fill_price = decision.fill_price
        order.first_ack_at = now + timedelta(milliseconds=decision.fill_latency_ms)
        order.filled_at = order.first_ack_at if terminal_status == "filled" else None
        order.broker_latency_ms = decision.fill_latency_ms
        order.fill_latency_ms = decision.fill_latency_ms
        order.reconciliation_latency_ms = decision.fill_latency_ms
        order.broker_response = {
            "paper_only": True,
            "mock_execution": True,
            "no_broker_order_sent": True,
            "status": ("PAPER_FILLED" if terminal_status == "filled" else "PAPER_PARTIALLY_FILLED"),
            "spread_bps": str(decision.spread_bps),
            "slippage_bps": str(decision.slippage_bps),
            "fee_amount": str(decision.fee_amount),
            "simulation_profile": decision.profile,
        }
        apply_order_execution_quality(order)
        await self._audit(
            "paper_fill_simulated",
            actor=actor,
            entity_type="order",
            entity_id=str(order.id),
            user_id=user.id,
            payload={
                "ticker": order.ticker,
                "fill_price": str(decision.fill_price),
                "filled_quantity": str(decision.filled_quantity),
                "fee_amount": str(decision.fee_amount),
                "no_broker_order_sent": True,
            },
        )
        await self._apply_fill_effects(user=user, order=order, decision=decision, actor=actor)
        await self.db.flush()
        return order

    async def fill_partial_order(
        self,
        order: Order,
        *,
        quantity: Decimal,
        estimated_price: Decimal,
        user: User,
    ) -> Order:
        """Apply one deterministic mock fill without exceeding the live remainder."""
        if settings.APP_MODE != "mock":
            raise PaperExecutionError(
                "Paper follow-up fills require APP_MODE=mock.", status_code=403
            )

        paper_connection = await self._lock_paper_connection(user)
        locked = (
            await self.db.execute(
                select(Order)
                .where(Order.id == order.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if (
            not locked.is_dry_run
            or locked.execution_environment != PAPER_EXECUTION_ENVIRONMENT
            or locked.status != "partially_filled"
        ):
            raise PaperExecutionError("Only an active partial paper order can receive a fill.")
        if str((locked.broker_request or {}).get("paper_connection_id", "")) != str(
            paper_connection.id
        ):
            raise PaperExecutionError("Partial paper order does not belong to this paper account.")

        quantity = quantity.quantize(PAPER_CASH_QUANTUM, rounding=ROUND_DOWN)
        remaining_before = locked.remaining_quantity
        if quantity <= 0 or quantity > remaining_before:
            raise PaperExecutionError(
                f"Follow-up fill quantity must be positive and at most remaining quantity "
                f"{remaining_before}."
            )

        decision = evaluate_paper_fill(
            side=locked.side,
            quantity=quantity,
            quote_price=estimated_price,
            profile="standard",
        )
        if (
            decision.outcome != "filled"
            or decision.fill_price is None
            or decision.filled_quantity != quantity
        ):
            raise PaperExecutionError("Deterministic follow-up fill policy did not fill exactly.")

        latest_account = await self._latest_paper_account(user)
        available_cash = latest_account.cash if latest_account else PAPER_STARTING_CASH
        required_cash = (
            decision.fill_price * decision.filled_quantity + decision.fee_amount
        ).quantize(PAPER_CASH_QUANTUM)
        if locked.side == "buy" and required_cash > available_cash:
            await self._audit(
                "paper_follow_up_fill_blocked",
                actor=user.email,
                entity_type="order",
                entity_id=str(locked.id),
                user_id=user.id,
                payload={
                    "reason": "paper_cash_guard",
                    "required_cash": str(required_cash),
                    "available_cash": str(available_cash),
                    "remaining_quantity": str(remaining_before),
                },
            )
            raise PaperExecutionError("Paper follow-up fill blocked by cash guard.")

        previous_filled = locked.filled_quantity or Decimal("0")
        previous_avg = locked.avg_fill_price or decision.fill_price
        cumulative_filled = previous_filled + decision.filled_quantity
        remaining_after = locked.quantity - cumulative_filled
        if remaining_after < 0:
            raise PaperExecutionError("Follow-up fill exceeds remaining quantity.")

        await self._apply_fill_effects(
            user=user,
            order=locked,
            decision=decision,
            actor=user.email,
        )
        cumulative_avg = (
            (previous_avg * previous_filled) + (decision.fill_price * decision.filled_quantity)
        ) / cumulative_filled
        cumulative_fee = (locked.fee_amount or Decimal("0")) + decision.fee_amount
        now = datetime.now(UTC)
        target_status = "filled" if remaining_after == 0 else "partially_filled"
        event_payload = {
            "incremental_filled_quantity": str(decision.filled_quantity),
            "cumulative_filled_quantity": str(cumulative_filled),
            "remaining_quantity": str(remaining_after),
            "fill_price": str(decision.fill_price),
            "incremental_fee_amount": str(decision.fee_amount),
            "cumulative_fee_amount": str(cumulative_fee),
            "paper_only": True,
            "no_broker_order_sent": True,
        }
        if target_status == "filled":
            transition_order_status_with_evidence(
                self.db,
                locked,
                "filled",
                event_type="paper_follow_up_fill",
                reason="paper remainder filled locally",
                actor=user.email,
                correlation_id=locked.client_order_key,
                payload=event_payload,
            )
            locked.filled_at = now
        else:
            await self._order_event(
                locked.id,
                "paper_follow_up_fill",
                from_status="partially_filled",
                to_status="partially_filled",
                payload=event_payload,
            )

        locked.filled_quantity = cumulative_filled
        locked.avg_fill_price = cumulative_avg.quantize(PAPER_CASH_QUANTUM)
        locked.fee_amount = cumulative_fee.quantize(PAPER_CASH_QUANTUM)
        if locked.side == "buy":
            incremental_cash = (
                decision.fill_price * decision.filled_quantity + decision.fee_amount
            ).quantize(PAPER_CASH_QUANTUM)
            locked.cash_used = (locked.cash_used or Decimal("0")) + incremental_cash
        locked.last_reconciled_at = now
        latest_fill_latency = milliseconds_between(locked.submitted_at, now) or 0
        locked.fill_latency_ms = max(locked.fill_latency_ms or 0, latest_fill_latency)
        locked.reconciliation_latency_ms = max(
            locked.reconciliation_latency_ms or 0,
            latest_fill_latency,
        )
        locked.execution_quality_notes = None
        locked.slippage_pct = None
        locked.slippage_value = None
        locked.broker_response = {
            **dict(locked.broker_response or {}),
            "status": ("PAPER_FILLED" if target_status == "filled" else "PAPER_PARTIALLY_FILLED"),
            "cumulative_filled_quantity": str(cumulative_filled),
            "remaining_quantity": str(remaining_after),
            "fee_amount": str(locked.fee_amount),
            "last_incremental_fill_quantity": str(decision.filled_quantity),
            "last_incremental_fill_price": str(decision.fill_price),
            "paper_only": True,
            "mock_execution": True,
            "no_broker_order_sent": True,
        }
        apply_order_execution_quality(locked)
        await self._audit(
            "paper_follow_up_fill",
            actor=user.email,
            entity_type="order",
            entity_id=str(locked.id),
            user_id=user.id,
            payload=event_payload,
        )
        await self.db.flush()
        return locked


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
        "no_broker_order_sent": True,
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
