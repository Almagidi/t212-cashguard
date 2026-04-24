"""
Helpers for converting rich Python objects into JSON-safe structures.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [to_jsonable(item) for item in value]
    return str(value)
