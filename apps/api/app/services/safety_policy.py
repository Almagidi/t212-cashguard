"""Central runtime safety policy for broker and order boundaries."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSettings, AuditLog, Order

RuntimeMode = Literal["mock", "paper", "demo", "live"]
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


# An authorization is a short-lived permit: it is valid from the fresh position read that
# produced it until the order is sent, which is seconds. A permit older than this was not
# produced by the read that justified it.
PROTECTIVE_EXIT_AUTHORIZATION_TTL = timedelta(seconds=120)


@dataclass(frozen=True)
class ProtectiveExitAuthorization:
    """Evidence that a sell only reduces a long position that is really held.

    Only ``ExecutionEngine`` creates one that counts, through
    ``_issue_protective_exit_authorization``, from a fresh broker position read. Every
    issued authorization is recorded in a process-private registry under a random
    ``nonce``; ``verify_protective_exit`` accepts an object only if it is identical to the
    recorded one, so an instance built by any other code (a made-up ``held_quantity``, a
    copied or altered real one) is refused. It is bound to one order (id, ticker,
    quantity) and expires after ``PROTECTIVE_EXIT_AUTHORIZATION_TTL``. It never relaxes
    any gate but the kill switch.
    """

    ticker: str
    held_quantity: Decimal
    in_flight_sell_quantity: Decimal = Decimal("0")
    order_id: uuid.UUID | None = None
    quantity: Decimal = Decimal("0")
    issued_at: datetime = datetime.min.replace(tzinfo=UTC)
    nonce: str = ""


_ISSUED_PROTECTIVE_EXITS: dict[str, ProtectiveExitAuthorization] = {}


def _issue_protective_exit_authorization(
    order: Order,
    *,
    held_quantity: Decimal,
    in_flight_sell_quantity: Decimal,
    now: datetime | None = None,
) -> ProtectiveExitAuthorization:
    """Mint and record an authorization. For ``ExecutionEngine`` only."""
    issued_at = now or datetime.now(UTC)
    for nonce, issued in list(_ISSUED_PROTECTIVE_EXITS.items()):
        if issued_at - issued.issued_at > PROTECTIVE_EXIT_AUTHORIZATION_TTL:
            del _ISSUED_PROTECTIVE_EXITS[nonce]
    authorization = ProtectiveExitAuthorization(
        ticker=str(order.ticker).strip().upper(),
        held_quantity=held_quantity,
        in_flight_sell_quantity=in_flight_sell_quantity,
        order_id=order.id,
        quantity=Decimal(str(order.quantity)),
        issued_at=issued_at,
        nonce=secrets.token_urlsafe(24),
    )
    _ISSUED_PROTECTIVE_EXITS[authorization.nonce] = authorization
    return authorization


def verify_protective_exit(
    order: Order, auth: ProtectiveExitAuthorization, *, now: datetime | None = None
) -> None:
    """Refuse anything that is not a sell reducing a held long position, proven by the engine."""

    def refuse(why: str) -> SafetyPolicyViolation:
        return SafetyPolicyViolation(
            f"Protective exit refused: {why}.", decision_code="protective_exit_refused"
        )

    if _ISSUED_PROTECTIVE_EXITS.get(auth.nonce) != auth or not auth.nonce:
        raise refuse("the authorization was not issued by the execution engine")
    if (now or datetime.now(UTC)) - auth.issued_at > PROTECTIVE_EXIT_AUTHORIZATION_TTL:
        raise refuse("the authorization has expired; the position must be read again")
    if auth.order_id != order.id:
        raise refuse("the authorization belongs to a different order")
    if order.side != "sell":
        raise refuse("only a sell can be a protective exit")
    if str(order.ticker).strip().upper() != auth.ticker:
        raise refuse("order ticker does not match the verified position")
    quantity = Decimal(str(order.quantity))
    if quantity != auth.quantity:
        raise refuse("order quantity changed after the position was verified")
    if quantity <= 0 or auth.held_quantity <= 0:
        raise refuse("there is no held long quantity to reduce")
    if quantity + auth.in_flight_sell_quantity > auth.held_quantity:
        raise refuse("quantity exceeds the held long position (would open or grow a short)")


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


def _runtime_mode() -> str:
    mode = str(settings.APP_MODE)
    if mode not in {"mock", "paper", "demo", "live"}:
        raise SafetyPolicyViolation(
            f"broker access blocked: APP_MODE is not recognized ({mode!r}).",
            decision_code="app_mode_invalid",
        )
    return mode


def require_adapter_credentials(
    *,
    api_key: str,
    api_secret: str,
    environment: str | None,
) -> None:
    """Ensure raw adapter construction cannot silently run with wrong/blank secrets."""
    require_broker_environment(environment, action="broker adapter construction")
    env_label = str(environment)
    if not api_key.strip() or not api_secret.strip():
        raise SafetyPolicyViolation(
            f"broker adapter construction blocked: {env_label} credentials are not configured.",
            decision_code=f"{env_label}_credentials_missing",
        )


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
    mode = _runtime_mode()
    if mode in {"mock", "paper"}:
        raise SafetyPolicyViolation(
            f"{action} blocked: APP_MODE={mode} must not call real broker endpoints.",
            decision_code=f"{mode}_broker_block",
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
    result = await db.execute(
        select(AppSettings).where(AppSettings.id == 1).execution_options(populate_existing=True)
    )
    return cast("AppSettings | None", result.scalar_one_or_none())


# A blocked protective exit is retried every monitor cycle. It is reported (audit + alert) once
# per ticker and reason in this window rather than every cycle.
PROTECTIVE_EXIT_NOTICE_WINDOW = timedelta(minutes=15)


async def report_protective_exit_blocked_once(
    db: Any,
    *,
    action: str,
    ticker: str,
    reason: str,
    actor: str,
    order: Order | None = None,
    metadata: dict[str, Any] | None = None,
    alert_type: str | None = None,
) -> bool:
    """Audit and alert a blocked or refused protective exit, once per (ticker, reason) per window.

    Returns True if it reported (the first time in the window), False if it was already
    reported. The audit rows themselves are the memory, so this survives restarts and is
    shared by every worker.
    """
    ticker = str(ticker).strip().upper()
    cutoff = datetime.now(UTC) - PROTECTIVE_EXIT_NOTICE_WINDOW
    recent = await db.execute(
        select(AuditLog.payload).where(AuditLog.action == action, AuditLog.occurred_at >= cutoff)
    )
    for (payload,) in recent.all():
        if (
            isinstance(payload, dict)
            and str(payload.get("ticker", "")).strip().upper() == ticker
            and payload.get("reason") == reason
        ):
            return False
    await audit_safety_decision(
        db,
        action=action,
        actor=actor,
        decision="blocked",
        reason=reason,
        order=order,
        metadata={**(metadata or {}), "ticker": ticker},
    )
    from app.services.alert_service import AlertService

    await AlertService(db).send(
        alert_type=alert_type or action,
        title=f"Protective exit blocked: {ticker}",
        message=reason,
        severity="critical",
        payload={"ticker": ticker, "action": action},
    )
    return True


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
                "broker_account_scope": order.broker_account_scope,
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
    broker: Any = None,
    protective_exit: ProtectiveExitAuthorization | None = None,
) -> dict[str, Any] | None:
    """Final gate immediately before any simulated or broker-backed submit.

    ``protective_exit`` exempts a verified long-reducing sell from the kill-switch check
    only. Mode, broker environment, live binding and live readiness still apply.
    """
    app_settings = await _get_app_settings(db)
    if app_settings is None:
        raise SafetyPolicyViolation(
            "Order submission blocked: app settings are not initialized.",
            decision_code="settings_missing",
        )

    if protective_exit is not None:
        try:
            verify_protective_exit(order, protective_exit)
        except SafetyPolicyViolation as exc:
            await audit_safety_decision(
                db,
                action="protective_exit_refused",
                actor=actor,
                decision="blocked",
                reason=exc.reason,
                order=order,
                metadata={"decision_code": exc.decision_code},
            )
            raise

    if app_settings.kill_switch_active and protective_exit is not None:
        await audit_safety_decision(
            db,
            action="protective_exit_allowed_under_kill_switch",
            actor=actor,
            decision="protective_exit_allowed",
            reason="Kill switch is active; a sell reducing a held long position is allowed.",
            order=order,
            metadata={
                "no_broker_order_sent": False,
                "held_quantity": str(protective_exit.held_quantity),
            },
        )
    elif app_settings.kill_switch_active:
        reason = "Kill switch is active. Order submission is blocked."
        await audit_safety_decision(
            db,
            action=(
                "demo_order_blocked_by_kill_switch"
                if current_runtime_mode() == "demo"
                else "order_blocked_by_kill_switch"
            ),
            actor=actor,
            decision="blocked",
            reason=reason,
            order=order,
        )
        raise SafetyPolicyViolation(reason, decision_code="kill_switch_block")

    if order.is_dry_run:
        return None

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
        from app.services.live_dispatch import LiveDispatchError, authorize_dispatch

        try:
            return await authorize_dispatch(
                db, broker, order, protective_exit=protective_exit is not None
            )
        except LiveDispatchError as exc:
            reason = str(exc)
            await audit_safety_decision(
                db,
                action="live_readiness_check_failed",
                actor=actor,
                decision="blocked",
                reason=reason,
                order=order,
            )
            raise SafetyPolicyViolation(reason, decision_code="live_readiness_block") from exc
    return None


async def audit_broker_request_attempt(
    db: Any,
    *,
    order: Order,
    actor: str,
    broker_environment: str | None,
) -> None:
    await audit_safety_decision(
        db,
        action=(
            "demo_broker_order_attempt"
            if current_runtime_mode() == "demo" and broker_environment == "demo"
            else "broker_request_attempted"
        ),
        actor=actor,
        decision="allowed",
        reason="Broker request allowed by runtime safety policy.",
        order=order,
        metadata={"broker_environment": broker_environment, "no_broker_order_sent": False},
    )
