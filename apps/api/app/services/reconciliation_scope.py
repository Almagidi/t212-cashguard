"""Which broker account an adapter speaks for, as far as reconciliation may rely on it."""

from __future__ import annotations

import inspect
from typing import Any


def bound_account_scope(broker: Any) -> str | None:
    """The account scope the provider bound to this adapter, or None if it carries none.

    Read without running any code on the adapter, and by the same rule the execution
    engine uses when it records the scope on a new order, so the two always agree.
    """
    scope = inspect.getattr_static(broker, "account_scope", None)
    if not isinstance(scope, str) or not scope.strip():
        return None
    return scope


def unscoped_orders_are_readable(active_connection_count: int) -> bool:
    """An order with no recorded account can only be attributed when one account exists."""
    return active_connection_count == 1
