"""Typed broker adapter protocols.

These protocols document the narrow broker surface the app currently relies on.
They are intentionally not wired into dependency injection yet, so importing
this module does not change Trading 212 runtime behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from decimal import Decimal


BROKER_PROTOCOL_WRITE_METHODS: frozenset[str] = frozenset(
    {
        "cancel_order",
        "place_limit_order",
        "place_market_order",
        "place_stop_limit_order",
        "place_stop_order",
    }
)


class BrokerEnvironmentProtocol(Protocol):
    """Minimum environment metadata used by safety and reconciliation gates."""

    environment: str


class ReconciliationHistoryBrokerProtocol(BrokerEnvironmentProtocol, Protocol):
    """Read-only broker history surface used by DEMO order reconciliation."""

    async def get_historical_orders(
        self,
        cursor: int | None = None,
        ticker: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...


class ReadOnlyBrokerProtocol(ReconciliationHistoryBrokerProtocol, Protocol):
    """Read-only broker methods used by account, order, and reconciliation paths."""

    async def get_account_summary(self) -> dict[str, Any]: ...

    async def get_account_metadata(self) -> dict[str, Any]: ...

    async def get_instruments(self) -> list[dict[str, Any]]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def get_pending_orders(self) -> list[dict[str, Any]]: ...

    async def get_order_by_id(self, order_id: str) -> dict[str, Any]: ...

    async def test_connection(self) -> dict[str, Any]: ...


class OrderPlacementBrokerProtocol(ReadOnlyBrokerProtocol, Protocol):
    """Broker write-like methods currently expected by execution paths."""

    async def place_market_order(
        self,
        ticker: str,
        quantity: Decimal,
        time_validity: str = "DAY",
    ) -> dict[str, Any]: ...

    async def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]: ...

    async def place_stop_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]: ...

    async def place_stop_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        *,
        time_validity: str = "DAY",
    ) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> None: ...
