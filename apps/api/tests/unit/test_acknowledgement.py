"""A broker's answer to an order request is acted on only when it is complete and agrees
with the order that was sent."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.execution.acknowledgement import (
    MAX_BROKER_ORDER_ID_LENGTH,
    Acknowledgement,
    UntrustedAcknowledgement,
    interpret_acknowledgement,
)

TWO = Decimal("2")


def _interpret(answer: Any, *, side: str = "buy", quantity: Decimal = TWO) -> Acknowledgement:
    return interpret_acknowledgement(answer, side=side, quantity=quantity)


def _refusal(
    answer: Any, *, side: str = "buy", quantity: Decimal = TWO
) -> UntrustedAcknowledgement:
    with pytest.raises(UntrustedAcknowledgement) as refused:
        _interpret(answer, side=side, quantity=quantity)
    return refused.value


def test_a_complete_fill_is_accepted_with_the_orders_own_quantity() -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": 2.0, "filledPrice": "50.25"}

    result = _interpret(answer)

    assert result == Acknowledgement(
        broker_order_id="B1",
        broker_status="FILLED",
        order_status="filled",
        transition_reason="broker returned FILLED",
        filled_quantity=TWO,
        fill_price=Decimal("50.25"),
    )


@pytest.mark.parametrize(
    ("quantity", "reported"),
    [("0.00000001", "0.00000001"), ("1.23456789", 1.23456789), ("250", "250.000"), ("3", 3)],
)
def test_an_exact_fill_is_recognised_however_the_number_is_written(
    quantity: str, reported: Any
) -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": reported, "filledPrice": 1}

    assert _interpret(answer, quantity=Decimal(quantity)).filled_quantity == Decimal(quantity)


@pytest.mark.parametrize(
    ("reported", "side", "reason"),
    [
        (3, "buy", "filled_quantity_exceeds_order"),
        (1, "buy", "filled_quantity_below_order"),
        (-1, "buy", "filled_quantity_sign_contradicts_side"),
        (-2, "buy", "filled_quantity_sign_contradicts_side"),
        (0, "buy", "filled_without_a_fill"),
        ("-0", "sell", "filled_without_a_fill"),
        (-3, "sell", "filled_quantity_exceeds_order"),
        (-1, "sell", "filled_quantity_below_order"),
        ("1e400", "buy", "filled_quantity_exceeds_order"),
        ("1e9999999", "buy", "filled_quantity_exceeds_order"),
        ("-1e9999999", "sell", "filled_quantity_exceeds_order"),
        (True, "buy", "filled_quantity_not_a_number"),
        ("sNaN", "buy", "filled_quantity_not_a_number"),
        (b"2", "buy", "filled_quantity_not_a_number"),
        (2 + 0j, "buy", "filled_quantity_not_a_number"),
    ],
)
def test_a_fill_that_disagrees_with_the_order_is_refused_and_the_order_id_is_kept(
    reported: Any, side: str, reason: str
) -> None:
    answer = {"id": 4711, "status": "FILLED", "filledQuantity": reported, "filledPrice": 50}

    refused = _refusal(answer, side=side)

    assert refused.reason == reason
    assert refused.broker_order_id == "4711"


@pytest.mark.parametrize("side", ["BUY", "Sell", "", "short", "sell "])
def test_only_the_exact_word_sell_allows_a_negative_amount(side: str) -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": -2, "filledPrice": 50}

    assert _refusal(answer, side=side).reason == "filled_quantity_sign_contradicts_side"


def test_the_price_is_checked_only_after_the_quantity() -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": 3}

    assert _refusal(answer).reason == "filled_quantity_exceeds_order"


@pytest.mark.parametrize("price", ["0.00000001", 0.01, "99999999.99", 50])
def test_any_finite_positive_price_is_kept_exactly(price: Any) -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": 2, "filledPrice": price}

    assert _interpret(answer).fill_price == Decimal(str(price))


@pytest.mark.parametrize("price", ["0.000000009", "1e-12", 0, "0.0", -0.01, "-1e-30"])
def test_a_price_that_would_be_stored_as_zero_or_less_is_refused(price: Any) -> None:
    answer = {"id": "B1", "status": "FILLED", "filledQuantity": 2, "filledPrice": price}

    assert _refusal(answer).reason == "fill_price_not_positive"


@pytest.mark.parametrize(
    ("order_id", "kept"),
    [
        ("B1", "B1"),
        ("  B1  ", "B1"),
        (0, "0"),
        (48850886521, "48850886521"),
        ("x" * MAX_BROKER_ORDER_ID_LENGTH, "x" * MAX_BROKER_ORDER_ID_LENGTH),
    ],
)
def test_usable_order_ids(order_id: Any, kept: str) -> None:
    assert _interpret({"id": order_id, "status": "WORKING"}).broker_order_id == kept


@pytest.mark.parametrize(
    "order_id",
    [None, "", " \t", True, False, 1.0, Decimal("5"), b"B1", ["B1"], ("B1",), {"a": 1}, "x" * 101],
)
def test_an_unusable_order_id_refuses_the_whole_answer_and_names_no_order(order_id: Any) -> None:
    refused = _refusal({"id": order_id, "status": "FILLED", "filledQuantity": 2, "filledPrice": 50})

    assert refused.reason == "order_id_not_usable"
    assert refused.broker_order_id is None


@pytest.mark.parametrize("answer", [None, [], "FILLED", 1, (), object()])
def test_an_answer_that_is_not_an_object_is_refused(answer: Any) -> None:
    assert _refusal(answer).reason == "not_an_object"


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
@pytest.mark.parametrize("reported", [None, 0, "0", 0.0, "-0.0"])
def test_a_final_status_with_no_fill_is_accepted_without_inventing_one(
    status: str, reported: Any
) -> None:
    result = _interpret({"id": "B1", "status": status, "filledQuantity": reported})

    assert result.order_status == status.lower()
    assert result.filled_quantity is None
    assert result.fill_price is None
    assert result.transition_reason == f"broker returned {status}"


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
@pytest.mark.parametrize(
    ("reported", "side", "reason"),
    [
        (1, "buy", "final_status_reports_a_fill"),
        (2, "buy", "final_status_reports_a_fill"),
        (-1, "sell", "final_status_reports_a_fill"),
        (-1, "buy", "filled_quantity_sign_contradicts_side"),
        ("x", "buy", "filled_quantity_not_a_number"),
        (float("inf"), "buy", "filled_quantity_not_a_number"),
    ],
)
def test_a_final_status_that_reports_a_fill_is_refused(
    status: str, reported: Any, side: str, reason: str
) -> None:
    refused = _refusal({"id": "B1", "status": status, "filledQuantity": reported}, side=side)

    assert refused.reason == reason
    assert refused.broker_order_id == "B1"


@pytest.mark.parametrize(
    ("status", "broker_status", "reason"),
    [
        ("WORKING", "WORKING", "broker returned working status"),
        ("PENDING", "PENDING", "broker returned working status"),
        ("NEW", "NEW", "broker status defaulted to accepted"),
        ("filled", "filled", "broker status defaulted to accepted"),
        ("", "", "broker status defaulted to accepted"),
        (None, "", "broker status defaulted to accepted"),
        (5, "", "broker status defaulted to accepted"),
        (["FILLED"], "", "broker status defaulted to accepted"),
    ],
)
def test_anything_not_final_keeps_the_order_active_whatever_else_it_says(
    status: Any, broker_status: str, reason: str
) -> None:
    result = _interpret(
        {"id": "B1", "status": status, "filledQuantity": "nonsense", "filledPrice": -1}
    )

    assert result.order_status == "accepted"
    assert result.broker_status == broker_status
    assert result.transition_reason == reason
    assert result.filled_quantity is None


async def test_what_the_mock_broker_answers_is_accepted_unchanged() -> None:
    from app.broker.mock_adapter import MockBrokerAdapter

    broker = MockBrokerAdapter()
    bought = await broker.place_market_order("AAPL_US_EQ", Decimal("2"))
    sold = await broker.place_market_order("AAPL_US_EQ", Decimal("-2"))
    resting = await broker.place_limit_order("AAPL_US_EQ", Decimal("2"), Decimal("10"))

    assert _interpret(bought).order_status == "filled"
    assert _interpret(bought).filled_quantity == TWO
    assert _interpret(sold, side="sell").order_status == "filled"
    assert _interpret(sold, side="sell").filled_quantity == TWO
    assert _interpret(resting).order_status == "accepted"


def test_the_refusal_says_nothing_about_the_answers_contents() -> None:
    refused = _refusal(
        {"id": "B1", "status": "FILLED", "filledQuantity": "s3cr3t-looking", "filledPrice": 5}
    )

    assert "s3cr3t" not in str(refused)
    assert str(refused) == "Broker acknowledgement not trusted (filled_quantity_not_a_number)."
