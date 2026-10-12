"""Reconciliation sees the broker through two reads. It cannot reach a write."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.broker.trading212 import T212APIError, T212AuthError, T212RateLimitError
from app.services import reconciliation_broker
from app.services.reconciliation_broker import ReadOnlyBroker, failure_outcome
from app.services.reconciliation_scope import bound_account_scope, unscoped_orders_are_readable


class Adapter:
    environment = "demo"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.answer: Any = {"status": "WORKING"}
        self.hang = False

    async def _reply(self) -> Any:
        if self.hang:
            await asyncio.Event().wait()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def get_order_by_id(self, broker_order_id: str) -> Any:
        self.calls.append(("get_order_by_id", broker_order_id))
        return await self._reply()

    async def get_historical_orders(self, **kwargs: Any) -> Any:
        self.calls.append(("get_historical_orders", kwargs))
        return await self._reply()

    async def place_market_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a write reached the adapter")

    async def cancel_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a write reached the adapter")


@pytest.mark.parametrize(
    "name",
    [
        "place_market_order",
        "place_limit_order",
        "place_stop_order",
        "place_stop_limit_order",
        "cancel_order",
        "modify_order",
        "_submit_order",
        "_request",
        "client",
        "api_key",
        "api_secret",
        "_adapter_secret",
    ],
)
def test_nothing_but_the_two_reads_can_be_reached(name: str) -> None:
    broker = ReadOnlyBroker(Adapter(), account_scope="scope")

    assert not hasattr(broker, name)


def test_the_public_surface_is_exactly_the_reads_and_their_bookkeeping() -> None:
    broker = ReadOnlyBroker(Adapter(), account_scope="scope")
    public = {name for name in dir(broker) if not name.startswith("_")}

    assert public == {
        "account_scope",
        "begin_order",
        "environment",
        "failed_reads_in_a_row",
        "failure",
        "get_historical_orders",
        "get_order_by_id",
        "reads",
        "stopped",
    }


async def test_reads_are_passed_through_and_counted() -> None:
    adapter = Adapter()
    broker = ReadOnlyBroker(adapter, account_scope="scope")

    assert await broker.get_order_by_id("b-1") == {"status": "WORKING"}
    await broker.get_historical_orders(ticker="AAPL_US_EQ", limit=50)

    assert adapter.calls == [
        ("get_order_by_id", "b-1"),
        ("get_historical_orders", {"ticker": "AAPL_US_EQ", "limit": 50}),
    ]
    assert broker.reads == 2
    assert broker.failure is None
    assert broker.environment == "demo"


async def test_a_failed_read_is_remembered_and_still_raised() -> None:
    adapter = Adapter()
    adapter.answer = T212APIError(503, "down")
    broker = ReadOnlyBroker(adapter, account_scope="scope")

    with pytest.raises(T212APIError):
        await broker.get_order_by_id("b-1")

    assert broker.failure is adapter.answer
    broker.begin_order()
    assert broker.failure is None
    assert broker.reads == 0


async def test_a_read_that_does_not_answer_in_time_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciliation_broker, "BROKER_READ_TIMEOUT_SECONDS", 0.05)
    adapter = Adapter()
    adapter.hang = True
    broker = ReadOnlyBroker(adapter, account_scope="scope")

    with pytest.raises(TimeoutError):
        # The outer limit only keeps this test from hanging if the broker's own is removed.
        async with asyncio.timeout(2):
            await broker.get_order_by_id("b-1")

    assert isinstance(broker.failure, TimeoutError), "the read's own time limit did not fire"


async def test_a_cancelled_read_is_not_recorded_as_a_broker_failure() -> None:
    adapter = Adapter()
    adapter.hang = True
    broker = ReadOnlyBroker(adapter, account_scope="scope")

    read = asyncio.ensure_future(broker.get_order_by_id("b-1"))
    await asyncio.sleep(0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    assert broker.failure is None


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (T212RateLimitError(5.0), "rate_limited"),
        (T212AuthError(401, "x"), "broker_auth_failed"),
        (T212AuthError(403, "x"), "broker_auth_failed"),
        (T212APIError(500, "x"), "broker_read_failed"),
        (T212APIError(503, "x"), "broker_read_failed"),
        (T212APIError(404, "x"), "broker_rejected_read"),
        (T212APIError(400, "x"), "broker_rejected_read"),
        (T212APIError(499, "x"), "broker_rejected_read"),
        (T212APIError(408, "x"), "broker_read_failed"),
        (T212APIError(425, "x"), "broker_read_failed"),
        (TimeoutError(), "broker_read_failed"),
        (ValueError("could not convert string to float"), "broker_read_failed"),
    ],
)
def test_each_kind_of_failure_has_its_own_name(failure: Exception, outcome: str) -> None:
    assert failure_outcome(failure) == outcome


class _ScopeIsAProperty:
    @property
    def account_scope(self) -> str:
        raise AssertionError("reading the scope must not run code on the adapter")


class _Scoped:
    def __init__(self, scope: Any) -> None:
        self.account_scope = scope


@pytest.mark.parametrize(
    ("broker", "scope"),
    [
        (_Scoped("trading212:demo:account:abc"), "trading212:demo:account:abc"),
        (_Scoped(None), None),
        (_Scoped(""), None),
        (_Scoped("   "), None),
        (_Scoped(123), None),
        (object(), None),
        (_ScopeIsAProperty(), None),
    ],
)
def test_the_account_scope_is_read_without_running_adapter_code(broker: Any, scope: Any) -> None:
    assert bound_account_scope(broker) == scope


@pytest.mark.parametrize(
    ("connections", "readable"), [(0, False), (1, True), (2, False), (7, False)]
)
def test_orders_without_an_account_are_attributable_only_to_a_sole_connection(
    connections: int, readable: bool
) -> None:
    assert unscoped_orders_are_readable(connections) is readable
