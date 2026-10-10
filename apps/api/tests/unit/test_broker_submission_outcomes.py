"""Transmission certainty at the Trading 212 order-placement boundary."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.broker.protocols import (
    BrokerSubmissionAmbiguous,
    BrokerSubmissionError,
    BrokerSubmissionNotTransmitted,
    BrokerSubmissionRejected,
)
from app.broker.trading212 import Trading212Adapter
from app.core.config import settings

SENSITIVE_BODY = "account 12345678 insufficient funds for A-SECRET-REFERENCE"


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route the adapter's HTTP client through handler; return the requests it receives."""
    seen: list[httpx.Request] = []

    async def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await handler(request)

    transport = httpx.MockTransport(recording_handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(settings, "APP_MODE", "demo")
    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
    return seen


async def _place(adapter: Trading212Adapter) -> dict:
    return await adapter.place_limit_order("AAPL_US_EQ", Decimal("1"), Decimal("100"))


@pytest.mark.asyncio
async def test_acknowledged_order_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 77, "status": "WORKING"})

    seen = _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        response = await _place(adapter)

    assert response == {"id": 77, "status": "WORKING"}
    assert len(seen) == 1
    assert seen[0].headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol],
)
@pytest.mark.asyncio
async def test_failure_before_transmission_is_not_transmitted(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.TransportError]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise failure("simulated", request=request)

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionNotTransmitted) as raised:
            await _place(adapter)

    assert raised.value.error_type == failure.__name__
    assert raised.value.http_status is None


@pytest.mark.asyncio
async def test_adapter_used_outside_its_context_is_not_transmitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "APP_MODE", "demo")
    adapter = Trading212Adapter("demo-key", "demo-secret", "demo")

    with pytest.raises(BrokerSubmissionNotTransmitted):
        await _place(adapter)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.WriteTimeout,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.LocalProtocolError,
        httpx.CloseError,
        httpx.ProxyError,
        httpx.DecodingError,
    ],
)
@pytest.mark.asyncio
async def test_failure_after_dispatch_started_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.RequestError]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise failure("simulated", request=request)

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionAmbiguous) as raised:
            await _place(adapter)

    assert raised.value.error_type == failure.__name__


@pytest.mark.asyncio
async def test_unclassified_exception_defaults_to_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise OSError("socket vanished")

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionAmbiguous) as raised:
            await _place(adapter)

    assert raised.value.error_type == "OSError"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
@pytest.mark.asyncio
async def test_definitive_refusal_is_rejected_without_echoing_the_body(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=SENSITIVE_BODY, headers={"Retry-After": "0"})

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionRejected) as raised:
            await _place(adapter)

    assert raised.value.http_status == status
    assert "12345678" not in str(raised.value)
    assert "A-SECRET-REFERENCE" not in repr(raised.value)


@pytest.mark.parametrize("status", [408, 409, 425, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_timeout_conflict_gateway_and_server_statuses_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=SENSITIVE_BODY)

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionAmbiguous) as raised:
            await _place(adapter)

    assert raised.value.http_status == status
    assert "12345678" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>gateway page</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"status": "WORKING"}),
        httpx.Response(200, json={"id": "", "status": "WORKING"}),
        httpx.Response(204),
        httpx.Response(302, headers={"Location": "https://example.invalid/"}),
    ],
    ids=["not-json", "not-an-object", "no-order-id", "empty-order-id", "no-content", "redirect"],
)
@pytest.mark.asyncio
async def test_unusable_success_response_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return response

    _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        with pytest.raises(BrokerSubmissionAmbiguous):
            await _place(adapter)


@pytest.mark.asyncio
async def test_every_order_type_goes_through_the_classified_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    seen = _use_transport(monkeypatch, handler)

    async with Trading212Adapter("demo-key", "demo-secret", "demo") as adapter:
        calls = [
            adapter.place_market_order("AAPL_US_EQ", Decimal("1")),
            adapter.place_limit_order("AAPL_US_EQ", Decimal("1"), Decimal("100")),
            adapter.place_stop_order("AAPL_US_EQ", Decimal("-1"), Decimal("90")),
            adapter.place_stop_limit_order(
                "AAPL_US_EQ", Decimal("-1"), Decimal("90"), Decimal("89")
            ),
        ]
        for call in calls:
            with pytest.raises(BrokerSubmissionAmbiguous):
                await call

    assert [request.url.path for request in seen] == [
        "/api/v0/equity/orders/market",
        "/api/v0/equity/orders/limit",
        "/api/v0/equity/orders/stop",
        "/api/v0/equity/orders/stop_limit",
    ]


def test_outcome_exceptions_share_one_base_and_carry_no_payload() -> None:
    for outcome in (
        BrokerSubmissionNotTransmitted,
        BrokerSubmissionRejected,
        BrokerSubmissionAmbiguous,
    ):
        assert issubclass(outcome, BrokerSubmissionError)
        raised = outcome(error_type="ReadTimeout", http_status=None)
        assert raised.error_type == "ReadTimeout"
        assert "ReadTimeout" in str(raised)
