"""Broker-neutral snapshot models.

These dataclasses are data containers only. They do not perform broker reads,
writes, database access, or runtime adapter construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from decimal import Decimal


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    broker: str
    environment: str
    currency: str | None
    cash: Decimal | None
    free_funds: Decimal | None
    blocked_funds: Decimal | None
    total_value: Decimal | None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    broker: str
    environment: str
    broker_order_id: str
    ticker: str | None
    status: str | None
    side: str | None
    order_type: str | None
    quantity: Decimal | None
    filled_quantity: Decimal | None
    average_fill_price: Decimal | None
    currency: str | None
    created_at: datetime | None
    filled_at: datetime | None
    raw: Mapping[str, Any] | None = None
