from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from app.core.serialization import to_jsonable


class DemoEnum(Enum):
    VALUE = Decimal("4.25")


class DemoModel(BaseModel):
    id: uuid.UUID
    created_at: datetime
    amount: Decimal


def test_primitives_and_none_pass_through():
    assert to_jsonable(None) is None
    assert to_jsonable("cashguard") == "cashguard"
    assert to_jsonable(12) == 12
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable(True) is True


def test_decimal_uuid_and_datetime_types_are_strings():
    value_id = uuid.uuid4()
    moment = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)

    assert to_jsonable(Decimal("12.34")) == "12.34"
    assert to_jsonable(value_id) == str(value_id)
    assert to_jsonable(moment) == moment.isoformat()
    assert to_jsonable(date(2024, 1, 2)) == "2024-01-02"
    assert to_jsonable(time(9, 30)) == "09:30:00"


def test_enum_model_mapping_and_sequence_are_converted_recursively():
    value_id = uuid.uuid4()
    model = DemoModel(
        id=value_id,
        created_at=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
        amount=Decimal("42.10"),
    )

    output = to_jsonable(
        {
            123: [DemoEnum.VALUE, model],
            "raw_bytes": b"abc",
        }
    )

    assert output["123"][0] == "4.25"
    assert output["123"][1]["id"] == str(value_id)
    assert output["123"][1]["amount"] == "42.10"
    assert output["raw_bytes"] == "b'abc'"
