"""Central runtime safety policy for broker and order boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order

RuntimeMode = Literal["mock", "demo", "live"]
BrokerEnvironment = Literal["demo", "live"]

BROKER_BASE_URLS: dict[BrokerEnvironment, str] = {
    "demo": "https://demo.trading212.com",
    "live": "https://live.trading212.com",
}


class SafetyPolicyViolation(Exception):
    """Raised when runtime policy blocks a safety-sensitive action."""

    def __init__(self, reason: str, *, decision_code: str = "safety_policy_block") -> None:
        self.reason = reason
        self.decision_code = decision_code
        super().__init__(reason)


def current_runtime_mode() -> RuntimeMode:
    return settings.APP_MODE


def live_execution_enabled() -> bool:
    return bool(settings.LIVE_TRADING_ENABLED)


def credentials_configured_status() -> dict[str, str]:
    """Safe startup diagnostic: configured/not configured only, never values."""
    return {
        "T212_DEMO_API_KEY": "configured" if settings.T212_DEMO_API_KEY else "not configured",
        "T212_LIVE_API_KEY": "configured" if settings.T212_LIVE_API_KEY else "not configured",
        "LIVE_TRADING_ENABLED": str(bool(settings.LIVE_TRADING_ENABLED)).lower(),
    }


def broker_calls_allowed(environment: str | None) -> bool:
    try:
        require_broker_environment(environment, action="broker_call")
    except SafetyPolicyViolation:
        return False
    return True


def broker_base_url_for(environment: str) -> str:
    require_broker_environment(environment, action="broker_base_url")
    return BROKER_BASE_URLS[environment]  # type: ignore[index]


def require_broker_environment(
    environment: str | None,
    *,
    action: str,
) -> None:
    """Ensure a broker adapter cannot cross mock/demo/live boundaries."""
    mode = current_runtime_mode()
    if mode == "mock":
        raise SafetyPolicyViolation(
            f"{action} blocked: APP_MODE=mock must not call real broker endpoints.",
            decision_code="mock_broker_block",
        )

    if environment not in BROKER_BASE_URLS:
        raise SafetyPolicyViolation(
            f"{action} blocked: broker environment is not explicitly demo or live.",
            decision_code="broker_environment_invalid",
        )

    if mode == "demo" and environment != "demo":
        raise SafetyPolicyViolation(
            f"{action} blocked: demo mode may only use demo broker endpoints.",
            decision_code="demo_to_live_block",
        )

    if mode == "live":
        if environment != "live":
            raise SafetyPolicyViolation(
                f"{action} blocked: live mode requires a live broker connection.",
                decision_code="live_environment_mismatch",
            )
        if not live_execution_enabled():
            raise SafetyPolicyViolation(
                f"{action} blocked: LIVE_TRADING_ENABLED must be true before live broker calls.",
                decision_code="live_flag_disabled",
            )


async def _get_app_settings(db: Any) -> AppSettings | None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    return result.scalar_one_or_none()


async def audit_safety_decision(
    db: Any,
    *,
    action: str,
    actor: str,
    decision: str,
    reason: str,
    order: Order | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "mode": current_runtime_mode(),
        "decision": decision,
        "reason": reason,
        "no_broker_order_sent": decision != "allowed",
        **(metadata or {}),
    }
    if order is not None:
        payload.update(
            {
                "ticker": order.ticker,
                "side": order.side,
                "order_type": order.order_type,
                "quantity": str(order.quantity),
                "order_id": str(order.id),
                "execution_environment": order.execution_environment,
                "is_dry_run": order.is_dry_run,
            }
        )

    db.add(
        AuditLog(
            action=action,
            entity_type="order" if order else "runtime",
            entity_id=str(order.id) if order else None,
            actor=actor,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()


async def require_order_submission_allowed(
    db: Any,
    *,
    order: Order,
    broker_environment: str | None,
    actor: str = "execution_engine",
) -> None:
    """Final gate immediately before any simulated or broker-backed submit."""
    app_settings = await _get_app_settings(db)
    if app_settings is None:
        raise SafetyPolicyViolation(
            "Order submission blocked: app settings are not initialized.",
            decision_code="settings_missing",
        )

    if app_settings.kill_switch_active:
        reason = "Kill switch is active. Order submission is blocked."
        await audit_safety_decision(
            db,
            action="order_blocked_by_kill_switch",
            actor=actor,
            decision="blocked",
            reason=reason,
            order=order,
        )
        raise SafetyPolicyViolation(reason, decision_code="kill_switch_block")

    if order.is_dry_run:
        return

    try:
        require_broker_environment(broker_environment, action="order submission")
    except SafetyPolicyViolation as exc:
        await audit_safety_decision(
            db,
            action="order_blocked_by_runtime_policy",
            actor=actor,
            decision="blocked",
            reason=exc.reason,
            order=order,
            metadata={"decision_code": exc.decision_code},
        )
        raise

    if current_runtime_mode() == "live":
        from app.services.live_readiness import LiveReadinessService

        readiness = await LiveReadinessService(db).evaluate()
        if not readiness["ready_for_live"]:
            blockers = readiness.get("blockers") or ["Live readiness checklist is incomplete."]
            reason = f"Live readiness incomplete. Order submission blocked: {blockers[0]}"
            await audit_safety_decision(
                db,
                action="live_readiness_check_failed",
                actor=actor,
                decision="blocked",
                reason=reason,
                order=order,
                metadata={"blockers": blockers},
            )
            raise SafetyPolicyViolation(reason, decision_code="live_readiness_block")


async def audit_broker_request_attempt(
    db: Any,
    *,
    order: Order,
    actor: str,
    broker_environment: str | None,
) -> None:
    await audit_safety_decision(
        db,
        action="broker_request_attempted",
        actor=actor,
        decision="allowed",
        reason="Broker request allowed by runtime safety policy.",
        order=order,
        metadata={"broker_environment": broker_environment, "no_broker_order_sent": False},
    )
