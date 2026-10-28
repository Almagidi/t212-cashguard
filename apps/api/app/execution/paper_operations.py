"""Owner-bound replay for identified, database-only manual paper commands.

No broker/outbox/exactly-once claim. Legacy no-ID execution is separate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api.schemas import OrderOut
from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order, PaperOperationReceipt, User
from app.execution.paper_engine import PaperBusinessRefusal, PaperExecutionEngine

if TYPE_CHECKING:
    from app.api.schemas import PaperOrderCreate


def canonical_request(body: PaperOrderCreate) -> dict[str, Any]:
    """Version 1: full validated defaults/nulls and exact Decimal values."""
    values = body.model_dump(exclude={"operation_id"})
    for key, value in values.items():
        if isinstance(value, Decimal):
            fixed = format(value, "f")
            values[key] = fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    return values


def request_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class OperationResult:
    status_code: int
    payload: dict[str, Any]


def _unconfirmed() -> OperationResult:
    return OperationResult(
        503,
        {
            "detail": "Paper result is unconfirmed. Keep this operation ID and check its receipt or retry the same operation."
        },
    )


class PaperOperationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _receipt(
        self, owner: uuid.UUID, operation: uuid.UUID
    ) -> PaperOperationReceipt | None:
        return await self.db.scalar(
            select(PaperOperationReceipt).where(
                PaperOperationReceipt.user_id == owner,
                PaperOperationReceipt.operation_id == operation,
            )
        )

    async def _view(self, receipt: PaperOperationReceipt, *, replayed: bool) -> dict[str, Any]:
        order = await self.db.get(Order, receipt.order_id) if receipt.order_id else None
        if receipt.order_id is not None and order is None:
            raise RuntimeError("Retained paper receipt has no result")
        if order is not None:
            # Flush-generated timestamps may be expired; load them explicitly
            # before synchronous DTO validation, never lazy-load after commit.
            await self.db.refresh(order)
        return {
            "operation_id": str(receipt.operation_id),
            "operation_state": "recorded",
            "outcome": receipt.outcome,
            "recorded_at": receipt.completed_at.isoformat(),
            "original_http_status": receipt.http_status,
            "replayed": replayed,
            "decision_code": receipt.decision_code,
            "detail": receipt.detail,
            "order": OrderOut.model_validate(order).model_dump(mode="json") if order else None,
        }

    @staticmethod
    def _submit_result(view: dict[str, Any]) -> OperationResult:
        body = {
            **(view["order"] or {}),
            **{key: value for key, value in view.items() if key != "order"},
        }
        code = view["original_http_status"]
        if view["replayed"] and view["outcome"] == "order_recorded":
            code = 200
        return OperationResult(code, body)

    async def lookup(self, owner: uuid.UUID, operation: uuid.UUID) -> OperationResult:
        """SELECT only. Admin callers still supply their OWN authenticated ID."""
        receipt = await self._receipt(owner, operation)
        if receipt is None:
            return OperationResult(
                404, {"detail": "No committed receipt found; outcome remains unknown."}
            )
        return OperationResult(200, await self._view(receipt, replayed=True))

    async def _reconcile(self, owner: uuid.UUID, body: PaperOrderCreate) -> OperationResult:
        """A fresh connection may read evidence, never execute another command."""
        bind = self.db.bind
        if isinstance(bind, AsyncConnection):
            if bind.sync_connection is None:
                return _unconfirmed()
            bind = bind.engine.execution_options(**bind.sync_connection.get_execution_options())
        if body.operation_id is None:
            return _unconfirmed()
        if bind is None:
            return _unconfirmed()
        try:
            async with asyncio.timeout(2), AsyncSession(bind, expire_on_commit=False) as fresh:
                service = PaperOperationService(fresh)
                receipt = await service._receipt(owner, body.operation_id)
                if receipt is None:
                    return _unconfirmed()
                if receipt.canonical_request != canonical_request(body):
                    return OperationResult(
                        409, {"detail": "Operation ID is bound to different paper inputs."}
                    )
                return self._submit_result(await service._view(receipt, replayed=True))
        except Exception:
            return _unconfirmed()

    async def submit(self, body: PaperOrderCreate, *, user: User) -> OperationResult:
        if body.operation_id is None:
            raise ValueError("Identified paper command requires operation_id")
        owner = user.id
        if settings.APP_MODE != "mock":
            return OperationResult(
                403, {"detail": "Identified paper execution requires APP_MODE=mock."}
            )
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return _unconfirmed()
        try:
            async with asyncio.timeout(10):
                return await self._submit(body, owner)
        except asyncio.CancelledError:
            await self.db.rollback()
            raise
        except Exception:
            # A commit acknowledgement can be lost after the server committed.
            # Discard caller work; never execute from the error/recovery path.
            try:
                await self.db.rollback()
            except Exception:
                await self.db.close()
            return await self._reconcile(owner, body)

    async def _submit(self, body: PaperOrderCreate, owner: uuid.UUID) -> OperationResult:
        if body.operation_id is None:
            raise ValueError("Identified paper command requires operation_id")
        if await self.db.scalar(text("SHOW transaction_isolation")) != "read committed":
            return _unconfirmed()
        # Transaction-local bounds fit inside the existing 15-second HTTP budget.
        await self.db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await self.db.execute(text("SET LOCAL statement_timeout = '8s'"))
        user = await self.db.scalar(
            select(User)
            .where(User.id == owner)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if user is None or not user.is_active or not user.is_admin:
            await self.db.rollback()
            return OperationResult(403, {"detail": "Active paper administrator required."})
        # Separate statement after mutex acquisition: observe the previous holder's commit.
        prior = await self._receipt(owner, body.operation_id)
        canonical = canonical_request(body)
        if prior is not None:
            if prior.canonical_request != canonical or prior.request_version != 1:
                await self.db.rollback()
                return OperationResult(
                    409, {"detail": "Operation ID is bound to different paper inputs."}
                )
            result = self._submit_result(await self._view(prior, replayed=True))
            await self.db.rollback()  # release locks; no business writes on replay
            return result

        engine = PaperExecutionEngine(self.db)
        await engine._lock_paper_connection(user)
        safety = await self.db.scalar(
            select(AppSettings.id).where(AppSettings.id == 1).with_for_update(read=True)
        )
        order = None
        refusal = None
        try:
            if safety is None:
                self.db.add(
                    AuditLog(
                        user_id=owner,
                        action="paper_signal_rejected",
                        actor=user.email,
                        payload={
                            "paper_only": True,
                            "no_broker_order_sent": True,
                            "decision_code": "safety_settings_missing",
                        },
                    )
                )
                raise PaperBusinessRefusal(
                    "Paper execution requires initialized safety settings.",
                    "safety_settings_missing",
                )
            order = await engine.execute(body, user=user)
        except PaperBusinessRefusal as exc:
            refusal = exc

        now = datetime.now(UTC)
        receipt = PaperOperationReceipt(
            id=uuid.uuid4(),
            user_id=owner,
            operation_id=body.operation_id,
            account_domain=f"paper:mock:user:{owner}",
            request_version=1,
            canonical_request=canonical,
            request_digest=request_digest(canonical),
            outcome="blocked" if refusal else "order_recorded",
            http_status=refusal.status_code if refusal else 201,
            decision_code=refusal.decision_code if refusal else None,
            detail=refusal.reason if refusal else None,
            order_id=order.id if order else None,
            created_at=now,
            completed_at=now,
        )
        self.db.add(receipt)
        await self.db.flush()
        result = self._submit_result(await self._view(receipt, replayed=False))
        await self.db.commit()
        return result
