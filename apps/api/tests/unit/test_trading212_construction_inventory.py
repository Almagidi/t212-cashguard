from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

API_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = API_ROOT / "app"
SCRIPTS_ROOT = API_ROOT / "scripts"
TRADING212_ADAPTER_PATH = APP_ROOT / "broker" / "trading212.py"

PROVIDER_BACKED_OPERATIONAL_PATHS = {
    "app/workers/tasks.py": {
        "provider_construct": 4,
        "call_sites": {
            "cancel_timed_out_orders",
            "reconcile_pending_orders",
            "sync_account_snapshot",
            "track_cfd_funding",
        },
    },
    "app/services/position_monitor.py": {
        "provider_construct": 1,
        "call_sites": {"PositionMonitor._get_broker"},
    },
    "app/services/portfolio_execution_service.py": {
        "provider_construct": 1,
        "call_sites": {"PortfolioExecutionService._get_broker"},
    },
    "app/services/strategy_runner.py": {
        "provider_construct": 1,
        "call_sites": {"StrategyRunner._get_broker"},
    },
    "app/services/system_control.py": {
        "provider_construct": 1,
        "call_sites": {"SystemControlService._get_broker"},
    },
}

REMAINING_DIRECT_TRADING212_ADAPTER_PATHS = {
    "app/broker/trading212.py": "adapter implementation",
    "app/broker/provider.py": "canonical provider final-construction boundary",
    "scripts/t212_demo_multi_order_reconciliation_smoke.py": (
        "terminal-only manual DEMO smoke with read-only write guard"
    ),
    "scripts/t212_demo_readonly_smoke.py": "terminal-only manual DEMO read-only smoke",
    "scripts/t212_demo_reconcile_order.py": ("terminal-only manual DEMO reconciliation script"),
}


def _runtime_source_paths() -> list[Path]:
    return sorted(
        [
            path
            for root in (APP_ROOT, SCRIPTS_ROOT)
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts and path != APP_ROOT / "broker" / "trading212.py"
        ]
    )


def _relative(path: Path) -> str:
    return path.relative_to(API_ROOT).as_posix()


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _trading212_adapter_references() -> dict[str, dict[str, int]]:
    references: dict[str, dict[str, int]] = {}

    for path in _runtime_source_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        path_references: Counter[str] = Counter()
        adapter_names: set[str] = set()
        trading212_module_names: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.broker.trading212":
                for alias in node.names:
                    if alias.name == "Trading212Adapter":
                        adapter_names.add(alias.asname or alias.name)
                        path_references["import"] += 1
            elif isinstance(node, ast.ImportFrom) and node.module == "app.broker":
                for alias in node.names:
                    if alias.name == "trading212":
                        trading212_module_names.add(alias.asname or alias.name)
                        path_references["import"] += 1
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.broker.trading212":
                        if alias.asname:
                            trading212_module_names.add(alias.asname)
                        path_references["import"] += 1
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in adapter_names:
                    path_references["construct"] += 1
                    continue

                if isinstance(func, ast.Attribute) and func.attr == "Trading212Adapter":
                    module_name = _dotted_name(func.value)
                    if module_name in trading212_module_names or (
                        module_name == "app.broker.trading212"
                    ):
                        path_references["construct"] += 1
        if path_references:
            references[_relative(path)] = dict(sorted(path_references.items()))

    return references


def _provider_adapter_call_counts() -> dict[str, int]:
    references: dict[str, int] = {}

    for path in _runtime_source_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        provider_names: set[str] = set()
        construct_count = 0

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.broker.provider":
                for alias in node.names:
                    if alias.name == "create_trading212_provider_adapter":
                        provider_names.add(alias.asname or alias.name)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in provider_names
            ):
                construct_count += 1

        if construct_count:
            references[_relative(path)] = construct_count

    return references


def test_runtime_inventory_counts_aliased_and_module_constructor_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_root = tmp_path / "app"
    scripts_root = tmp_path / "scripts"
    source_path = app_root / "example.py"
    app_root.mkdir()
    scripts_root.mkdir()
    source_path.write_text(
        "\n".join(
            [
                "from app.broker.trading212 import Trading212Adapter as T212Adapter",
                "from app.broker import trading212",
                "",
                "T212Adapter(api_key='key', api_secret='secret', environment='demo')",
                "trading212.Trading212Adapter(",
                "    api_key='key', api_secret='secret', environment='demo'",
                ")",
            ]
        )
    )

    monkeypatch.setattr(__name__ + ".API_ROOT", tmp_path)
    monkeypatch.setattr(__name__ + ".APP_ROOT", app_root)
    monkeypatch.setattr(__name__ + ".SCRIPTS_ROOT", scripts_root)

    assert _trading212_adapter_references() == {"app/example.py": {"construct": 2, "import": 2}}


def test_runtime_trading212_adapter_construction_inventory_is_locked() -> None:
    expected_references = {
        # Canonical Trading 212 provider construction. This is the intended final
        # adapter constructor after caller-owned credential and safety decisions.
        "app/broker/provider.py": {"construct": 1, "import": 2},
        # Manual terminal-only DEMO reconciliation smoke with a write-method guard.
        "scripts/t212_demo_multi_order_reconciliation_smoke.py": {
            "construct": 1,
            "import": 1,
        },
        # Manual terminal-only DEMO read-only smoke.
        "scripts/t212_demo_readonly_smoke.py": {"construct": 1, "import": 1},
        # Manual terminal-only DEMO single-order history reconciliation.
        "scripts/t212_demo_reconcile_order.py": {"construct": 1, "import": 1},
    }

    actual = _trading212_adapter_references()
    assert actual == expected_references, (
        "Trading212Adapter runtime inventory changed. "
        "Update docs/architecture/broker-interface-readiness-audit.md when adding or removing references."
    )


def test_post_provider_migration_operational_paths_are_provider_backed() -> None:
    direct_references = _trading212_adapter_references()
    provider_call_counts = _provider_adapter_call_counts()

    for path, expected in PROVIDER_BACKED_OPERATIONAL_PATHS.items():
        assert (
            path not in direct_references
        ), f"{path} must stay provider-backed without direct Trading212Adapter import/construction"
        assert provider_call_counts.get(path) == expected["provider_construct"], (
            path,
            expected["call_sites"],
        )


def test_remaining_direct_trading212_adapter_paths_are_precisely_classified() -> None:
    actual_paths = set(_trading212_adapter_references()) | {_relative(TRADING212_ADAPTER_PATH)}

    assert actual_paths == set(
        REMAINING_DIRECT_TRADING212_ADAPTER_PATHS
    ), "Remaining direct Trading212Adapter paths changed; classify any new path precisely."
    assert TRADING212_ADAPTER_PATH.exists()
    assert "class Trading212Adapter" in TRADING212_ADAPTER_PATH.read_text()
