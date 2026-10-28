"""The request identity binds exact validated paper semantics, not rounded floats."""

from decimal import Decimal, localcontext
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.api.schemas import PaperOrderCreate
from app.db.models import User
from app.execution.paper_operations import canonical_request, request_digest


def body(**changes):
    return PaperOrderCreate(ticker="EXAMPLE", side="buy", quantity="1", **changes)


def test_identity_is_not_part_of_payload_fingerprint():
    a = body(operation_id=uuid4())
    b = body(operation_id=uuid4())
    assert canonical_request(a) == canonical_request(b)
    assert request_digest(canonical_request(a)) == request_digest(canonical_request(b))


def test_decimal_binding_is_lossless_even_under_small_decimal_context():
    a = body(estimated_price=Decimal("123456789012.12345678"))
    with localcontext() as context:
        context.prec = 3
        assert canonical_request(a)["estimated_price"] == "123456789012.12345678"
    assert canonical_request(body(estimated_price="100.00")) == canonical_request(body())
    assert canonical_request(body(estimated_price="100.00000001")) != canonical_request(body())


def test_complete_defaults_nulls_and_simulation_are_bound():
    value = canonical_request(body())
    assert value["strategy"] is None
    assert value["notional"] is None
    assert value["simulation_profile"] == "standard"
    assert canonical_request(body(simulation_profile="partial_fill")) != value
    assert canonical_request(body(source="another-intent")) != value


def test_compatible_user_mutex_sql():
    from sqlalchemy import select

    query = select(User.id).with_for_update(key_share=True)
    assert str(query.compile(dialect=postgresql.dialect())).endswith("FOR NO KEY UPDATE")
