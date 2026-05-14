"""Broker adapter safety inventories."""

from __future__ import annotations

TRADING212_BROKER_WRITE_METHODS: frozenset[str] = frozenset(
    {
        "cancel_order",
        "modify_order",
        "place_limit_order",
        "place_market_order",
        "place_order",
        "place_stop_limit_order",
        "place_stop_order",
        "submit_order",
    }
)


def is_broker_write_method(name: str) -> bool:
    """Return True when a broker method name is classified as write-like."""
    return name in TRADING212_BROKER_WRITE_METHODS
