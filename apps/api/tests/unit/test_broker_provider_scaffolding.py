from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path
from typing import ClassVar, cast

import pytest

from app.broker import provider
from app.broker.provider import (
    BrokerId,
    BrokerProviderCredentials,
    BrokerProviderPurpose,
    BrokerProviderRequest,
    BrokerProviderValidationError,
    BrokerRuntimeEnvironment,
    create_trading212_provider_adapter,
    validate_broker_provider_credentials,
    validate_broker_provider_request,
)
from app.broker.safety import TRADING212_BROKER_WRITE_METHODS

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"


def _imported_modules() -> set[str]:
    tree = ast.parse(inspect.getsource(provider))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    return imported_modules


def _top_level_runtime_imported_modules() -> set[str]:
    tree = ast.parse(inspect.getsource(provider))
    imported_modules: set[str] = set()
    # Intentionally inspect only top-level statements so TYPE_CHECKING imports
    # and local function-body imports do not count as module runtime imports.
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    return imported_modules


def _runtime_source_paths() -> list[Path]:
    return [
        p
        for p in APP_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and "tests" not in p.parts
        and p != APP_ROOT / "broker" / "provider.py"
    ] + sorted(SCRIPTS_ROOT.glob("*.py"))


class RecordingTrading212Adapter:
    calls: ClassVar[list[tuple[str, str, str]]] = []

    def __init__(self, api_key: str, api_secret: str, environment: str = "demo") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment
        self.calls.append((api_key, api_secret, environment))


@pytest.fixture(autouse=True)
def _clear_recording_adapter_calls() -> None:
    RecordingTrading212Adapter.calls.clear()


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


def test_provider_scaffolding_does_not_import_trading212_adapter_at_module_runtime() -> None:
    imported_modules = _top_level_runtime_imported_modules()

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


def test_valid_provider_credentials_pass_validation_unchanged() -> None:
    credentials = BrokerProviderCredentials(api_key="key", api_secret="secret")

    assert validate_broker_provider_credentials(credentials) is credentials


@pytest.mark.parametrize(
    "credentials",
    [
        BrokerProviderCredentials(api_key="", api_secret="secret"),
        BrokerProviderCredentials(api_key="   ", api_secret="secret"),
        BrokerProviderCredentials(api_key="key", api_secret=""),
        BrokerProviderCredentials(api_key="key", api_secret="   "),
    ],
)
def test_blank_provider_credentials_fail_direct_validation(
    credentials: BrokerProviderCredentials,
) -> None:
    with pytest.raises(BrokerProviderValidationError, match="credentials are not configured"):
        validate_broker_provider_credentials(credentials)


def test_valid_demo_trading212_provider_request_constructs_adapter_with_supplied_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )
    credentials = BrokerProviderCredentials(api_key="demo-key", api_secret="demo-secret")

    adapter = create_trading212_provider_adapter(
        request,
        credentials,
        app_mode="demo",
        live_trading_enabled=False,
    )

    assert isinstance(adapter, RecordingTrading212Adapter)
    assert adapter.environment == "demo"
    assert RecordingTrading212Adapter.calls == [("demo-key", "demo-secret", "demo")]


def test_live_adapter_construction_blocked_without_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
    )
    credentials = BrokerProviderCredentials(api_key="live-key", api_secret="live-secret")

    with pytest.raises(BrokerProviderValidationError, match="LIVE_TRADING_ENABLED"):
        create_trading212_provider_adapter(
            request,
            credentials,
            app_mode="live",
            live_trading_enabled=False,
        )

    assert RecordingTrading212Adapter.calls == []


def test_live_adapter_construction_succeeds_with_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.trading212 as trading212

    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="live",
        purpose="dependency",
    )
    credentials = BrokerProviderCredentials(api_key="live-key", api_secret="live-secret")

    adapter = create_trading212_provider_adapter(
        request,
        credentials,
        app_mode="live",
        live_trading_enabled=True,
    )

    assert isinstance(adapter, RecordingTrading212Adapter)
    assert RecordingTrading212Adapter.calls == [("live-key", "live-secret", "live")]


@pytest.mark.parametrize(
    ("provider_request", "app_mode", "match"),
    [
        (
            BrokerProviderRequest(
                broker_id=cast(BrokerId, "alpaca"),
                environment="demo",
                purpose="dependency",
            ),
            "demo",
            "Unsupported broker id",
        ),
        (
            BrokerProviderRequest(
                broker_id="trading212",
                environment=cast(BrokerRuntimeEnvironment, "sandbox"),
                purpose="dependency",
            ),
            "demo",
            "broker environment",
        ),
        (
            BrokerProviderRequest(
                broker_id="trading212",
                environment="demo",
                purpose="dependency",
            ),
            "mock",
            "must not construct real broker",
        ),
        (
            BrokerProviderRequest(
                broker_id="trading212",
                environment="demo",
                purpose="dependency",
            ),
            "paper",
            "must not construct real broker",
        ),
        (
            BrokerProviderRequest(
                broker_id="trading212",
                environment="live",
                purpose="dependency",
            ),
            "demo",
            "demo mode",
        ),
        (
            BrokerProviderRequest(
                broker_id="trading212",
                environment="demo",
                purpose="dependency",
            ),
            "live",
            "live mode",
        ),
    ],
)
def test_invalid_provider_adapter_requests_fail_before_construction(
    monkeypatch: pytest.MonkeyPatch,
    provider_request: BrokerProviderRequest,
    app_mode: str,
    match: str,
) -> None:
    import app.broker.trading212 as trading212

    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)
    credentials = BrokerProviderCredentials(api_key="key", api_secret="secret")

    with pytest.raises(BrokerProviderValidationError, match=match):
        create_trading212_provider_adapter(
            provider_request,
            credentials,
            app_mode=app_mode,
            live_trading_enabled=True,
        )

    assert RecordingTrading212Adapter.calls == []


@pytest.mark.parametrize(
    "credentials",
    [
        BrokerProviderCredentials(api_key="", api_secret="secret"),
        BrokerProviderCredentials(api_key="   ", api_secret="secret"),
        BrokerProviderCredentials(api_key="key", api_secret=""),
        BrokerProviderCredentials(api_key="key", api_secret="   "),
    ],
)
def test_blank_provider_credentials_fail_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
    credentials: BrokerProviderCredentials,
) -> None:
    import app.broker.trading212 as trading212

    monkeypatch.setattr(trading212, "Trading212Adapter", RecordingTrading212Adapter)
    request = BrokerProviderRequest(
        broker_id="trading212",
        environment="demo",
        purpose="dependency",
    )

    with pytest.raises(BrokerProviderValidationError, match="credentials are not configured"):
        create_trading212_provider_adapter(
            request,
            credentials,
            app_mode="demo",
            live_trading_enabled=False,
        )

    assert RecordingTrading212Adapter.calls == []


def test_provider_function_is_only_referenced_from_approved_runtime_call_sites() -> None:
    forbidden = {
        "BrokerProviderCredentials",
        "BrokerProviderRequest",
        "BrokerProviderValidationError",
        "BrokerRuntimeEnvironment",
        "create_trading212_provider_adapter",
    }

    scanned = [path for path in _runtime_source_paths() if path.exists()]
    assert len(scanned) >= 10, f"Call-site search scanned too few files: {scanned}"

    matches: dict[str, set[str]] = {}
    for path in scanned:
        source = path.read_text()
        used = {name for name in forbidden if name in source}
        if used:
            matches[str(path.relative_to(APP_ROOT.parent))] = used

    assert matches == {
        "app/api/deps.py": {
            "BrokerProviderCredentials",
            "BrokerProviderRequest",
            "BrokerProviderValidationError",
            "BrokerRuntimeEnvironment",
            "create_trading212_provider_adapter",
        },
        "app/api/v1/routes/broker.py": {
            "BrokerProviderCredentials",
            "BrokerProviderRequest",
            "BrokerProviderValidationError",
            "BrokerRuntimeEnvironment",
            "create_trading212_provider_adapter",
        },
        "app/services/demo_reconciliation_scheduler.py": {
            "BrokerProviderCredentials",
            "BrokerProviderRequest",
            "BrokerProviderValidationError",
            "create_trading212_provider_adapter",
        },
        "scripts/t212_demo_reconciliation_worker.py": {
            "BrokerProviderCredentials",
            "BrokerProviderRequest",
            "BrokerProviderValidationError",
            "create_trading212_provider_adapter",
        },
    }
