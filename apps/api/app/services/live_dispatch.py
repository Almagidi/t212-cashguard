"""Account-bound authorization at intent creation and dispatch.

The attempt commit is the authorization boundary. A later last-mile read can
refuse an unsent request, but cannot recall an already dispatched broker call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import Order, OrderEvent
from app.market_data.live_source import LiveMarketSourceError, resolve_live_source
from app.services.live_account import LiveAccountError, binding_for_adapter
from app.services.live_readiness import LiveReadinessService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class LiveDispatchError(RuntimeError):
    """Selected transport, intent or approval no longer matches."""


async def capture_intent_context(db: AsyncSession, broker: Any, *, side: str) -> dict[str, Any]:
    try:
        binding, _ = await binding_for_adapter(db, broker)
        source = resolve_live_source() if side == "buy" else None
    except (LiveAccountError, LiveMarketSourceError) as exc:
        raise LiveDispatchError(str(exc)) from exc
    return {"schema": 1, "binding": binding.payload(), "market_source": source}


async def authorize_dispatch(
    db: AsyncSession,
    broker: Any,
    order: Order,
    *,
    lock: bool = True,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        binding, connection = await binding_for_adapter(db, broker, lock=lock)
        if order.execution_environment != "live" or order.venue != "t212":
            raise LiveDispatchError("Live order venue or environment is not attributable.")
        if order.broker_account_scope != binding.account_scope:
            raise LiveDispatchError("Live order belongs to a different account.")
        events = list(
            (
                await db.execute(
                    select(OrderEvent).where(
                        OrderEvent.order_id == order.id,
                        OrderEvent.event_type == "live_account_bound",
                    )
                )
            ).scalars()
        )
        source = resolve_live_source() if order.side == "buy" else None
        wanted = {"schema": 1, "binding": binding.payload(), "market_source": source}
        if len(events) != 1 or events[0].payload != wanted:
            raise LiveDispatchError(
                "Live intent context changed or is unbound; it cannot be rebound."
            )
        service = LiveReadinessService(db, binding=binding, lock=lock)
        readiness = await service.evaluate(require_market_data=order.side == "buy")
        if not readiness["ready_for_live"] or service.current_head_id is None:
            raise LiveDispatchError("Bound live readiness is incomplete or unavailable.")
        authorization = {
            "binding": binding.payload(),
            "head_id": str(service.current_head_id),
            "broker_test_id": str(connection.last_test_audit_id),
            "market_source": source,
        }
        if expected is not None and authorization != expected:
            raise LiveDispatchError("Live authorization changed after dispatch admission.")
        return authorization
    except (LiveAccountError, LiveMarketSourceError) as exc:
        raise LiveDispatchError(str(exc)) from exc
