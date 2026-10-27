"""Synthetic wire-envelope regression of actual app callbacks and final transport.

All network operations are blocked before SDK/application import. Only the
transport's outbound delegate is replaced; production privacy policy executes.
"""

import atexit
import hashlib
import importlib.metadata
import json
import os
import socket
import sys
import traceback
from pathlib import Path


def run() -> None:
    RUN_DIR = Path(sys.argv[1])
    RUN_DIR.mkdir()
    attempts = []
    envelopes = []
    init_calls = []
    case = "import"
    report = {"status": "RUNNING", "network_attempts": attempts, "checks": {}, "cases": []}

    def blocked(action, target):
        record = {"case": case, "action": action, "target": repr(target)}
        attempts.append(record)
        (RUN_DIR / "network-attempts.json").write_text(json.dumps(attempts, indent=2))
        raise RuntimeError("NETWORK_TRIPWIRE: " + action)

    def audit(event, args):
        if (
            event == "socket.__new__"
            and len(args) > 1
            and args[1] in (socket.AF_INET, socket.AF_INET6)
        ):
            blocked(event, args[1:])
        if event in (
            "socket.connect",
            "socket.sendto",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
        ):
            blocked(
                event,
                args[1:]
                if event.startswith("socket.") and args and isinstance(args[0], socket.socket)
                else args,
            )

    sys.addaudithook(audit)
    for method in ["connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"]:
        original = getattr(socket.socket, method, None)
        if original is None:
            continue

        def guarded(self, *args, _method=method, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                blocked("socket." + _method, args)
            return _original(self, *args, **kwargs)

        setattr(socket.socket, method, guarded)
    socket.create_connection = lambda *a, **_kwargs: blocked("socket.create_connection", a)
    # urllib3 probes IPv6 by creating and binding a loopback socket at import time.
    # Model this offline harness as IPv4-only so no socket is created at all.
    # All IPv4/IPv6 socket creation and network operations remain blocked globally.
    socket.has_ipv6 = False
    report["harness_constraints"] = {
        "transport": "Production envelope boundary with MemoryTransport delegate",
        "socket_has_ipv6": False,
        "reason": "Prevent urllib3 import-time IPv6 loopback capability bind; no application privacy policy replacement",
    }

    def finish():
        report["network_attempts"] = attempts
        report["envelope_count"] = len(envelopes)
        report["init_call_count"] = len(init_calls)
        (RUN_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str))
        (RUN_DIR / "envelopes.json").write_text(json.dumps(envelopes, indent=2, default=str))
        (RUN_DIR / "network-attempts.json").write_text(json.dumps(attempts, indent=2))

    atexit.register(finish)

    if len(sys.argv) > 2 and sys.argv[2] == "--tripwire-selftest":
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except RuntimeError as exc:
            assert str(exc) == "NETWORK_TRIPWIRE: socket.__new__"
        else:
            raise AssertionError("Tripwire did not block Internet socket creation")
        assert len(attempts) == 1
        report["status"] = "PASS_TRIPWIRE_SELFTEST"
        report["checks"]["blocked_before_any_sdk_or_app_import"] = (
            "sentry_sdk" not in sys.modules and "app.main" not in sys.modules
        )
        sys.exit(0)

    def sentinel(label):
        return "SYNTHETIC_CG325_" + label + "_NOT_REAL"

    def locations(value, token, prefix="$"):
        found = []
        if isinstance(value, dict):
            for key, child in value.items():
                found.extend(locations(child, token, prefix + "." + str(key)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(locations(child, token, prefix + "[" + str(index) + "]"))
        elif isinstance(value, str) and token in value:
            found.append(prefix)
        return found

    def summarize(name, start, labels):
        items = []
        for envelope in envelopes[start:]:
            for item in envelope["items"]:
                payload = item.get("json")
                if payload is not None:
                    items.append(
                        {
                            "type": item["headers"].get("type"),
                            "transaction": payload.get("transaction"),
                            "event_id": payload.get("event_id"),
                            "sentinel_locations": {
                                label: locations(payload, sentinel(label)) for label in labels
                            },
                        }
                    )
        report["cases"].append({"name": name, "envelopes": len(envelopes) - start, "items": items})

    try:
        import sentry_sdk
        from sentry_sdk.transport import Transport

        class MemoryTransport(Transport):
            def capture_envelope(self, envelope):
                # Force actual wire serialization, then retain both structured payload and bytes.
                raw = envelope.serialize()
                index = len(envelopes)
                (RUN_DIR / f"envelope-{index:03d}.bin").write_bytes(raw)
                parsed = []
                for item in envelope.items:
                    try:
                        data = json.loads(item.get_bytes())
                        parsed.append({"headers": item.headers, "json": data})
                    except (ValueError, UnicodeDecodeError):
                        parsed.append(
                            {"headers": item.headers, "non_json_length": len(item.get_bytes())}
                        )
                envelopes.append(
                    {
                        "case": case,
                        "headers": envelope.headers,
                        "wire_sha256": hashlib.sha256(raw).hexdigest(),
                        "items": parsed,
                    }
                )

        try:
            from app.core import sentry_transport as policy_transport
        except ModuleNotFoundError as error:
            if error.name not in ("app.core.sentry_transport", "app.core"):
                raise
            policy_transport = None
        else:
            # Keep the real application envelope boundary; replace only its outbound delegate.
            policy_transport.make_transport = lambda options: MemoryTransport(options)
        report["checks"]["production_transport_boundary_present"] = policy_transport is not None

        actual_init = sentry_sdk.init

        def safe_init(*args, **kwargs):
            if args:
                raise AssertionError("Application init unexpectedly uses positional arguments")
            if kwargs.get("dsn") != "https://synthetic-public-key@sentry.invalid/1":
                raise AssertionError("Unexpected DSN")
            init_calls.append(
                {
                    "keys": sorted(kwargs),
                    "send_default_pii": kwargs.get("send_default_pii"),
                    "integrations": [type(i).__name__ for i in kwargs["integrations"]],
                }
            )
            if policy_transport is None:
                kwargs["transport"] = MemoryTransport
            else:
                assert kwargs.get("transport") is policy_transport.ConfidentialTransport
            return actual_init(**kwargs)

        sentry_sdk.init = safe_init

        import app.main as main
        from app.core.config import settings

        # Envelope policy test invokes the production post-admission initializer.
        # Separate startup regressions prove it is never called before admission.
        assert not init_calls
        main._init_sentry()

        assert settings.APP_MODE == "mock" and settings.LIVE_TRADING_ENABLED is False
        assert os.environ["CASHGUARD_ENV_FILE"] == ""
        report["runtime"] = {
            "python": sys.version,
            "sentry_version": importlib.metadata.version("sentry-sdk"),
            "sentry_path": sentry_sdk.__file__,
            "pydantic": importlib.metadata.version("pydantic"),
            "pydantic_core": importlib.metadata.version("pydantic-core"),
            "source_main_sha256": hashlib.sha256(Path(main.__file__).read_bytes()).hexdigest(),
            "environment_keys": sorted(os.environ),
            "init_calls": init_calls,
        }

        if not settings.SENTRY_DSN:
            assert not init_calls
            assert not sentry_sdk.is_initialized()
            sentry_sdk.capture_message("SYNTHETIC disabled no-op")
            sentry_sdk.flush()
            assert not envelopes
            report["checks"]["dsn_empty_no_init_no_capture"] = True
        else:
            client = sentry_sdk.get_client()
            report["runtime"]["sdk_options"] = {
                k: client.options.get(k)
                for k in [
                    "send_default_pii",
                    "data_collection",
                    "include_local_variables",
                    "max_request_body_size",
                    "traces_sample_rate",
                    "profiles_sample_rate",
                    "trace_lifecycle",
                    "enable_logs",
                    "enable_metrics",
                ]
            }
            report["runtime"]["sdk_options"]["event_scrubber_recursive"] = getattr(
                client.options.get("event_scrubber"), "recursive", None
            )
            report["runtime"]["sdk_options"]["before_send_transaction"] = (
                client.options.get("before_send_transaction") is not None
            )
            report["runtime"]["sdk_options"]["before_send_callback_filename"] = client.options[
                "before_send"
            ].__code__.co_filename
            report["runtime"]["integrations"] = sorted(client.integrations)
            assert client.options["send_default_pii"] is False
            assert client.should_send_default_pii() is False
            assert policy_transport is not None
            assert isinstance(client.transport, policy_transport.ConfidentialTransport)
            assert isinstance(client.transport._delegate, MemoryTransport)
            assert client.options["include_local_variables"] is False
            assert client.options["max_request_body_size"] == "never"
            assert client.options["profiles_sample_rate"] == 0
            assert client.options["profile_session_sample_rate"] == 0
            assert client.options["auto_session_tracking"] is False
            assert client.options["send_client_reports"] is False
            assert client.options["enable_logs"] is False
            assert client.options["spotlight"] is False
            assert client.options["before_send_transaction"] is not None
            assert len(init_calls) == 1
            report["checks"]["actual_callback_and_no_pii_option"] = True

        import asyncio
        from typing import Any

        import httpx
        from fastapi import Body
        from sqlalchemy import create_engine, text

        @main.app.post("/__synthetic_probe__/body")
        async def failing_body(payload: Any = Body(...)):
            raise RuntimeError("SYNTHETIC failure " + str(payload))

        @main.app.get("/__synthetic_probe__/user-breadcrumb")
        async def failing_user():
            sentry_sdk.set_user(
                {
                    "email": sentinel("USER_EMAIL") + "@example.invalid",
                    "username": sentinel("USER_NAME"),
                    "ip_address": "192.0.2.42",
                }
            )
            sentry_sdk.add_breadcrumb(
                category="synthetic",
                message="synthetic breadcrumb",
                data={
                    "password": sentinel("BREAD_PASSWORD"),
                    "custom_vault": sentinel("BREAD_CUSTOM"),
                    "nested": {"password": sentinel("BREAD_NESTED_PASSWORD")},
                },
            )
            raise RuntimeError("SYNTHETIC user breadcrumb failure")

        @main.app.get("/__synthetic_probe__/sql")
        async def successful_sql():
            # Uses in-process SQLite solely to invoke the real SQLAlchemy integration.
            engine = create_engine("sqlite:///:memory:")
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT :bound_value"), {"bound_value": sentinel("SQL_BOUND")}
                ).scalar() == sentinel("SQL_BOUND")
                assert connection.execute(
                    text("SELECT '" + sentinel("SQL_LITERAL") + "'")
                ).scalar() == sentinel("SQL_LITERAL")
            engine.dispose()
            return {"ok": True}

        @main.app.get("/__synthetic_probe__/logging")
        async def logging_failure():
            import logging

            logging.getLogger("privacy-probe").error(
                "password=%s", sentinel("LOG_PASSWORD"), extra={"unknown": sentinel("LOG_EXTRA")}
            )
            return {"ok": True}

        async def requests():
            nonlocal case
            from tests.admitted_runtime import admitted_runtime

            async with (
                admitted_runtime() as admitted,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=admitted, raise_app_exceptions=False),
                    base_url="http://synthetic.invalid",
                ) as http,
            ):
                for name, payload in [
                    (
                        "asgi-object",
                        {
                            "password": sentinel("OBJECT_PASSWORD"),
                            "email": sentinel("OBJECT_EMAIL") + "@example.invalid",
                            "custom_vault": sentinel("OBJECT_CUSTOM"),
                            "nested": {
                                "password": sentinel("NESTED_PASSWORD"),
                                "secret": sentinel("NESTED_SECRET"),
                            },
                            "items": [{"token": sentinel("ITEM_TOKEN")}],
                        },
                    ),
                    (
                        "asgi-array",
                        [
                            {
                                "password": sentinel("ARRAY_PASSWORD"),
                                "custom_vault": sentinel("ARRAY_CUSTOM"),
                                "nested": {"secret": sentinel("ARRAY_NESTED_SECRET")},
                            }
                        ],
                    ),
                ]:
                    case = name
                    start = len(envelopes)
                    response = await http.post(
                        "/__synthetic_probe__/body?password="
                        + sentinel("QUERY_PASSWORD")
                        + "&custom_vault="
                        + sentinel("QUERY_CUSTOM"),
                        json=payload,
                        headers=[
                            ("AuThOrIzAtIoN", sentinel("AUTH_HEADER")),
                            ("cOoKiE", "session=" + sentinel("COOKIE_HEADER")),
                            ("X-API-Key", sentinel("API_HEADER")),
                            ("x-custom-vault", sentinel("CUSTOM_HEADER")),
                            ("User-Agent", sentinel("USER_AGENT")),
                        ],
                    )
                    assert response.status_code == 500
                    sentry_sdk.flush()
                    summarize(
                        name,
                        start,
                        [
                            "OBJECT_PASSWORD",
                            "OBJECT_EMAIL",
                            "OBJECT_CUSTOM",
                            "NESTED_PASSWORD",
                            "NESTED_SECRET",
                            "ITEM_TOKEN",
                            "ARRAY_PASSWORD",
                            "ARRAY_CUSTOM",
                            "ARRAY_NESTED_SECRET",
                            "QUERY_PASSWORD",
                            "QUERY_CUSTOM",
                            "AUTH_HEADER",
                            "COOKIE_HEADER",
                            "API_HEADER",
                            "CUSTOM_HEADER",
                            "USER_AGENT",
                        ],
                    )
                for name, route, labels, status in [
                    (
                        "asgi-user-breadcrumb",
                        "user-breadcrumb",
                        [
                            "USER_EMAIL",
                            "USER_NAME",
                            "BREAD_PASSWORD",
                            "BREAD_CUSTOM",
                            "BREAD_NESTED_PASSWORD",
                        ],
                        500,
                    ),
                    ("asgi-sql", "sql", ["SQL_BOUND", "SQL_LITERAL"], 200),
                    ("asgi-logging", "logging", ["LOG_PASSWORD", "LOG_EXTRA"], 200),
                ]:
                    case = name
                    start = len(envelopes)
                    response = await http.get("/__synthetic_probe__/" + route)
                    assert response.status_code == status
                    sentry_sdk.flush()
                    summarize(name, start, labels)

        asyncio.run(requests())

        if settings.SENTRY_DSN:
            case = "celery-local-apply"
            from app.workers.celery_app import celery_app

            @celery_app.task(name="synthetic.cg325.privacy_probe", max_retries=1)
            def fail_task(payload):
                # max_retries=1 means the app's real DLQ handler returns before Redis.
                sentry_sdk.add_breadcrumb(
                    category="synthetic-celery",
                    data={
                        "password": sentinel("CELERY_BREAD_PASSWORD"),
                        "nested": {"password": sentinel("CELERY_BREAD_NESTED")},
                    },
                )
                raise RuntimeError("SYNTHETIC Celery failure")

            start = len(envelopes)
            result = fail_task.apply(
                args=[
                    {
                        "password": sentinel("CELERY_PASSWORD"),
                        "custom_vault": sentinel("CELERY_CUSTOM"),
                    }
                ],
                throw=False,
            )
            assert result.failed()
            sentry_sdk.flush()
            summarize(
                case,
                start,
                [
                    "CELERY_PASSWORD",
                    "CELERY_CUSTOM",
                    "CELERY_BREAD_PASSWORD",
                    "CELERY_BREAD_NESTED",
                ],
            )
            report["checks"]["celery_failed_locally_without_broker"] = True

        case = "shutdown"
        sentry_sdk.flush()
        if settings.SENTRY_DSN:
            sentry_sdk.get_client().close()
        assert not attempts, "A socket/network operation was attempted"
        report["checks"]["network_tripwire_zero_attempts"] = True
        report["status"] = "PASS_HARNESS_EXECUTION"
    except BaseException as exc:
        report["status"] = "FAIL_HARNESS_EXECUTION"
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        raise
    finally:
        finish()


if __name__ == "__main__":
    run()
