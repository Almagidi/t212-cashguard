"""Exchange-aware, strategy-scoped end-of-day flattening safety boundary."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import (
    AppSettings,
    AuditLog,
    EodFlattenOperation,
    Order,
    RiskEvent,
    Signal,
)
from app.execution.engine import ExecutionEngine
from app.execution.paper_engine import PAPER_EXECUTION_ENVIRONMENT
from app.execution.state_machine import ACTIVE_ORDER_STATUSES
from app.market_data.exchange_calendar import calendar_for_venue
from app.services.alert_service import AlertService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

EOD_DUE_WINDOW_MINUTES = 10

_EXCHANGE_BY_RUNTIME_VENUE = {
    "t212": "XNYS",
}


@dataclass(frozen=True, slots=True)
class EodDueWindow:
    """One bounded EOD opportunity for an exchange session."""

    exchange: str
    exchange_session_date: date
    cutoff_at_utc: datetime
    due_until_utc: datetime
    is_early_close: bool


def _parse_local_hhmm(value: str) -> time | None:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError):
        return None
    if parsed.strftime("%H:%M") != value:
        return None
    return parsed.time()


def eod_due_window(strategy: Any, now_utc: datetime) -> EodDueWindow | None:
    """Return the current strategy's bounded exchange-session EOD window.

    Runtime broker venues are mapped explicitly to an exchange calendar. Unknown
    venues, malformed settings, non-session days, naive instants, and timestamps
    outside the bounded window all fail closed.
    """

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        return None
    runtime_venue = str(getattr(strategy, "venue", "")).strip().lower()
    exchange = _EXCHANGE_BY_RUNTIME_VENUE.get(runtime_venue)
    if exchange is None:
        return None
    configured_close = _parse_local_hhmm(str(getattr(strategy, "session_end", "")))
    if configured_close is None:
        return None

    try:
        calendar = calendar_for_venue(exchange)
        exchange_tz = ZoneInfo(calendar.exchange_timezone)
        local_now = now_utc.astimezone(exchange_tz)
        sessions = calendar.expected_sessions(local_now.date(), local_now.date())
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if not sessions:
        return None
    session = sessions[0]
    configured_cutoff = datetime.combine(session.local_date, configured_close, exchange_tz)
    if configured_cutoff <= session.open_at:
        return None
    cutoff = min(configured_cutoff, session.close_at)
    due_until = cutoff + timedelta(minutes=EOD_DUE_WINDOW_MINUTES)
    if not cutoff <= local_now < due_until:
        return None
    return EodDueWindow(
        exchange=exchange,
        exchange_session_date=session.local_date,
        cutoff_at_utc=cutoff.astimezone(UTC),
        due_until_utc=due_until.astimezone(UTC),
        is_early_close=session.is_early_close,
    )


@dataclass(frozen=True, slots=True)
class _FlattenCandidate:
    strategy: Any
    window: EodDueWindow
    ticker: str
    quantity: Decimal

    @property
    def operation_identity(self) -> str:
        return ":".join(
            (
                "eod_flatten",
                str(self.strategy.id),
                self.strategy.venue.lower(),
                self.window.exchange_session_date.isoformat(),
                self.ticker,
            )
        )


class _AttributionError(ValueError):
    def __init__(self, ticker: str, reason: str) -> None:
        self.ticker = ticker.strip().upper() or "UNKNOWN"
        self.reason = reason
        super().__init__(reason)


def _filled_quantity(order: Order) -> Decimal:
    quantity = Decimal(order.quantity)
    if not quantity.is_finite() or quantity <= 0:
        raise _AttributionError(
            order.ticker,
            "Invalid persisted order quantity makes strategy attribution ambiguous.",
        )
    if order.filled_quantity is None:
        if order.status == "filled":
            raise _AttributionError(
                order.ticker,
                "A filled order without filled quantity makes strategy attribution ambiguous.",
            )
        return Decimal("0")
    raw_quantity = Decimal(order.filled_quantity)
    if not raw_quantity.is_finite():
        raise _AttributionError(
            order.ticker,
            "Non-finite filled quantity makes strategy attribution ambiguous.",
        )
    if raw_quantity < 0 or raw_quantity > quantity:
        raise _AttributionError(
            order.ticker,
            "Persisted filled quantity is outside the order quantity bounds.",
        )
    if order.status == "filled" and raw_quantity == 0:
        raise _AttributionError(
            order.ticker,
            "A filled order with zero filled quantity makes strategy attribution ambiguous.",
        )
    return raw_quantity


def _positive_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not parsed.is_finite():
        return Decimal("0")
    return max(parsed, Decimal("0"))


class EodFlattenService:
    """Flatten only reconciled strategy quantities once per exchange session."""

    OPERATION_KIND = "eod_flatten"

    def __init__(self, db: AsyncSession, broker: Any) -> None:
        self.db = db
        self.broker = broker

    @property
    def _execution_environment(self) -> str:
        return (
            PAPER_EXECUTION_ENVIRONMENT
            if settings.APP_MODE in {"mock", "paper"}
            else settings.APP_MODE
        )

    @property
    def _broker_account_scope(self) -> str | None:
        value = inspect.getattr_static(self.broker, "account_scope", None)
        return str(value) if value else None

    async def _kill_switch_active(self) -> bool | None:
        return cast(
            "bool | None",
            await self.db.scalar(select(AppSettings.kill_switch_active).where(AppSettings.id == 1)),
        )

    @staticmethod
    def _summary(*, reason: str | None = None) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "flattened": 0,
            "operations_created": 0,
            "manual_reconciliation_required": 0,
            "reentries_blocked": 0,
        }
        if reason is not None:
            summary["reason"] = reason
        return summary

    async def run(
        self,
        strategies: list[Any],
        *,
        now_utc: datetime,
    ) -> dict[str, Any]:
        summary = self._summary()
        kill_switch_active = await self._kill_switch_active()
        if kill_switch_active is None:
            return self._summary(reason="settings_missing")
        if kill_switch_active:
            return self._summary(reason="kill_switch")
        if settings.APP_MODE == "live":
            return self._summary(reason="live_eod_prohibited")

        due = [
            (strategy, window)
            for strategy in strategies
            if (window := eod_due_window(strategy, now_utc)) is not None
        ]
        if not due:
            return self._summary(reason="not_due")

        candidates: list[_FlattenCandidate] = []
        for strategy, window in due:
            await self._synchronize_strategy_operations(strategy, window)
            try:
                quantities = await self._strategy_net_quantities(strategy)
            except _AttributionError as exc:
                candidate = _FlattenCandidate(strategy, window, exc.ticker, Decimal("0"))
                existing = await self._existing_operation(candidate)
                if existing is None and await self._record_ambiguity(candidate, exc.reason):
                    summary["operations_created"] += 1
                    summary["manual_reconciliation_required"] += 1
                continue
            for ticker, quantity in quantities.items():
                if quantity == 0:
                    continue
                candidate = _FlattenCandidate(strategy, window, ticker, abs(quantity))
                existing = await self._existing_operation(candidate)
                if existing is not None:
                    if existing.status == "completed":
                        await self._record_reentry(existing, candidate)
                        summary["reentries_blocked"] += 1
                    elif existing.status in {"claimed", "intent_persisted"}:
                        await self._mark_existing_manual(
                            existing,
                            candidate,
                            "A prior EOD worker stopped after persisting the order intent; "
                            "automatic resubmission is blocked.",
                        )
                        summary["manual_reconciliation_required"] += 1
                    continue
                blocker = await self._prior_unresolved_operation(candidate)
                if blocker is not None:
                    if blocker.status in {"claimed", "intent_persisted"}:
                        await self._mark_existing_manual(
                            blocker,
                            candidate,
                            "A prior-session EOD operation has no authoritative terminal outcome; "
                            "new automatic sells are blocked until explicit reconciliation.",
                        )
                        summary["manual_reconciliation_required"] += 1
                    continue
                if quantity < 0:
                    created = await self._record_ambiguity(
                        candidate,
                        "Persisted strategy attribution is negative and cannot be reconciled safely.",
                    )
                    if created:
                        summary["operations_created"] += 1
                        summary["manual_reconciliation_required"] += 1
                    continue
                candidates.append(candidate)

        if not candidates:
            return summary

        if settings.APP_MODE in {"mock", "paper"}:
            reason = (
                "Paper EOD flatten is not wired to the authoritative paper position ledger; "
                "automatic simulation is blocked to avoid a false flatten result."
            )
            for candidate in candidates:
                if await self._record_ambiguity(candidate, reason):
                    summary["operations_created"] += 1
                    summary["manual_reconciliation_required"] += 1
            summary["reason"] = "paper_eod_unsupported"
            return summary

        if await self._kill_switch_active():
            summary["reason"] = "kill_switch"
            return summary

        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            reason = f"Broker position snapshot failed with {type(exc).__name__}."
            for candidate in candidates:
                created = await self._record_ambiguity(candidate, reason)
                if created:
                    summary["operations_created"] += 1
                    summary["manual_reconciliation_required"] += 1
            log.warning("eod_flatten.position_snapshot_failed", error_type=type(exc).__name__)
            return summary
        position_map: dict[str, list[dict[str, Any]]] = {}
        for raw_position in positions:
            ticker = str(raw_position.get("ticker", "")).strip().upper()
            if ticker:
                position_map.setdefault(ticker, []).append(raw_position)

        active_sell_tickers = {
            ticker.strip().upper()
            for ticker in (
                (
                    await self.db.execute(
                        select(Order.ticker).where(
                            Order.side == "sell",
                            Order.status.in_(ACTIVE_ORDER_STATUSES),
                            Order.execution_environment == self._execution_environment,
                            Order.broker_account_scope == self._broker_account_scope,
                        )
                    )
                )
                .scalars()
                .all()
            )
        }

        by_ticker: dict[str, list[_FlattenCandidate]] = {}
        for candidate in candidates:
            by_ticker.setdefault(candidate.ticker, []).append(candidate)

        executable: list[_FlattenCandidate] = []
        for ticker, ticker_candidates in by_ticker.items():
            ambiguity: str | None = None
            broker_rows = position_map.get(ticker, [])
            if ticker in active_sell_tickers:
                ambiguity = "An active sell order already exists for this ticker."
            elif len(broker_rows) != 1:
                ambiguity = (
                    "Broker position is missing."
                    if not broker_rows
                    else "Broker returned duplicate position rows."
                )
            else:
                broker_quantity = _positive_decimal(broker_rows[0].get("quantity"))
                max_sell_raw = broker_rows[0].get("maxSell")
                max_sell = (
                    broker_quantity if max_sell_raw is None else _positive_decimal(max_sell_raw)
                )
                available = min(broker_quantity, max_sell)
                required = sum(
                    (candidate.quantity for candidate in ticker_candidates), Decimal("0")
                )
                if available < required:
                    ambiguity = (
                        f"Attributable quantity {required} exceeds broker sellable quantity "
                        f"{available}."
                    )

            if ambiguity is not None:
                for candidate in ticker_candidates:
                    created = await self._record_ambiguity(candidate, ambiguity)
                    if created:
                        summary["operations_created"] += 1
                        summary["manual_reconciliation_required"] += 1
                continue
            executable.extend(ticker_candidates)

        for candidate in executable:
            operation, created = await self._claim_with_order_intent(candidate, position_map)
            if not created:
                continue
            summary["operations_created"] += 1
            order = operation.order
            if order is None:
                raise RuntimeError("EOD operation was committed without its order intent")
            try:
                order = await ExecutionEngine(self.db, self.broker).submit_order(order)
            except Exception as exc:
                operation.status = "manual_reconciliation_required"
                operation.requires_manual_reconciliation = True
                operation.details = {
                    **dict(operation.details or {}),
                    "failure_type": type(exc).__name__,
                    "reason": "Submission outcome requires manual reconciliation.",
                }
                await self._emit_manual_evidence(
                    candidate,
                    operation,
                    "EOD submission outcome is ambiguous and requires manual reconciliation.",
                )
                summary["manual_reconciliation_required"] += 1
                await self.db.commit()
                continue

            if order.status == "filled":
                operation.status = "completed"
                operation.requires_manual_reconciliation = False
                summary["flattened"] += 1
            elif order.status in {"submitted", "accepted", "partially_filled"}:
                operation.status = "submission_pending"
            else:
                operation.status = "manual_reconciliation_required"
                operation.requires_manual_reconciliation = True
                await self._emit_manual_evidence(
                    candidate,
                    operation,
                    f"EOD order reached {order.status!r}; automatic resubmission is blocked.",
                )
                summary["manual_reconciliation_required"] += 1
            operation.details = {
                **dict(operation.details or {}),
                "order_status": order.status,
                "broker_order_id": order.broker_order_id,
            }
            await self.db.commit()

        self.db.add(
            AuditLog(
                action="eod_flatten_executed",
                actor="eod_flatten_service",
                payload={
                    **summary,
                    "strategy_ids": sorted({str(strategy.id) for strategy, _window in due}),
                    "execution_environment": self._execution_environment,
                    "broker_account_scope": self._broker_account_scope,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await self.db.commit()
        log.info("eod_flatten.completed", **summary)
        return summary

    async def _strategy_net_quantities(self, strategy: Any) -> dict[str, Decimal]:
        quantities: dict[str, Decimal] = {}
        order_venue = "paper" if settings.APP_MODE in {"mock", "paper"} else strategy.venue
        signal_orders = (
            (
                await self.db.execute(
                    select(Order)
                    .join(Signal, Signal.id == Order.signal_id)
                    .where(
                        Signal.strategy_id == strategy.id,
                        Order.venue == order_venue,
                        Order.execution_environment == self._execution_environment,
                        Order.is_dry_run.is_(settings.APP_MODE in {"mock", "paper"}),
                    )
                )
            )
            .scalars()
            .all()
        )
        for order in signal_orders:
            if self._broker_account_scope is None:
                if _filled_quantity(order) > 0:
                    raise _AttributionError(
                        order.ticker,
                        "The active broker adapter has no authoritative account scope.",
                    )
                continue
            if order.broker_account_scope is None:
                if _filled_quantity(order) > 0:
                    raise _AttributionError(
                        order.ticker,
                        "A broker-backed fill has no authoritative account scope.",
                    )
                continue
            if order.broker_account_scope != self._broker_account_scope:
                continue
            if order.side not in {"buy", "sell"}:
                raise _AttributionError(
                    order.ticker,
                    f"Unsupported persisted order side {order.side!r} makes attribution ambiguous.",
                )
            filled = _filled_quantity(order)
            ticker = order.ticker.strip().upper()
            direction = Decimal("1") if order.side == "buy" else Decimal("-1")
            quantities[ticker] = quantities.get(ticker, Decimal("0")) + direction * filled

        operation_orders = (
            (
                await self.db.execute(
                    select(Order)
                    .join(EodFlattenOperation, EodFlattenOperation.order_id == Order.id)
                    .where(
                        EodFlattenOperation.strategy_id == strategy.id,
                        EodFlattenOperation.venue == strategy.venue,
                        EodFlattenOperation.execution_environment == self._execution_environment,
                        EodFlattenOperation.broker_account_scope == self._broker_account_scope,
                    )
                )
            )
            .scalars()
            .all()
        )
        for order in operation_orders:
            ticker = order.ticker.strip().upper()
            quantities[ticker] = quantities.get(ticker, Decimal("0")) - _filled_quantity(order)
        return quantities

    async def _existing_operation(self, candidate: _FlattenCandidate) -> EodFlattenOperation | None:
        return cast(
            "EodFlattenOperation | None",
            await self.db.scalar(
                select(EodFlattenOperation).where(
                    EodFlattenOperation.operation_kind == self.OPERATION_KIND,
                    EodFlattenOperation.strategy_id == candidate.strategy.id,
                    EodFlattenOperation.venue == candidate.strategy.venue,
                    EodFlattenOperation.exchange_session_date
                    == candidate.window.exchange_session_date,
                    EodFlattenOperation.ticker == candidate.ticker,
                )
            ),
        )

    async def _prior_unresolved_operation(
        self, candidate: _FlattenCandidate
    ) -> EodFlattenOperation | None:
        return cast(
            "EodFlattenOperation | None",
            await self.db.scalar(
                select(EodFlattenOperation)
                .where(
                    EodFlattenOperation.operation_kind == self.OPERATION_KIND,
                    EodFlattenOperation.strategy_id == candidate.strategy.id,
                    EodFlattenOperation.venue == candidate.strategy.venue,
                    EodFlattenOperation.ticker == candidate.ticker,
                    EodFlattenOperation.execution_environment == self._execution_environment,
                    EodFlattenOperation.broker_account_scope == self._broker_account_scope,
                    EodFlattenOperation.exchange_session_date
                    < candidate.window.exchange_session_date,
                    EodFlattenOperation.status != "completed",
                )
                .order_by(EodFlattenOperation.exchange_session_date)
                .limit(1)
            ),
        )

    def _new_operation(
        self,
        candidate: _FlattenCandidate,
        *,
        status: str,
        manual: bool,
        details: dict[str, Any] | None = None,
    ) -> EodFlattenOperation:
        return EodFlattenOperation(
            id=uuid.uuid4(),
            operation_kind=self.OPERATION_KIND,
            strategy_id=candidate.strategy.id,
            venue=candidate.strategy.venue,
            exchange=candidate.window.exchange,
            execution_environment=self._execution_environment,
            broker_account_scope=self._broker_account_scope,
            exchange_session_date=candidate.window.exchange_session_date,
            ticker=candidate.ticker,
            attributable_quantity=candidate.quantity,
            status=status,
            requires_manual_reconciliation=manual,
            details=details,
        )

    async def _insert_unique_operation(
        self, operation: EodFlattenOperation, candidate: _FlattenCandidate
    ) -> tuple[EodFlattenOperation, bool]:
        try:
            async with self.db.begin_nested():
                self.db.add(operation)
                await self.db.flush()
        except IntegrityError:
            existing = await self._existing_operation(candidate)
            if existing is None:
                raise
            return existing, False
        return operation, True

    async def _claim_with_order_intent(
        self,
        candidate: _FlattenCandidate,
        position_map: dict[str, list[dict[str, Any]]],
    ) -> tuple[EodFlattenOperation, bool]:
        operation = self._new_operation(candidate, status="claimed", manual=False)
        operation, created = await self._insert_unique_operation(operation, candidate)
        if not created:
            return operation, False

        broker_position = position_map[candidate.ticker][0]
        estimated_price = _positive_decimal(
            broker_position.get("currentPrice") or broker_position.get("averagePrice")
        )
        order = await ExecutionEngine(self.db, self.broker).create_order_intent(
            ticker=candidate.ticker,
            side="sell",
            order_type="market",
            quantity=candidate.quantity,
            is_dry_run=False,
            estimated_price=estimated_price or None,
            venue=candidate.strategy.venue,
            stable_operation_identity=candidate.operation_identity,
        )
        operation.order = order
        operation.status = "intent_persisted"
        operation.details = {"operation_identity": candidate.operation_identity}
        self.db.add(
            AuditLog(
                action="eod_flatten_intent_persisted",
                entity_type="order",
                entity_id=str(order.id),
                actor="eod_flatten_service",
                payload={
                    "strategy_id": str(candidate.strategy.id),
                    "venue": candidate.strategy.venue,
                    "exchange": candidate.window.exchange,
                    "exchange_session_date": candidate.window.exchange_session_date.isoformat(),
                    "ticker": candidate.ticker,
                    "quantity": str(candidate.quantity),
                },
                occurred_at=datetime.now(UTC),
            )
        )
        # The durable claim and order intent must exist before any broker transmission.
        await self.db.commit()
        return operation, True

    async def _synchronize_strategy_operations(self, strategy: Any, window: EodDueWindow) -> None:
        operations = (
            (
                await self.db.execute(
                    select(EodFlattenOperation).where(
                        EodFlattenOperation.operation_kind == self.OPERATION_KIND,
                        EodFlattenOperation.strategy_id == strategy.id,
                        EodFlattenOperation.venue == strategy.venue,
                        EodFlattenOperation.execution_environment == self._execution_environment,
                        EodFlattenOperation.broker_account_scope == self._broker_account_scope,
                        EodFlattenOperation.status.in_(
                            {"submission_pending", "claimed", "intent_persisted"}
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        changed = False
        for operation in operations:
            if operation.status in {"claimed", "intent_persisted"} or operation.order_id is None:
                continue
            order = await self.db.scalar(
                select(Order)
                .where(Order.id == operation.order_id)
                .execution_options(populate_existing=True)
            )
            if order is None:
                operation.status = "manual_reconciliation_required"
                operation.requires_manual_reconciliation = True
                operation.details = {
                    **dict(operation.details or {}),
                    "reason": "The linked EOD order is missing and requires reconciliation.",
                }
                await self._emit_manual_evidence_for_operation(
                    operation,
                    strategy,
                    "The linked EOD order is missing and requires reconciliation.",
                )
                changed = True
            elif order.status == "filled":
                try:
                    _filled_quantity(order)
                except _AttributionError as exc:
                    operation.status = "manual_reconciliation_required"
                    operation.requires_manual_reconciliation = True
                    operation.details = {**dict(operation.details or {}), "reason": exc.reason}
                    await self._emit_manual_evidence_for_operation(operation, strategy, exc.reason)
                else:
                    operation.status = "completed"
                    operation.requires_manual_reconciliation = False
                    operation.details = {
                        **dict(operation.details or {}),
                        "order_status": "filled",
                    }
                changed = True
            elif order.status in {"cancelled", "rejected", "error"}:
                reason = (
                    f"The linked EOD order reached terminal status {order.status!r}; "
                    "manual reconciliation is required."
                )
                operation.status = "manual_reconciliation_required"
                operation.requires_manual_reconciliation = True
                operation.details = {**dict(operation.details or {}), "reason": reason}
                await self._emit_manual_evidence_for_operation(operation, strategy, reason)
                changed = True
        if changed:
            await self.db.commit()

    async def _record_ambiguity(self, candidate: _FlattenCandidate, reason: str) -> bool:
        operation = self._new_operation(
            candidate,
            status="manual_reconciliation_required",
            manual=True,
            details={"reason": reason},
        )
        operation, created = await self._insert_unique_operation(operation, candidate)
        if not created:
            return False
        await self._emit_manual_evidence(candidate, operation, reason)
        await self.db.commit()
        return True

    async def _record_reentry(
        self,
        operation: EodFlattenOperation,
        candidate: _FlattenCandidate,
    ) -> None:
        operation.status = "reentry_blocked"
        operation.requires_manual_reconciliation = True
        operation.details = {
            **dict(operation.details or {}),
            "reentry_quantity": str(candidate.quantity),
            "reason": "Positive strategy-attributable quantity reappeared in the same session.",
        }
        await self._emit_manual_evidence(
            candidate,
            operation,
            "Same-session position re-entry detected after completed EOD flatten.",
        )
        await self.db.commit()

    async def _mark_existing_manual(
        self,
        operation: EodFlattenOperation,
        candidate: _FlattenCandidate,
        reason: str,
    ) -> None:
        operation.status = "manual_reconciliation_required"
        operation.requires_manual_reconciliation = True
        operation.details = {**dict(operation.details or {}), "reason": reason}
        await self._emit_manual_evidence(candidate, operation, reason)
        await self.db.commit()

    async def _emit_manual_evidence(
        self,
        candidate: _FlattenCandidate,
        operation: EodFlattenOperation,
        reason: str,
    ) -> None:
        await self._emit_manual_evidence_for_operation(
            operation,
            candidate.strategy,
            reason,
            attributable_quantity=candidate.quantity,
        )

    async def _emit_manual_evidence_for_operation(
        self,
        operation: EodFlattenOperation,
        strategy: Any,
        reason: str,
        *,
        attributable_quantity: Decimal | None = None,
    ) -> None:
        payload = {
            "operation_id": str(operation.id),
            "strategy_id": str(strategy.id),
            "venue": operation.venue,
            "exchange": operation.exchange,
            "exchange_session_date": operation.exchange_session_date.isoformat(),
            "execution_environment": operation.execution_environment,
            "broker_account_scope": operation.broker_account_scope,
            "ticker": operation.ticker,
            "attributable_quantity": str(
                operation.attributable_quantity
                if attributable_quantity is None
                else attributable_quantity
            ),
            "manual_reconciliation_required": True,
            "reason": reason,
        }
        self.db.add(
            RiskEvent(
                id=uuid.uuid4(),
                event_type="eod_flatten_manual_reconciliation",
                ticker=operation.ticker,
                order_id=operation.order_id,
                message=reason,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )
        await AlertService(self.db).send(
            alert_type="eod_flatten_manual_reconciliation",
            title=f"EOD flatten blocked: {operation.ticker}",
            message=reason,
            severity="critical",
            payload=payload,
        )
