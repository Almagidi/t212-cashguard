from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution.state_machine import (
    InvalidOrderTransition,
    can_transition_order_status,
    is_terminal_status,
    transition_order_status,
)

LEGAL_TRANSITIONS = {
    "pending_intent": {"submitted", "rejected", "cancelled", "error"},
    "submitted": {
        "accepted",
        "partially_filled",
        "filled",
        "rejected",
        "cancelled",
        "error",
    },
    "accepted": {"partially_filled", "filled", "rejected", "cancelled", "error"},
    "partially_filled": {"filled", "cancelled", "error"},
}


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (from_status, to_status)
        for from_status, to_statuses in LEGAL_TRANSITIONS.items()
        for to_status in to_statuses
    ],
)
def test_legal_order_status_transitions_succeed(from_status: str, to_status: str):
    order = SimpleNamespace(status=from_status)

    transition_order_status(order, to_status)

    assert order.status == to_status
    assert can_transition_order_status(from_status, to_status) is True


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("pending_intent", "filled"),
        ("accepted", "submitted"),
        ("partially_filled", "submitted"),
        ("rejected", "cancelled"),
        ("cancelled", "rejected"),
        ("error", "accepted"),
    ],
)
def test_illegal_order_status_transitions_raise(from_status: str, to_status: str):
    order = SimpleNamespace(status=from_status)

    with pytest.raises(InvalidOrderTransition):
        transition_order_status(order, to_status)

    assert order.status == from_status
    assert can_transition_order_status(from_status, to_status) is False


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("filled", "cancelled"),
        ("filled", "rejected"),
        ("filled", "error"),
        ("filled", "submitted"),
        ("cancelled", "filled"),
        ("cancelled", "submitted"),
        ("rejected", "filled"),
        ("rejected", "submitted"),
        ("error", "filled"),
        ("error", "submitted"),
    ],
)
def test_terminal_order_statuses_are_immutable(from_status: str, to_status: str):
    order = SimpleNamespace(status=from_status)

    assert is_terminal_status(from_status) is True
    with pytest.raises(InvalidOrderTransition):
        transition_order_status(order, to_status)

    assert order.status == from_status


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("unknown", "submitted"),
        ("submitted", "unknown"),
        ("unknown", "unknown"),
    ],
)
def test_unknown_order_statuses_fail_closed(from_status: str, to_status: str):
    order = SimpleNamespace(status=from_status)

    with pytest.raises(InvalidOrderTransition):
        transition_order_status(order, to_status)

    assert order.status == from_status
    assert can_transition_order_status(from_status, to_status) is False


@pytest.mark.parametrize(
    "status",
    [
        "pending_intent",
        "submitted",
        "accepted",
        "partially_filled",
        "filled",
        "cancelled",
        "rejected",
        "error",
    ],
)
def test_same_known_status_transition_is_idempotent_no_op(status: str):
    order = SimpleNamespace(status=status)

    transition_order_status(order, status)

    assert order.status == status
    assert can_transition_order_status(status, status) is True


def test_regression_filled_order_cannot_be_cancelled_locally():
    order = SimpleNamespace(status="filled")

    with pytest.raises(InvalidOrderTransition):
        transition_order_status(order, "cancelled")

    assert order.status == "filled"


def _direct_order_status_assignments(source: str) -> list[int]:
    """Return direct assignments that bypass the persisted-order state machine."""
    violations: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "status"
                and isinstance(target.value, ast.Name)
                and (target.value.id == "order" or target.value.id.endswith("_order"))
            ):
                violations.append(node.lineno)
    return violations


def test_direct_order_status_assignment_detector_catches_bypasses():
    source = 'order.status = "filled"\npaper_order.status: str = "cancelled"\n'

    assert _direct_order_status_assignments(source) == [1, 2]


def test_persistent_order_status_is_only_mutated_by_state_machine():
    app_root = Path(__file__).parents[2] / "app"
    allowed = app_root / "execution" / "state_machine.py"
    violations: list[str] = []

    for path in app_root.rglob("*.py"):
        if path == allowed or "backtest" in path.parts:
            continue
        for line in _direct_order_status_assignments(path.read_text()):
            violations.append(f"{path.relative_to(app_root)}:{line}")

    assert violations == [], (
        "Persistent order status must advance only through the central state machine; "
        f"direct assignment violations: {violations}"
    )


def test_persistent_orders_are_only_constructed_in_the_initial_state():
    app_root = Path(__file__).parents[2] / "app"
    violations: list[str] = []

    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if call_name != "Order":
                continue
            status = next((item.value for item in node.keywords if item.arg == "status"), None)
            if not isinstance(status, ast.Constant) or status.value != "pending_intent":
                violations.append(f"{path.relative_to(app_root)}:{node.lineno}")

    assert violations == [], (
        "Persistent orders must be constructed in pending_intent and advance only "
        f"through the central state machine; violations: {violations}"
    )
