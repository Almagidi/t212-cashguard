from __future__ import annotations

import ast
import inspect
import uuid
from typing import cast

import pytest

from app.broker import provider
from app.broker.provider import (
    BrokerId,
    BrokerProviderPurpose,
    BrokerProviderRequest,
    BrokerProviderValidationError,
    BrokerRuntimeEnvironment,
    validate_broker_provider_request,
)
from app.broker.safety import TRADING212_BROKER_WRITE_METHODS


def _imported_modules() -> set[str]:
    tree = ast.parse(inspect.getsource(provider))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    return imported_modules


def test_valid_trading212_demo_request_passes_validation() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    assert (
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )
        is request
    )


def test_live_request_blocked_when_live_trading_disabled() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="LIVE_TRADING_ENABLED"):
        validate_broker_provider_request(
            request,
            app_mode="live",
            live_trading_enabled=False,
        )


def test_live_request_passes_when_live_trading_enabled() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
    )

    assert (
        validate_broker_provider_request(
            request,
            app_mode="live",
            live_trading_enabled=True,
        )
        is request
    )


def test_demo_app_mode_cannot_request_live_environment() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="demo mode"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


def test_live_app_mode_cannot_request_demo_environment() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="live mode"):
        validate_broker_provider_request(
            request,
            app_mode="live",
            live_trading_enabled=True,
        )


@pytest.mark.parametrize("app_mode", ["mock", "paper"])
def test_mock_and_paper_app_modes_cannot_request_real_broker_construction(
    app_mode: str,
) -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="must not construct real broker"):
        validate_broker_provider_request(
            request,
            app_mode=app_mode,
            live_trading_enabled=False,
        )


def test_unknown_app_mode_fails_closed() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="Unsupported app mode"):
        validate_broker_provider_request(
            request,
            app_mode="staging",
            live_trading_enabled=False,
        )


def test_invalid_broker_id_fails_closed() -> None:
    request = BrokerProviderRequest(
        broker_id=cast(BrokerId, "alpaca"),
        environment="demo",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="Unsupported broker id"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


def test_unsupported_broker_id_error_precedes_unsupported_environment() -> None:
    request = BrokerProviderRequest(
        broker_id=cast(BrokerId, "alpaca"),
        environment=cast(BrokerRuntimeEnvironment, "sandbox"),
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="Unsupported broker id"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


@pytest.mark.parametrize("environment", ["mock", "paper", "sandbox"])
def test_invalid_real_broker_environment_fails_closed(environment: str) -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment=cast(BrokerRuntimeEnvironment, environment),
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="broker environment"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


def test_invalid_provider_purpose_fails_closed() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose=cast(BrokerProviderPurpose, "strategy_trading"),
    )

    with pytest.raises(BrokerProviderValidationError, match="Unsupported provider purpose"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


@pytest.mark.parametrize(
    "purpose",
    ["worker_reconcile", "worker_cancel", "worker_account_sync"],
)
def test_demo_app_mode_rejects_live_only_purposes(purpose: BrokerProviderPurpose) -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose=purpose,
        user_id=uuid.uuid4(),
    )

    with pytest.raises(BrokerProviderValidationError, match="not allowed in demo mode"):
        validate_broker_provider_request(
            request,
            app_mode="demo",
            live_trading_enabled=False,
        )


def test_live_app_mode_rejects_demo_only_purpose() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="demo_reconciliation",
    )

    with pytest.raises(BrokerProviderValidationError, match="not allowed in live mode"):
        validate_broker_provider_request(
            request,
            app_mode="live",
            live_trading_enabled=True,
        )


def test_user_scoped_purpose_requires_user_id() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="worker_reconcile",
    )

    with pytest.raises(BrokerProviderValidationError, match="requires user_id"):
        validate_broker_provider_request(
            request,
            app_mode="live",
            live_trading_enabled=True,
        )


def test_provider_request_scaffolding_exposes_no_write_methods() -> None:
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    exposed = set(dir(request)) | set(dir(provider))

    assert exposed.isdisjoint(TRADING212_BROKER_WRITE_METHODS)


def test_provider_scaffolding_does_not_import_trading212_adapter() -> None:
    imported_modules = _imported_modules()

    assert "app.broker.trading212" not in imported_modules


def test_provider_scaffolding_does_not_import_db_api_routes_or_sqlalchemy() -> None:
    imported_modules = _imported_modules()

    assert not any(
        name.startswith("app.db.")
        or name.startswith("app.api.")
        or name == "sqlalchemy"
        or name.startswith("sqlalchemy.")
        for name in imported_modules
    )
