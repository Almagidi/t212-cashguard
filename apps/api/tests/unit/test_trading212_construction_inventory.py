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
        # Portfolio rebalance helper still builds a broad broker for account
        # reads and possible order submission. Keep until write-capable paths
        # have separate provider acceptance tests.
        "app/services/portfolio_execution_service.py": {"construct": 1, "import": 1},
        # Position monitoring can submit exits and EOD flatten orders, so it is
        # intentionally not the first remaining provider migration target.
        "app/services/position_monitor.py": {"construct": 1, "import": 1},
        # Strategy runner can submit strategy orders after its own gates.
        "app/services/strategy_runner.py": {"construct": 1, "import": 1},
        # System control has read-only status helpers and emergency cancel/flatten
        # operations sharing one broker helper.
        "app/services/system_control.py": {"construct": 1, "import": 1},
        # Worker task references map mechanically to these functions:
        # reconcile_pending_orders: import + construct
        # cancel_timed_out_orders: import + construct
        # track_cfd_funding: import + construct
        # sync_account_snapshot has migrated to provider construction.
        "app/workers/tasks.py": {"construct": 3, "import": 3},
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
