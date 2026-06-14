from __future__ import annotations

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
