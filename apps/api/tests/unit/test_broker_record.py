"""A broker's later record of an order settles it only when the record is about that order
and says everything the final state depends on."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.execution.broker_record import (
    FinalRecord,
    LocalOrder,
    OpenRecord,
    UntrustedRecord,
    has_older_records,
    history_items,
    interpret_history,
    interpret_open_order_read,
    records_of,
    require_reads_agree,
    require_same_order,
)

FIVE = Decimal("5")


def _order(**changes: Any) -> LocalOrder:
    facts: dict[str, Any] = {
        "broker_order_id": "B-1",
        "ticker": "AAPL_US_EQ",
        "side": "buy",
        "order_type": "limit",
        "quantity": FIVE,
    }
    return LocalOrder(**{**facts, **changes})


def _open_read(answer: Any, **order: Any) -> FinalRecord | OpenRecord:
    return interpret_open_order_read(answer, order=_order(**order))


def _refused_open_read(answer: Any, **order: Any) -> UntrustedRecord:
    with pytest.raises(UntrustedRecord) as refused:
        _open_read(answer, **order)
    return refused.value


def _nested(
    *, status: str = "FILLED", fill: dict[str, Any] | None = None, **order: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {"order": {"id": "B-1", "status": status, **order}}
    if fill is not None:
        item["fill"] = fill
    return item


def _history(*items: Any, **order: Any) -> FinalRecord | OpenRecord | None:
    found = interpret_history({"items": list(items)}, order=_order(**order))
    return None if found is None else found[0]


def _refused_history(*items: Any, **order: Any) -> UntrustedRecord:
    with pytest.raises(UntrustedRecord) as refused:
        _history(*items, **order)
    return refused.value


# ── the open-orders read ────────────────────────────────────────────────────────────────


def test_a_complete_fill_is_accepted() -> None:
    answer = {"id": "B-1", "status": "FILLED", "filledQuantity": 5.0, "filledPrice": "101.5"}

    assert _open_read(answer) == FinalRecord(
        broker_status="FILLED",
        order_status="filled",
        filled_quantity=FIVE,
        fill_price=Decimal("101.5"),
        filled_at=None,
    )


def test_an_answer_without_an_id_is_about_the_order_that_was_asked_for() -> None:
    answer = {"status": "FILLED", "filledQuantity": "5", "filledPrice": "10"}

    assert _open_read(answer).order_status == "filled"  # type: ignore[union-attr]


def test_a_sell_reported_with_a_negative_quantity_is_settled_as_an_amount() -> None:
    answer = {
        "id": "B-1",
        "status": "FILLED",
        "quantity": -5,
        "filledQuantity": -5,
        "filledPrice": 99,
    }

    record = _open_read(answer, side="sell")

    assert isinstance(record, FinalRecord)
    assert record.filled_quantity == FIVE
    assert record.fill_price == Decimal("99")


def test_a_negative_amount_is_refused_for_a_buy() -> None:
    answer = {"id": "B-1", "status": "FILLED", "filledQuantity": -5, "filledPrice": 99}

    assert _refused_open_read(answer).reason == "filled_quantity_sign_contradicts_side"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("id", "B-2", "order_id_differs"),
        ("ticker", "MSFT_US_EQ", "ticker_differs"),
        ("side", "SELL", "side_differs"),
        ("type", "MARKET", "order_type_differs"),
        ("quantity", 4, "quantity_differs"),
        ("quantity", -5, "quantity_sign_contradicts_side"),
        ("quantity", "many", "quantity_not_a_number"),
    ],
)
def test_a_record_about_another_order_is_refused_and_history_is_not_consulted(
    field: str, value: Any, reason: str
) -> None:
    answer = {
        "id": "B-1",
        "ticker": "AAPL_US_EQ",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": 5,
        "status": "FILLED",
        "filledQuantity": 5,
        "filledPrice": 100,
        field: value,
    }

    refusal = _refused_open_read(answer)

    assert refusal.reason == reason
    assert refusal.lacks_detail is False


def test_agreeing_details_are_accepted_whatever_their_letter_case() -> None:
    answer = {
        "id": "B-1",
        "ticker": "aapl_us_eq",
        "side": "Buy",
        "type": "limit",
        "quantity": "5.000",
        "status": "FILLED",
        "filledQuantity": 5,
        "filledPrice": 100,
    }

    assert isinstance(_open_read(answer), FinalRecord)


def test_an_order_type_this_code_does_not_know_is_not_compared() -> None:
    answer = {
        "id": "B-1",
        "type": "TRAILING",
        "status": "FILLED",
        "filledQuantity": 5,
        "filledPrice": 100,
    }

    assert isinstance(_open_read(answer), FinalRecord)


@pytest.mark.parametrize(
    ("changes", "reason", "lacks_detail"),
    [
        ({"filledQuantity": None}, "filled_quantity_missing", True),
        ({"filledPrice": None}, "fill_price_missing", True),
        ({"filledQuantity": 0}, "filled_without_a_fill", False),
        ({"filledQuantity": 4}, "filled_quantity_below_order", False),
        ({"filledQuantity": 6}, "filled_quantity_exceeds_order", False),
        ({"filledQuantity": "NaN"}, "filled_quantity_not_a_number", False),
        ({"filledQuantity": True}, "filled_quantity_not_a_number", False),
        ({"filledPrice": "Infinity"}, "fill_price_not_a_number", False),
        ({"filledPrice": "abc"}, "fill_price_not_a_number", False),
        ({"filledPrice": 0}, "fill_price_not_positive", False),
        ({"filledPrice": -1}, "fill_price_not_positive", False),
        ({"filledPrice": "0.000000001"}, "fill_price_not_positive", False),
        ({"filledPrice": "1e999999"}, "fill_price_too_large", False),
        ({"filledPrice": "1000000000000"}, "fill_price_too_large", False),
    ],
)
def test_a_fill_is_not_recorded_without_a_usable_quantity_and_price(
    changes: dict[str, Any], reason: str, lacks_detail: bool
) -> None:
    answer = {"id": "B-1", "status": "FILLED", "filledQuantity": 5, "filledPrice": 100, **changes}

    refusal = _refused_open_read(answer)

    assert refusal.reason == reason
    assert refusal.lacks_detail is lacks_detail


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_a_cancelled_or_rejected_order_keeps_the_part_that_filled(status: str) -> None:
    answer = {"id": "B-1", "status": status, "filledQuantity": 2, "filledPrice": "101"}

    assert _open_read(answer) == FinalRecord(
        broker_status=status,
        order_status=status.lower(),
        filled_quantity=Decimal("2"),
        fill_price=Decimal("101"),
        filled_at=None,
    )


def test_the_largest_price_the_column_holds_is_accepted() -> None:
    answer = {
        "id": "B-1",
        "status": "FILLED",
        "filledQuantity": 5,
        "filledPrice": "999999999999.99999999",
    }

    record = _open_read(answer)

    assert isinstance(record, FinalRecord)
    assert record.fill_price == Decimal("999999999999.99999999")


def test_an_unfilled_cancellation_carries_no_price_whatever_the_record_puts_there() -> None:
    answer = {"id": "B-1", "status": "CANCELLED", "filledQuantity": 0, "filledPrice": 0}

    record = _open_read(answer)

    assert isinstance(record, FinalRecord)
    assert record.filled_quantity == Decimal("0")
    assert record.fill_price is None


@pytest.mark.parametrize(
    ("changes", "reason", "lacks_detail"),
    [
        ({"filledQuantity": None}, "filled_quantity_missing", True),
        ({"filledQuantity": 2, "filledPrice": None}, "fill_price_missing", True),
        ({"filledQuantity": 2, "filledPrice": 0}, "fill_price_not_positive", False),
        ({"filledQuantity": 6}, "filled_quantity_exceeds_order", False),
        ({"filledQuantity": "x"}, "filled_quantity_not_a_number", False),
    ],
)
def test_the_open_orders_read_never_settles_a_cancellation_whose_fill_it_does_not_state(
    changes: dict[str, Any], reason: str, lacks_detail: bool
) -> None:
    answer = {
        "id": "B-1",
        "status": "CANCELLED",
        "filledQuantity": 0,
        "filledPrice": 0,
        **changes,
    }

    refusal = _refused_open_read(answer)

    assert refusal.reason == reason
    assert refusal.lacks_detail is lacks_detail


@pytest.mark.parametrize("status", ["WORKING", "PENDING", "NEW", "", None])
def test_anything_but_a_final_status_leaves_the_order_open(status: Any) -> None:
    answer = {"id": "B-1", "status": status, "filledQuantity": 0}

    assert _open_read(answer) == OpenRecord(
        broker_status=status.upper() if isinstance(status, str) else ""
    )


@pytest.mark.parametrize("answer", [None, [], "FILLED", 7])
def test_an_answer_that_is_not_an_object_is_refused(answer: Any) -> None:
    assert _refused_open_read(answer).reason == "not_an_object"


# ── is the record about this order at all ───────────────────────────────────────────────


def test_what_a_record_does_not_state_is_not_held_against_it() -> None:
    require_same_order({}, _order())
    require_same_order({"status": "FILLED", "filledQuantity": 99}, _order())
    require_same_order({"order": {"id": "B-1"}}, _order())


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({"id": "B-2"}, "order_id_differs"),
        ({"order": {"id": "B-2"}}, "order_id_differs"),
        ({"id": True}, "order_id_differs"),
        ({"ticker": "MSFT_US_EQ"}, "ticker_differs"),
        ({"order": {"instrument": {"ticker": "MSFT_US_EQ"}}}, "ticker_differs"),
        ({"side": "sell"}, "side_differs"),
        ({"type": "STOP_LIMIT"}, "order_type_differs"),
        ({"orderedQuantity": 6}, "quantity_differs"),
        ({"quantity": "Infinity"}, "quantity_not_a_number"),
        ("FILLED", "not_an_object"),
    ],
)
def test_a_record_that_states_another_order_is_refused(record: Any, reason: str) -> None:
    with pytest.raises(UntrustedRecord) as refused:
        require_same_order(record, _order())

    assert refused.value.reason == reason
    assert refused.value.lacks_detail is False


# ── the order history ───────────────────────────────────────────────────────────────────


def test_history_without_the_orders_id_says_nothing() -> None:
    other = _nested(filledQuantity=5, fill={"quantity": 5, "price": 10})
    other["order"]["id"] = "B-10"

    assert _history(other) is None
    assert _history() is None


def test_ids_are_matched_whole_never_by_their_beginning() -> None:
    # Surrounding space is dropped and a whole number is read as its digits, as for the
    # id in the broker's immediate answer; a longer id that begins the same way is another.
    page = {"items": [{"id": "B-10"}, {"id": "B-1 "}, {"id": 1}, {"order": {"id": "B-1"}}, "x"]}

    assert records_of(page, "B-1") == [{"id": "B-1 "}, {"order": {"id": "B-1"}}]
    assert records_of(page, "1") == [{"id": 1}]


def test_a_history_page_may_be_an_object_or_a_bare_list() -> None:
    assert history_items({"items": [{"id": "B-1"}, 3]}) == [{"id": "B-1"}]
    assert history_items([{"id": "B-1"}]) == [{"id": "B-1"}]
    assert history_items({"items": []}) == []


@pytest.mark.parametrize("answer", [{"items": None}, {}, "nothing", None, 7, {"items": "x"}])
def test_an_answer_that_is_not_a_page_of_records_is_not_an_empty_page(answer: Any) -> None:
    with pytest.raises(UntrustedRecord) as refused:
        history_items(answer)

    assert refused.value.reason == "history_not_understood"
    with pytest.raises(UntrustedRecord):
        interpret_history(answer, order=_order())


@pytest.mark.parametrize(
    ("page", "older"),
    [
        ({"items": [{}] * 50}, True),
        ({"items": [{}] * 49}, False),
        ({"items": [{}] * 50, "nextPagePath": None}, False),
        ({"items": [{}] * 3, "nextPagePath": "/api/v0/equity/history/orders?cursor=9"}, True),
        ({"items": [{}] * 3, "nextPagePath": ""}, False),
        ([{}] * 50, True),
    ],
)
def test_older_records_exist_when_the_broker_says_so_or_the_page_is_full(
    page: Any, older: bool
) -> None:
    assert has_older_records(page, 50) is older


def test_a_nested_history_record_settles_a_fill_with_its_time() -> None:
    item = _nested(
        ticker="AAPL_US_EQ",
        side="BUY",
        type="LIMIT",
        quantity=5,
        filledQuantity=5,
        fill={"quantity": 5, "price": 101.25, "filledAt": "2026-10-05T14:00:03Z"},
    )

    assert _history(item) == FinalRecord(
        broker_status="FILLED",
        order_status="filled",
        filled_quantity=FIVE,
        fill_price=Decimal("101.25"),
        filled_at=datetime(2026, 10, 5, 14, 0, 3, tzinfo=UTC),
    )


def test_a_flat_history_record_settles_a_fill() -> None:
    item = {
        "id": "B-1",
        "ticker": "AAPL_US_EQ",
        "type": "LIMIT",
        "status": "FILLED",
        "orderedQuantity": 5,
        "filledQuantity": 5,
        "fillPrice": 101.25,
        "dateExecuted": "2026-10-05T14:00:03+00:00",
    }

    record = _history(item)

    assert isinstance(record, FinalRecord)
    assert (record.filled_quantity, record.fill_price) == (FIVE, Decimal("101.25"))
    assert record.filled_at == datetime(2026, 10, 5, 14, 0, 3, tzinfo=UTC)


def test_a_sell_in_history_with_negative_quantities_is_settled_as_an_amount() -> None:
    item = _nested(quantity=-5, filledQuantity=-5, fill={"quantity": -5, "price": 99})

    record = _history(item, side="sell")

    assert isinstance(record, FinalRecord)
    assert record.filled_quantity == FIVE


@pytest.mark.parametrize("status", ["FILLED", "CANCELLED"])
def test_a_fill_on_its_own_is_never_taken_for_the_total(status: str) -> None:
    item = _nested(status=status, fill={"quantity": 2, "price": 99})

    assert _refused_history(item).reason == "filled_quantity_missing"


def test_a_cancellation_in_history_has_to_say_that_nothing_filled() -> None:
    assert _history(_nested(status="CANCELLED", filledQuantity=0)) == FinalRecord(
        broker_status="CANCELLED",
        order_status="cancelled",
        filled_quantity=Decimal("0"),
        fill_price=None,
        filled_at=None,
    )
    for silent in (
        _nested(status="CANCELLED"),
        _nested(status="REJECTED"),
        {"id": "B-1", "status": "CANCELLED", "filledQty": 3, "avgPrice": 10},
    ):
        refusal = _refused_history(silent)
        assert refusal.reason == "filled_quantity_missing"
        assert refusal.lacks_detail is False


def test_a_cancellation_in_history_keeps_the_part_that_filled() -> None:
    item = _nested(status="CANCELLED", filledQuantity=2, fill={"quantity": 2, "price": 50})

    record = _history(item)

    assert isinstance(record, FinalRecord)
    assert (record.order_status, record.filled_quantity, record.fill_price) == (
        "cancelled",
        Decimal("2"),
        Decimal("50"),
    )


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_nested(status="CANCELLED", fill={"price": 50}), "fill_without_its_quantity"),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"price": 50}),
            "fill_without_its_quantity",
        ),
        (_nested(status="CANCELLED", fill={"price": -5}), "fill_price_not_positive"),
        (_nested(status="WORKING"), "history_record_not_final"),
        (_nested(status="EXPIRED", filledQuantity=0), "history_record_not_final"),
        ({"order": {"id": "B-1", "filledQuantity": 5}}, "history_record_not_final"),
        (
            {"order": {"id": "B-1", "status": "WORKING"}, "status": "FILLED"},
            "record_contradicts_itself",
        ),
        (
            {
                "id": "B-1",
                "status": "FILLED",
                "filledQuantity": 5,
                "filledPrice": 999,
                "fill": {"quantity": 5, "price": 1},
            },
            "record_contradicts_itself",
        ),
        (
            {"order": {"id": "B-1", "status": "FILLED", "quantity": 5}, "orderedQuantity": 6},
            "record_contradicts_itself",
        ),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"quantity": 6, "price": 50}),
            "fill_does_not_cover_filled_quantity",
        ),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"quantity": "x", "price": 50}),
            "filled_quantity_not_a_number",
        ),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"quantity": -5, "price": 50}),
            "filled_quantity_sign_contradicts_side",
        ),
        (
            _nested(status="CANCELLED", filledQuantity=6, fill={"quantity": 6, "price": 50}),
            "filled_quantity_exceeds_order",
        ),
        (_nested(status="FILLED", filledQuantity="1e999999"), "filled_quantity_too_large"),
        (_nested(status="CANCELLED", filledQuantity=2), "fill_price_missing"),
        (_nested(status="FILLED"), "filled_quantity_missing"),
        (_nested(status="FILLED", filledQuantity=5), "fill_price_missing"),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"quantity": 2, "price": 50}),
            "fill_does_not_cover_filled_quantity",
        ),
        (_nested(status="FILLED", ticker="MSFT_US_EQ"), "ticker_differs"),
        (
            _nested(status="FILLED", filledQuantity=5, fill={"quantity": 5, "price": "Infinity"}),
            "fill_price_not_a_number",
        ),
    ],
)
def test_a_history_record_that_cannot_be_relied_on_is_refused(
    item: dict[str, Any], reason: str
) -> None:
    assert _refused_history(item).reason == reason


def test_one_record_per_fill_is_combined_when_the_fills_add_up_to_the_stated_total() -> None:
    first = _nested(
        filledQuantity=5, fill={"quantity": 2, "price": 100, "filledAt": "2026-10-05T14:00:00Z"}
    )
    second = _nested(
        filledQuantity=5, fill={"quantity": 3, "price": 110, "filledAt": "2026-10-05T14:01:00Z"}
    )

    record = _history(first, second)

    assert record == FinalRecord(
        broker_status="FILLED",
        order_status="filled",
        filled_quantity=FIVE,
        fill_price=Decimal("106.00000000"),
        filled_at=datetime(2026, 10, 5, 14, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        (
            _nested(filledQuantity=5, fill={"quantity": 2, "price": 100}),
            _nested(status="CANCELLED", filledQuantity=5, fill={"quantity": 3, "price": 110}),
            "records_disagree_on_status",
        ),
        (
            _nested(filledQuantity=5, fill={"quantity": 2, "price": 100}),
            _nested(filledQuantity=4, fill={"quantity": 3, "price": 110}),
            "records_disagree_on_filled_quantity",
        ),
        (
            _nested(filledQuantity=5, fill={"quantity": 5, "price": 100}),
            _nested(filledQuantity=5, fill={"quantity": 5, "price": 100}),
            "fills_do_not_add_up",
        ),
        (
            _nested(fill={"quantity": 2, "price": 100}),
            _nested(fill={"quantity": 3, "price": 110}),
            "several_records_without_their_fills",
        ),
        (
            _nested(filledQuantity=5, fill={"quantity": 2, "price": 100}),
            _nested(filledQuantity=5),
            "several_records_without_their_fills",
        ),
        (
            _nested(filledQuantity=5, fill={"quantity": 2, "price": 100}),
            _nested(filledQuantity=5, ticker="MSFT_US_EQ", fill={"quantity": 3, "price": 110}),
            "ticker_differs",
        ),
    ],
)
def test_several_records_that_do_not_add_up_settle_nothing(
    first: dict[str, Any], second: dict[str, Any], reason: str
) -> None:
    assert _refused_history(first, second).reason == reason


def test_several_unfilled_records_of_a_cancellation_agree_on_no_fill() -> None:
    first = _nested(status="CANCELLED", filledQuantity=0, fill={"quantity": 0, "price": 0})
    second = _nested(status="CANCELLED", filledQuantity=0, fill={"quantity": 0, "price": 0})

    record = _history(first, second)

    assert isinstance(record, FinalRecord)
    assert (record.filled_quantity, record.fill_price) == (Decimal("0"), None)


def test_the_same_fact_written_twice_the_same_way_is_no_contradiction() -> None:
    item = {
        "id": "B-1",
        "status": "filled ",
        "quantity": "5.0",
        "filledQuantity": 5,
        "filledPrice": "101.50",
        "order": {"id": "B-1", "status": "FILLED", "quantity": 5, "filledQuantity": 5.0},
        "fill": {"quantity": 5, "price": 101.5},
    }

    record = _history(item)

    assert isinstance(record, FinalRecord)
    assert record.fill_price == Decimal("101.50")


def test_a_sells_amount_written_with_and_without_its_sign_is_one_amount() -> None:
    item = {
        "order": {"id": "B-1", "status": "FILLED", "quantity": -5, "filledQuantity": -5},
        "orderedQuantity": 5,
        "filledQuantity": 5,
        "fill": {"quantity": -5, "price": 99},
    }

    record = _history(item, side="sell")

    assert isinstance(record, FinalRecord)
    assert record.filled_quantity == FIVE


def test_refusals_say_whether_the_record_is_about_another_order() -> None:
    assert UntrustedRecord("ticker_differs").about_another_order
    assert UntrustedRecord("record_contradicts_itself").about_another_order
    assert not UntrustedRecord("fill_price_missing").about_another_order
    assert not UntrustedRecord("history_record_not_final").about_another_order


def test_a_fill_at_no_price_among_several_is_refused() -> None:
    first = _nested(filledQuantity=5, fill={"quantity": 2, "price": 0})
    second = _nested(filledQuantity=5, fill={"quantity": 3, "price": 110})

    assert _refused_history(first, second).reason == "fill_price_not_positive"


def test_fills_that_add_up_to_less_than_the_stated_total_are_refused() -> None:
    first = _nested(filledQuantity=5, fill={"quantity": 2, "price": 100})
    second = _nested(filledQuantity=5, fill={"quantity": 2, "price": 110})

    assert _refused_history(first, second).reason == "fills_do_not_add_up"


def test_the_average_of_several_fills_is_kept_to_the_stored_scale() -> None:
    first = _nested(filledQuantity=3, quantity=3, fill={"quantity": 1, "price": 10})
    second = _nested(filledQuantity=3, quantity=3, fill={"quantity": 2, "price": 11})

    record = _history(first, second, quantity=Decimal("3"))

    assert isinstance(record, FinalRecord)
    assert str(record.fill_price) == "10.66666667"


def test_the_largest_prices_the_column_holds_are_still_averaged() -> None:
    first = _nested(filledQuantity=5, fill={"quantity": 2, "price": "9e11"})
    second = _nested(filledQuantity=5, fill={"quantity": 3, "price": "9e11"})

    record = _history(first, second)

    assert isinstance(record, FinalRecord)
    assert record.fill_price == Decimal("9e11")


def test_every_record_the_verdict_rests_on_is_returned() -> None:
    first = _nested(filledQuantity=5, fill={"quantity": 2, "price": 100})
    second = _nested(filledQuantity=5, fill={"quantity": 3, "price": 110})

    found = interpret_history({"items": [first, {"id": "B-2"}, second]}, order=_order())

    assert found is not None
    assert found[1] == [first, second]


# ── amounts are compared as they are stored ─────────────────────────────────────────────


def test_float_noise_in_an_amount_is_not_a_difference() -> None:
    answer = {
        "id": "B-1",
        "status": "FILLED",
        "quantity": 0.30000000000000004,
        "filledQuantity": 0.1 + 0.2,
        "filledPrice": 5,
    }

    record = _open_read(answer, quantity=Decimal("0.3"))

    assert isinstance(record, FinalRecord)
    assert record.filled_quantity == Decimal("0.3")


def test_an_amount_that_differs_at_the_stored_scale_is_a_difference() -> None:
    answer = {"id": "B-1", "status": "WORKING", "quantity": 0.123456}

    assert _refused_open_read(answer, quantity=Decimal("0.12345678")).reason == "quantity_differs"


def test_a_timestamp_without_a_zone_is_read_as_utc() -> None:
    item = _nested(
        filledQuantity=5, fill={"quantity": 5, "price": 1, "filledAt": "2026-10-05T14:00:03"}
    )

    record = _history(item)

    assert isinstance(record, FinalRecord)
    assert record.filled_at == datetime(2026, 10, 5, 14, 0, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("order_type", "reported", "refused"),
    [
        ("stop", "STOP", False),
        ("stop", "STOP_LIMIT", True),
        ("stop_limit", "STOP_LIMIT", False),
        ("stop_limit", "LIMIT", True),
        ("market", " market ", False),
    ],
)
def test_each_order_type_is_compared_with_its_own_name(
    order_type: str, reported: str, refused: bool
) -> None:
    record = {"id": "B-1", "type": reported, "ticker": " aapl_us_eq ", "side": " BUY "}
    if refused:
        with pytest.raises(UntrustedRecord) as refusal:
            require_same_order(record, _order(order_type=order_type))
        assert refusal.value.reason == "order_type_differs"
    else:
        require_same_order(record, _order(order_type=order_type))


def test_an_order_id_written_as_order_id_is_compared() -> None:
    require_same_order({"orderId": "B-1"}, _order())
    with pytest.raises(UntrustedRecord) as refusal:
        require_same_order({"orderId": "B-2"}, _order())
    assert refusal.value.reason == "order_id_differs"


def test_the_smallest_price_the_column_holds_is_a_price() -> None:
    answer = {"id": "B-1", "status": "FILLED", "filledQuantity": 5, "filledPrice": "0.00000001"}

    record = _open_read(answer)

    assert isinstance(record, FinalRecord)
    assert record.fill_price == Decimal("0.00000001")


# ── the history may complete an answer, not overrule it ─────────────────────────────────


def _incomplete(answer: dict[str, Any]) -> UntrustedRecord:
    refusal = _refused_open_read(answer)
    assert refusal.lacks_detail is True
    return refusal


def _final(status: str, filled: str, price: str | None = "10") -> FinalRecord:
    return FinalRecord(
        broker_status=status,
        order_status=status.lower(),
        filled_quantity=Decimal(filled),
        fill_price=Decimal(price) if price is not None else None,
        filled_at=None,
    )


def test_a_history_record_that_agrees_with_the_answer_completes_it() -> None:
    without_price = _incomplete({"id": "B-1", "status": "FILLED", "filledQuantity": 5})
    without_amount = _incomplete({"id": "B-1", "status": "CANCELLED"})

    require_reads_agree(without_price, _final("FILLED", "5"))
    require_reads_agree(without_amount, _final("CANCELLED", "2"))
    require_reads_agree(without_amount, _final("CANCELLED", "0", None))


@pytest.mark.parametrize(
    ("answer", "history"),
    [
        ({"id": "B-1", "status": "FILLED", "filledQuantity": 5}, _final("CANCELLED", "0", None)),
        ({"id": "B-1", "status": "CANCELLED"}, _final("FILLED", "5")),
        ({"id": "B-1", "status": "CANCELLED", "filledQuantity": 2}, _final("CANCELLED", "3")),
        ({"id": "B-1", "status": "REJECTED"}, _final("CANCELLED", "0", None)),
    ],
)
def test_a_history_record_that_tells_another_story_than_the_answer_is_refused(
    answer: dict[str, Any], history: FinalRecord
) -> None:
    with pytest.raises(UntrustedRecord) as refused:
        require_reads_agree(_incomplete(answer), history)

    assert refused.value.reason == "reads_disagree"
    assert refused.value.lacks_detail is False
