"""Broker provider request and Trading 212 construction scaffolding.

This module validates real-broker provider requests without touching credential
stores or importing database/session code at module import time. Runtime call
sites remain responsible for credential lookup and safety-policy context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from uuid import UUID

    from app.broker.trading212 import Trading212Adapter

BrokerId = Literal["trading212"]
BrokerRuntimeEnvironment = Literal["demo", "live"]
BrokerProviderPurpose = Literal[
    "dependency",
    "credential_test",
    "demo_reconciliation",
    "worker_account_sync",
    "worker_cfd_funding",
    "worker_reconcile",
    "worker_cancel",
    "worker_position_monitor",
    "worker_strategy_runner",
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
        "worker_cfd_funding",
        "worker_reconcile",
        "worker_cancel",
        "worker_position_monitor",
        "worker_strategy_runner",
    }
)
_DEMO_ONLY_PURPOSES: frozenset[str] = frozenset({"demo_reconciliation"})
_LIVE_ONLY_PURPOSES: frozenset[str] = frozenset({"worker_cancel"})
_USER_SCOPED_PURPOSES: frozenset[str] = frozenset(
    {
        "worker_account_sync",
        "worker_cfd_funding",
        "worker_reconcile",
        "worker_cancel",
        "worker_position_monitor",
        "worker_strategy_runner",
    }
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


@dataclass(frozen=True)
class BrokerProviderCredentials:
    """Explicit credentials supplied by a broker-specific caller.

    Provider scaffolding does not fetch, decrypt, or infer credentials.
    """

    api_key: str
    api_secret: str


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


def validate_broker_provider_credentials(
    credentials: BrokerProviderCredentials,
) -> BrokerProviderCredentials:
    """Validate explicit provider credentials before adapter construction."""

    if not credentials.api_key.strip() or not credentials.api_secret.strip():
        raise BrokerProviderValidationError(
            "Broker provider credentials are not configured for adapter construction."
        )
    return credentials


def create_trading212_provider_adapter(
    request: BrokerProviderRequest,
    credentials: BrokerProviderCredentials,
    *,
    app_mode: str,
    live_trading_enabled: bool,
) -> Trading212Adapter:
    """Construct a Trading 212 adapter after explicit provider validation.

    This function does not read settings, fetch/decrypt credentials, access the
    database, call Trading 212, or place/cancel orders.
    """

    validated_request = validate_broker_provider_request(
        request,
        app_mode=app_mode,
        live_trading_enabled=live_trading_enabled,
    )
    validated_credentials = validate_broker_provider_credentials(credentials)

    from app.broker.trading212 import Trading212Adapter

    return Trading212Adapter(
        validated_credentials.api_key,
        validated_credentials.api_secret,
        validated_request.environment,
    )
