from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.unit import test_trading212_construction_inventory as construction_inventory

API_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = API_ROOT / "app"
SCRIPTS_ROOT = API_ROOT / "scripts"

TASKS_PATH = APP_ROOT / "workers" / "tasks.py"
REMAINING_SERVICE_PATHS = {
    "system_control": APP_ROOT / "services" / "system_control.py",
}
PORTFOLIO_EXECUTION_PATH = APP_ROOT / "services" / "portfolio_execution_service.py"
STRATEGY_RUNNER_PATH = APP_ROOT / "services" / "strategy_runner.py"
POSITION_MONITOR_PATH = APP_ROOT / "services" / "position_monitor.py"
MANUAL_SMOKE_PATHS = {
    "readonly_smoke": SCRIPTS_ROOT / "t212_demo_readonly_smoke.py",
    "reconcile_order": SCRIPTS_ROOT / "t212_demo_reconcile_order.py",
    "multi_order_reconciliation_smoke": (
        SCRIPTS_ROOT / "t212_demo_multi_order_reconciliation_smoke.py"
    ),
}
SYSTEM_CONTROL_READ_STATUS_METHODS = {"get_positions_summary", "get_snapshot"}
SYSTEM_CONTROL_EMERGENCY_METHODS = {"cancel_all_pending", "flatten_all"}
READ_ONLY_FORBIDDEN_CALLS = {
    "cancel_order",
    "create_order_intent",
    "place_limit_order",
    "place_market_order",
    "place_order",
    "place_stop_limit_order",
    "place_stop_order",
    "submit_order",
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _top_level_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing top-level function {name!r}")


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name!r}")


def _method_node(class_node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing method {class_node.name}.{name}")


def _adapter_counts(node: ast.AST) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module == "app.broker.trading212":
            counts["import"] += sum(alias.name == "Trading212Adapter" for alias in child.names)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "Trading212Adapter"
        ):
            counts["construct"] += 1
    return dict(sorted(counts.items()))


def _zero_filled_adapter_counts(node: ast.AST) -> dict[str, int]:
    counts = _adapter_counts(node)
    return {"construct": counts.get("construct", 0), "import": counts.get("import", 0)}


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def _function_names_with_call(tree: ast.Module, call_name: str) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and call_name in _call_names(node)
    }


def _function_names_with_direct_adapter(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _adapter_counts(node)
    }


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _source_contains(node: ast.AST, text: str) -> bool:
    # ast.unparse normalises string quotes, so assertions use that canonical source.
    return text in ast.unparse(node)


def test_cancel_timed_out_orders_is_provider_backed_and_cancellation_capable() -> None:
    tree = _parse(TASKS_PATH)
    cancel_node = _top_level_function(tree, "cancel_timed_out_orders")

    assert _zero_filled_adapter_counts(cancel_node) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in _call_names(cancel_node)
    assert "BrokerProviderRequest" in ast.unparse(cancel_node)
    assert "BrokerProviderCredentials" in ast.unparse(cancel_node)
    assert "worker_cancel_timed_out_orders" in ast.unparse(cancel_node)
    assert "ExecutionEngine" in ast.unparse(cancel_node)
    assert "cancel_order" in _call_names(cancel_node)


def test_workers_tasks_direct_inventory_and_provider_call_sites_are_locked() -> None:
    tree = _parse(TASKS_PATH)

    assert "app/workers/tasks.py" not in construction_inventory._trading212_adapter_references()
    assert _function_names_with_direct_adapter(tree) == set()
    assert _function_names_with_call(tree, "create_trading212_provider_adapter") == {
        "sync_account_snapshot",
        "track_cfd_funding",
        "reconcile_pending_orders",
        "cancel_timed_out_orders",
    }


@pytest.mark.parametrize(
    ("name", "path", "class_name", "write_evidence", "classification"),
    [
        (
            "system_control",
            REMAINING_SERVICE_PATHS["system_control"],
            "SystemControlService",
            {"cancel_all_pending", "flatten_all", "cancel_order", "submit_order"},
            "mixed/write-capable",
        ),
    ],
)
def test_service_construction_paths_are_classified_by_write_surface(
    name: str,
    path: Path,
    class_name: str,
    write_evidence: set[str],
    classification: str,
) -> None:
    tree = _parse(path)
    service_class = _class_node(tree, class_name)
    get_broker = _method_node(service_class, "_get_broker")
    service_method_names = {
        node.name
        for node in service_class.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }

    assert _adapter_counts(get_broker) == {"construct": 1, "import": 1}, name
    # Check the full service file, not just the service class, to catch helper-level wiring.
    assert "create_trading212_provider_adapter" not in _call_names(tree), name
    # Classification labels document audit intent; write evidence is the asserted contract.
    assert write_evidence <= (_call_names(service_class) | service_method_names), (
        name,
        classification,
    )


def test_portfolio_execution_is_provider_backed_but_still_mixed_write_capable_for_rebalance_orders() -> (
    None
):
    tree = _parse(PORTFOLIO_EXECUTION_PATH)
    service_class = _class_node(tree, "PortfolioExecutionService")
    get_broker = _method_node(service_class, "_get_broker")
    run_strategy_once = _method_node(service_class, "run_strategy_once")
    source = PORTFOLIO_EXECUTION_PATH.read_text()

    assert _zero_filled_adapter_counts(tree) == {"construct": 0, "import": 0}
    assert _zero_filled_adapter_counts(get_broker) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in _call_names(get_broker)
    assert "BrokerProviderRequest" in source
    assert "BrokerProviderCredentials" in source
    assert "worker_portfolio_execution" in ast.unparse(get_broker)
    assert {"get_account_summary", "get_positions"} <= _call_names(service_class)
    assert {"create_order_intent", "submit_order"} <= _call_names(run_strategy_once)
    assert _source_contains(run_strategy_once, "portfolio_rebalance_order")


def test_position_monitor_is_provider_backed_and_write_capable_for_exits_and_eod_flatten() -> None:
    tree = _parse(POSITION_MONITOR_PATH)
    service_class = _class_node(tree, "PositionMonitor")
    get_broker = _method_node(service_class, "_get_broker")
    monitor_position = _method_node(service_class, "_monitor_position")
    eod_flatten = _method_node(service_class, "eod_flatten")
    source = POSITION_MONITOR_PATH.read_text()

    assert _zero_filled_adapter_counts(tree) == {"construct": 0, "import": 0}
    assert _zero_filled_adapter_counts(get_broker) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in _call_names(get_broker)
    assert "BrokerProviderRequest" in source
    assert "BrokerProviderCredentials" in source
    assert "worker_position_monitor" in ast.unparse(get_broker)
    assert {"create_order_intent", "submit_order"} <= _call_names(monitor_position)
    assert {"get_positions", "create_order_intent", "submit_order"} <= _call_names(eod_flatten)
    assert _source_contains(monitor_position, "side='sell'")
    assert _source_contains(eod_flatten, "side='sell'")


def test_strategy_runner_is_provider_backed_and_write_capable_for_entries_and_exits() -> None:
    tree = _parse(STRATEGY_RUNNER_PATH)
    service_class = _class_node(tree, "StrategyRunner")
    get_broker = _method_node(service_class, "_get_broker")
    process_ticker = _method_node(service_class, "_process_ticker")
    check_exit = _method_node(service_class, "_check_exit")
    run_all_enabled = _method_node(service_class, "run_all_enabled")
    source = STRATEGY_RUNNER_PATH.read_text()

    assert _zero_filled_adapter_counts(tree) == {"construct": 0, "import": 0}
    assert _zero_filled_adapter_counts(get_broker) == {"construct": 0, "import": 0}
    assert "create_trading212_provider_adapter" in _call_names(get_broker)
    assert "BrokerProviderRequest" in source
    assert "BrokerProviderCredentials" in source
    assert "worker_strategy_runner" in ast.unparse(get_broker)
    assert _source_contains(run_all_enabled, "live_trading_unlocked")
    assert {"create_order_intent", "submit_order"} <= _call_names(process_ticker)
    assert {"create_order_intent", "submit_order"} <= _call_names(check_exit)
    assert _source_contains(process_ticker, "strategy_order_placed")
    assert _source_contains(check_exit, "strategy_exit_placed")


def test_strategy_runner_is_write_capable_for_strategy_entries_and_exits() -> None:
    tree = _parse(STRATEGY_RUNNER_PATH)
    service_class = _class_node(tree, "StrategyRunner")
    process_ticker = _method_node(service_class, "_process_ticker")
    check_exit = _method_node(service_class, "_check_exit")

    assert {"create_order_intent", "submit_order"} <= _call_names(process_ticker)
    assert {"create_order_intent", "submit_order"} <= _call_names(check_exit)
    assert _source_contains(process_ticker, "strategy_order_placed")
    assert _source_contains(check_exit, "strategy_exit_placed")


def test_portfolio_execution_service_is_mixed_write_capable_for_rebalance_orders() -> None:
    tree = _parse(PORTFOLIO_EXECUTION_PATH)
    service_class = _class_node(tree, "PortfolioExecutionService")
    run_strategy_once = _method_node(service_class, "run_strategy_once")

    assert {"get_account_summary", "get_positions"} <= _call_names(service_class)
    assert {"create_order_intent", "submit_order"} <= _call_names(run_strategy_once)
    assert _source_contains(run_strategy_once, "portfolio_rebalance_order")


def test_system_control_is_mixed_write_capable_for_emergency_cancel_and_flatten() -> None:
    tree = _parse(REMAINING_SERVICE_PATHS["system_control"])
    service_class = _class_node(tree, "SystemControlService")
    get_snapshot = _method_node(service_class, "get_snapshot")
    get_positions_summary = _method_node(service_class, "get_positions_summary")
    cancel_all_pending = _method_node(service_class, "cancel_all_pending")
    flatten_all = _method_node(service_class, "flatten_all")
    method_names = {
        node.name
        for node in service_class.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }

    assert method_names >= SYSTEM_CONTROL_READ_STATUS_METHODS
    assert method_names >= SYSTEM_CONTROL_EMERGENCY_METHODS
    assert {"get_account_summary", "get_positions"} <= _call_names(get_snapshot)
    assert "get_positions" in _call_names(get_positions_summary)
    assert READ_ONLY_FORBIDDEN_CALLS.isdisjoint(_call_names(get_snapshot))
    assert READ_ONLY_FORBIDDEN_CALLS.isdisjoint(_call_names(get_positions_summary))
    assert "ExecutionEngine" not in ast.unparse(get_snapshot)
    assert "ExecutionEngine" not in ast.unparse(get_positions_summary)
    assert "cancel_order" in _call_names(cancel_all_pending)
    assert {"create_order_intent", "submit_order"} <= _call_names(flatten_all)
    assert "ExecutionEngine" in ast.unparse(cancel_all_pending)
    assert "ExecutionEngine" in ast.unparse(flatten_all)
    assert _source_contains(cancel_all_pending, "emergency_cancel_all")
    assert _source_contains(flatten_all, "emergency_flatten_all")


@pytest.mark.parametrize("path", sorted(MANUAL_SMOKE_PATHS.values()))
def test_manual_smoke_scripts_remain_terminal_only_demo_gated_and_provider_unwired(
    path: Path,
) -> None:
    tree = _parse(path)

    assert _adapter_counts(tree) == {"construct": 1, "import": 1}
    assert "create_trading212_provider_adapter" not in _call_names(tree)
    assert _contains_name(tree, "__name__")
    assert _source_contains(tree, "APP_MODE")
    assert _source_contains(tree, "T212_ENVIRONMENT")
    assert _source_contains(tree, "'demo'")
    assert _source_contains(tree, "LIVE_TRADING_ENABLED")


def test_manual_multi_order_smoke_keeps_read_only_write_guard() -> None:
    tree = _parse(MANUAL_SMOKE_PATHS["multi_order_reconciliation_smoke"])
    guard = _class_node(tree, "ReadOnlyBrokerGuard")

    assert "ReadOnlyBrokerGuard" in ast.unparse(tree)
    assert "is_broker_write_method" in _call_names(guard)
    assert "write_calls" in ast.unparse(guard)
