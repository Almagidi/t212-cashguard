"""Confidentiality at the actual application Sentry callback boundary."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app import main

SECRET = "SYNTHETIC_PRIVACY_VALUE_NOT_A_REAL_SECRET"


def _error_event() -> dict[str, Any]:
    module = ast.parse(Path(main.__file__).read_text())
    function = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "request_logging"
    )
    return {
        "event_id": "a" * 32,
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "safe diagnostic",
                    "stacktrace": {
                        "frames": [
                            {
                                "module": "app.main",
                                "function": "request_logging",
                                "filename": "/private/location/main.py",
                                "lineno": function.body[0].lineno,
                            }
                        ]
                    },
                }
            ]
        },
    }


@pytest.mark.parametrize(
    "location",
    [
        "request",
        "locals",
        "exception",
        "breadcrumb",
        "log",
        "user",
        "transaction",
    ],
)
def test_confidential_input_never_survives_event_boundary(location: str) -> None:
    event = _error_event()
    if location == "request":
        event["request"] = {
            "method": "POST",
            "url": f"https://example.invalid/{SECRET}",
            "headers": {"AuThOrIzAtIoN": SECRET, "Cookie": SECRET, "X-Custom": SECRET},
            "query_string": f"password={SECRET}",
            "cookies": {"session": SECRET},
            "data": [{"password": SECRET, "unknown": {SECRET: SECRET}}],
        }
    elif location == "locals":
        frame = event["exception"]["values"][0]["stacktrace"]["frames"][0]
        frame.update(
            vars={"scope": {"headers": [["authorization", SECRET]]}},
            context_line=SECRET,
            pre_context=[SECRET],
            post_context=[SECRET],
        )
    elif location == "exception":
        event["exception"]["values"][0].update(
            value=f"password={SECRET}",
            mechanism={"type": "asgi", "data": {"raw": SECRET}},
        )
    elif location == "breadcrumb":
        event["breadcrumbs"] = {
            "values": [
                {
                    "category": "query",
                    "level": "info",
                    "message": SECRET,
                    "data": {"nested": [{"password": SECRET}]},
                }
            ]
        }
    elif location == "log":
        event.update(
            message=SECRET,
            logentry={"formatted": SECRET, "params": [SECRET]},
            extra={"nested": {"token": SECRET}},
        )
    elif location == "user":
        event.update(
            user={"email": f"{SECRET}@example.invalid"},
            tags={"secret": SECRET},
            contexts={"custom": {"raw": SECRET}},
        )
    else:
        event.update(
            type="transaction",
            transaction=SECRET,
            spans=[{"op": "db", "description": f"SELECT '{SECRET}'", "data": {"raw": SECRET}}],
            measurements={"private": {"value": SECRET}},
        )
    before = copy.deepcopy(event)
    result = main._scrub_sentry_event(event)
    assert result is not None  # keep useful safe diagnostics, not just drop every event
    assert SECRET not in json.dumps(result)
    assert event == before


def test_exception_type_and_trusted_application_location_remain_useful() -> None:
    result = main._scrub_sentry_event(_error_event())
    assert result is not None
    value = result["exception"]["values"][0]
    assert value["type"] == "RuntimeError"
    frame = value["stacktrace"]["frames"][0]
    assert frame["module"] == "app.main"
    assert frame["function"] == "request_logging"
    assert frame["filename"] == "app/main.py"
    assert frame["lineno"] > 0


def test_unknown_event_shape_is_dropped_instead_of_forwarded() -> None:
    assert main._scrub_sentry_event({"unknown": {"password": SECRET}}) is None


@pytest.mark.parametrize("field", ["module", "function", "filename"])
def test_untrusted_frame_metadata_cannot_smuggle_input(field: str) -> None:
    event = _error_event()
    event["exception"]["values"][0]["stacktrace"]["frames"][0][field] = SECRET
    result = main._scrub_sentry_event(event)
    assert result is not None
    assert SECRET not in json.dumps(result)


def test_only_finite_protocol_metadata_is_retained() -> None:
    event = _error_event()
    event.update(
        timestamp="2026-10-27T12:00:00Z",
        environment="mock",
        release="cashguard@1.0.0",
        tags={"http.status_code": "500", "private": SECRET},
        contexts={
            "trace": {
                "trace_id": "b" * 32,
                "span_id": "c" * 16,
                "op": "http.server",
                "status": "internal_error",
                "data": {"password": SECRET},
            },
            "unknown": SECRET,
        },
    )
    result = main._scrub_sentry_event(event)
    assert result is not None
    assert result["tags"] == {"http.status_code": "500"}
    assert result["contexts"]["trace"]["op"] == "http.server"
    assert result["contexts"]["trace"]["trace_id"] == "b" * 32
    assert SECRET not in json.dumps(result)
    assert main._scrub_sentry_event(result) == result


def test_sanitizer_failure_drops_event_without_logging_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import telemetry_privacy

    def fail(_value: object) -> None:
        raise ValueError(SECRET)

    monkeypatch.setattr(telemetry_privacy, "_exception", fail)
    assert main._scrub_sentry_event(_error_event()) is None


def test_native_dictionary_requirement_avoids_user_defined_hooks() -> None:
    class Untrusted(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("An untrusted dictionary hook was invoked")

    assert main._scrub_sentry_event(Untrusted(password=SECRET)) is None


def test_final_transport_filters_headers_items_and_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sentry_sdk.envelope import Envelope, Item, PayloadRef
    from sentry_sdk.transport import Transport

    from app.core import sentry_transport

    received: list[Envelope] = []
    lifecycle: list[object] = []

    class Sink(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            received.append(envelope)

        def flush(self, timeout: float, callback: object = None) -> None:
            lifecycle.append(("flush", timeout, callback))

        def kill(self) -> None:
            lifecycle.append("kill")

        def is_healthy(self) -> bool:
            return False

        def record_lost_event(self, *args: object, **kwargs: object) -> None:
            lifecycle.append(("lost", args, kwargs))

    sink = Sink()
    dsn = "https://synthetic-public-key@sentry.invalid/1"

    def factory(options: dict[str, Any]) -> Transport:
        assert options["dsn"] == dsn
        assert options["transport"] is None
        return sink

    monkeypatch.setattr(sentry_transport, "make_transport", factory)
    transport = sentry_transport.ConfidentialTransport({"dsn": dsn})
    envelope = Envelope(headers={"event_id": "a" * 32, "trace": {"user": SECRET}, SECRET: SECRET})
    event = _error_event()
    event["exception"]["values"][0]["value"] = SECRET
    envelope.add_item(
        Item(payload=PayloadRef(json=event), type="event", headers={"private": SECRET})
    )
    for kind in [
        "attachment",
        "profile",
        "profile_chunk",
        "session",
        "sessions",
        "client_report",
        "log",
        "metric_buckets",
        "check_in",
        "span",
        "unknown",
    ]:
        envelope.add_item(Item(payload=PayloadRef(json={"private": SECRET}), type=kind))
    # An unparseable item claiming to be an error cannot bypass the native-dict policy.
    envelope.add_item(Item(payload=PayloadRef(bytes=SECRET.encode()), type="event"))
    transport.capture_envelope(envelope)
    assert len(received) == 1
    assert len(received[0].items) == 1
    assert received[0].items[0].type == "event"
    assert SECRET.encode() not in received[0].serialize()
    assert received[0].headers == {"event_id": "a" * 32}
    assert received[0].items[0].headers == {"type": "event", "content_type": "application/json"}
    transport.flush(1.0)
    transport.record_lost_event("test", data_category="error", quantity=2)
    transport.kill()
    assert not transport.is_healthy()
    assert lifecycle[0] == ("flush", 1.0, None)
    assert lifecycle[-1] == "kill"


def test_transport_failure_never_falls_back_to_raw_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sentry_sdk.envelope import Envelope
    from sentry_sdk.transport import Transport

    from app.core import sentry_transport

    received: list[Envelope] = []

    class Sink(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            received.append(envelope)

    monkeypatch.setattr(sentry_transport, "make_transport", lambda _options: Sink())
    transport = sentry_transport.ConfidentialTransport({"dsn": ""})

    def fail(_headers: object) -> None:
        raise ValueError(SECRET)

    monkeypatch.setattr(sentry_transport, "sanitize_envelope_headers", fail)
    transport.capture_envelope(Envelope(headers={"private": SECRET}))
    assert received == []


@pytest.mark.parametrize("kind", ["event", "transaction"])
def test_item_type_subclass_cannot_launder_input_into_serialized_headers(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from sentry_sdk.envelope import Envelope, Item, PayloadRef
    from sentry_sdk.transport import Transport

    from app.core import sentry_transport

    received: list[bytes] = []

    class UntrustedType(str):
        def __eq__(self, other: object) -> bool:
            return other == kind

        __hash__ = str.__hash__

    class Sink(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            received.append(envelope.serialize())

    monkeypatch.setattr(sentry_transport, "make_transport", lambda _options: Sink())
    transport = sentry_transport.ConfidentialTransport({"dsn": ""})
    event = _error_event()
    if kind == "transaction":
        event.update(type="transaction", transaction="private", contexts={})
    envelope = Envelope()
    envelope.add_item(Item(payload=PayloadRef(json=event), type=UntrustedType(SECRET)))
    transport.capture_envelope(envelope)
    assert not any(SECRET.encode() in payload for payload in received)
    assert received == []
