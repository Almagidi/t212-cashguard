"""Type-only broker provider request scaffolding.

This module validates future real-broker provider requests without constructing
adapters, touching credentials, importing database/session code, or wiring any
runtime call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from uuid import UUID

BrokerId = Literal["trading212"]
BrokerRuntimeEnvironment = Literal["demo", "live"]
BrokerProviderPurpose = Literal[
    "dependency",
    "credential_test",
    "demo_reconciliation",
    "worker_account_sync",
    "worker_reconcile",
    "worker_cancel",
]

_SUPPORTED_BROKER_IDS: frozenset[str] = frozenset({"trading212"})
_SUPPORTED_ENVIRONMENTS: frozenset[str] = frozenset({"demo", "live"})
_SUPPORTED_APP_MODES: frozenset[str] = frozenset({"demo", "live", "mock", "paper"})
_SUPPORTED_PURPOSES: frozenset[str] = frozenset(
    {
        "dependency",
        "credential_test",
        "demo_reconciliation",
        "worker_account_sync",
        "worker_reconcile",
        "worker_cancel",
    }
)
_DEMO_ONLY_PURPOSES: frozenset[str] = frozenset({"demo_reconciliation"})
_LIVE_ONLY_PURPOSES: frozenset[str] = frozenset(
    {"worker_account_sync", "worker_reconcile", "worker_cancel"}
)
_USER_SCOPED_PURPOSES: frozenset[str] = frozenset(
    {"worker_account_sync", "worker_reconcile", "worker_cancel"}
)


class BrokerProviderValidationError(ValueError):
    """Raised when a broker provider request is rejected before construction."""


@dataclass(frozen=True)
class BrokerProviderRequest:
    """Typed request for a future broker provider boundary.

    The request object is intentionally data-only. It does not own credentials,
    construct adapters, or expose broker read/write methods.
    """

    broker_id: BrokerId
    environment: BrokerRuntimeEnvironment
    purpose: BrokerProviderPurpose
    user_id: UUID | None = None


def validate_broker_provider_request(
    request: BrokerProviderRequest,
    *,
    app_mode: str,
    live_trading_enabled: bool,
) -> BrokerProviderRequest:
    """Validate a future real-broker provider request and return it unchanged.

    This is deliberately pure validation. It does not consult global settings,
    read credentials, import adapters, open DB sessions, or create clients.
    """

    if request.broker_id not in _SUPPORTED_BROKER_IDS:
        raise BrokerProviderValidationError(
            f"Unsupported broker id for provider request: {request.broker_id!r}."
        )

    if request.environment not in _SUPPORTED_ENVIRONMENTS:
        raise BrokerProviderValidationError(
            "Real broker environment must be explicitly 'demo' or 'live'. "
            f"Received {request.environment!r}."
        )

    if request.purpose not in _SUPPORTED_PURPOSES:
        raise BrokerProviderValidationError(f"Unsupported provider purpose: {request.purpose!r}.")

    if app_mode not in _SUPPORTED_APP_MODES:
        raise BrokerProviderValidationError(
            f"Unsupported app mode for provider request: {app_mode!r}."
        )

    if app_mode in {"mock", "paper"}:
        raise BrokerProviderValidationError(
            f"APP_MODE={app_mode} must not construct real broker providers."
        )

    if request.purpose in _USER_SCOPED_PURPOSES and request.user_id is None:
        raise BrokerProviderValidationError(
            f"Provider purpose {request.purpose!r} requires user_id."
        )

    if app_mode == "demo":
        if request.environment != "demo":
            raise BrokerProviderValidationError(
                "demo mode may only request the demo broker environment."
            )
        if request.purpose in _LIVE_ONLY_PURPOSES:
            raise BrokerProviderValidationError(
                f"Provider purpose {request.purpose!r} is not allowed in demo mode."
            )
        return request

    if app_mode == "live":
        if request.environment != "live":
            raise BrokerProviderValidationError(
                "live mode may only request the live broker environment."
            )
        if not live_trading_enabled:
            raise BrokerProviderValidationError(
                "LIVE_TRADING_ENABLED must be true before live provider validation can pass."
            )
        if request.purpose in _DEMO_ONLY_PURPOSES:
            raise BrokerProviderValidationError(
                f"Provider purpose {request.purpose!r} is not allowed in live mode."
            )
        return request

    raise BrokerProviderValidationError(f"Unsupported app mode for provider request: {app_mode!r}.")
