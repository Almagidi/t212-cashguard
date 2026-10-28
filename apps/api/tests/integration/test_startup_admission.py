"""Real ASGI admission, including servers that never execute lifespan."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.services.recovery_controls import RecoveryRefusal
from app.services.startup_admission import ADMISSION_KEY

PRODUCTION_INIT_SENTRY = main._init_sentry


async def test_actual_sdk_global_ownership_preserves_external_isolation_client(
    setup_boundary, monkeypatch
) -> None:
    from unittest.mock import Mock

    import sentry_sdk
    from sentry_sdk.transport import Transport

    from app.core import sentry_transport

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope):
            pass

    monkeypatch.setattr(sentry_transport, "make_transport", MemoryTransport)
    monkeypatch.setattr(main.settings, "SENTRY_DSN", "https://synthetic@sentry.invalid/1")
    monkeypatch.setattr(main, "_init_sentry", PRODUCTION_INIT_SENTRY)
    previous = sentry_sdk.get_global_scope().client
    external = sentry_sdk.Client(
        dsn="https://synthetic@sentry.invalid/2",
        transport=MemoryTransport,
        default_integrations=False,
        auto_session_tracking=False,
    )
    external_close = Mock(wraps=external.close)
    monkeypatch.setattr(external, "close", external_close)
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(external)
            async with main.lifespan(main.app):
                owned = sentry_sdk.get_global_scope().client
                assert owned is not None and owned is not previous and owned is not external
                assert sentry_sdk.get_client() is external
                owned_close = Mock(wraps=owned.close)
                monkeypatch.setattr(owned, "close", owned_close)
            owned_close.assert_called_once()
            external_close.assert_not_called()
            assert sentry_sdk.get_client() is external
            assert sentry_sdk.get_global_scope().client is previous
    finally:
        external.close()


@asynccontextmanager
async def real_lifespan():
    """Send real ASGI lifecycle events, retaining the state the server copies."""
    incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    state: dict[str, Any] = {}
    task = asyncio.create_task(
        main.app(
            {"type": "lifespan", "state": state, "asgi": {"version": "3.0"}},
            incoming.get,
            outgoing.put,
        )
    )
    await incoming.put({"type": "lifespan.startup"})
    startup = await outgoing.get()
    if startup["type"] != "lifespan.startup.complete":
        await task
        raise RuntimeError("Lifespan refused startup")

    async def request(scope, receive, send):
        await main.app({**scope, "state": dict(state)}, receive, send)

    try:
        yield request, state
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        shutdown = await outgoing.get()
        assert shutdown["type"] == "lifespan.shutdown.complete"
        await task


@pytest.fixture
def setup_boundary(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    checks = AsyncMock()
    start, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "assert_saved_permissions_unarmed", checks)
    monkeypatch.setattr(main, "start_global_demo_reconciliation_scheduler", start)
    monkeypatch.setattr(main, "stop_global_demo_reconciliation_scheduler", stop)
    monkeypatch.setattr(main, "_init_sentry", lambda: None)
    return {"checks": checks, "start": start, "stop": stop}


@pytest.mark.parametrize("path", ["/v1/health/ready", "/v1/health/deps", "/docs", "/"])
async def test_lifespan_off_is_closed_before_dependencies(path: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["startup_state"] == "UNVALIDATED"


async def test_exact_liveness_remains_available_without_lifespan() -> None:
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/v1/health/live")).status_code == 200
        assert (await client.get("/v1/health/live/extra")).status_code == 503


async def test_websocket_without_lifespan_never_reaches_handler() -> None:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await main.app(
        {"type": "websocket", "path": "/v1/ws/live", "headers": [], "query_string": b""},
        receive,
        send,
    )
    assert messages == [
        {"type": "websocket.close", "code": 1013, "reason": "Startup review required"}
    ]


async def test_real_lifespan_publishes_then_revokes_and_lifespan_off_stays_closed(
    setup_boundary,
) -> None:
    async with real_lifespan() as (admitted, state):
        async with AsyncClient(
            transport=ASGITransport(app=admitted), base_url="http://test"
        ) as client:
            assert (await client.get("/docs")).status_code == 200
            assert state[ADMISSION_KEY].state == "READY_UNARMED"
            # A later explicit rearm is not a new boot check/readiness reset.
            setup_boundary["checks"].side_effect = RecoveryRefusal("Saved permissions armed.")
            assert (await client.get("/v1/health/ready")).status_code == 200
        async with AsyncClient(
            transport=ASGITransport(app=main.app), base_url="http://test"
        ) as off:
            assert (await off.get("/docs")).status_code == 503
    async with AsyncClient(transport=ASGITransport(app=admitted), base_url="http://test") as stale:
        assert (await stale.get("/docs")).status_code == 503
    setup_boundary["checks"].assert_awaited_once()
    setup_boundary["stop"].assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [RecoveryRefusal("Review saved permissions."), OSError("PRIVATE_DSN")]
)
async def test_blocked_lifespan_never_calls_setup_or_dependencies(
    setup_boundary, monkeypatch, failure
) -> None:
    setup_boundary["checks"].side_effect = failure

    def forbidden():
        raise AssertionError("Pre-admission exporter/configuration initialization")

    monkeypatch.setattr(main, "_init_sentry", forbidden)
    monkeypatch.setattr(main, "assert_startup_safe", forbidden)
    async with real_lifespan() as (blocked, state):
        assert state[ADMISSION_KEY].state == "STARTUP_BLOCKED"
        async with AsyncClient(
            transport=ASGITransport(app=blocked), base_url="http://test"
        ) as client:
            for path in [
                "/v1/health/ready",
                "/v1/health/deps",
                "/v1/settings",
                "/metrics",
                "/docs",
            ]:
                response = await client.get(path)
                assert response.status_code == 503
                assert "PRIVATE_DSN" not in response.text
            assert (await client.get("/v1/health/live")).status_code == 200
    setup_boundary["start"].assert_not_awaited()
    setup_boundary["stop"].assert_not_awaited()


async def test_admission_is_not_published_until_setup_completes(setup_boundary) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def setup():
        entered.set()
        await release.wait()

    setup_boundary["start"].side_effect = setup
    context = main.lifespan(main.app)
    entering = asyncio.create_task(context.__aenter__())
    await entered.wait()
    lease = main.admission._owner
    assert lease is not None and lease.state == "UNVALIDATED"
    assert not lease.revoked
    release.set()
    state = await entering
    assert state[ADMISSION_KEY].state == "READY_UNARMED"
    await context.__aexit__(None, None, None)
    assert lease.revoked


async def test_interrupted_setup_revokes_before_cleanup_and_next_lifespan_recovers(
    setup_boundary,
) -> None:
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    setup_boundary["start"].side_effect = wait
    context = main.lifespan(main.app)
    entering = asyncio.create_task(context.__aenter__())
    await entered.wait()
    lease = main.admission._owner
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert lease.revoked
    setup_boundary["start"].side_effect = None
    async with real_lifespan() as (_, state):
        assert state[ADMISSION_KEY].state == "READY_UNARMED"


async def test_second_concurrent_lifespan_does_not_revoke_first(setup_boundary) -> None:
    async with main.lifespan(main.app) as first:
        with pytest.raises(RuntimeError, match="already owns"):
            async with main.lifespan(main.app):
                pytest.fail("Concurrent lifecycle admitted")
        assert main.admission.owns(first[ADMISSION_KEY])
        assert first[ADMISSION_KEY].state == "READY_UNARMED"


async def test_configuration_failure_retains_strict_refusal_and_revokes(
    setup_boundary, monkeypatch
) -> None:
    def fail():
        raise RuntimeError("Unsafe paper startup configuration")

    monkeypatch.setattr(main, "assert_startup_safe", fail)
    with pytest.raises(RuntimeError, match="Unsafe paper"):
        async with main.lifespan(main.app):
            pytest.fail("Unsafe startup admitted")
    assert main.admission._owner is None
    setup_boundary["start"].assert_not_awaited()


async def test_fastapi_native_environment_export_is_explicitly_disabled(
    setup_boundary, monkeypatch
) -> None:
    from fastapi.telemetry import _runtime

    seen = []
    real_configure = _runtime._configure_from_environment

    def observe(config):
        seen.append(config["auto_configure"])
        real_configure(config)

    monkeypatch.setattr(_runtime, "_configure_from_environment", observe)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://synthetic.invalid/no-egress")
    async with real_lifespan():
        pass
    assert seen and not any(seen)


async def test_lease_from_another_event_loop_is_not_admitted(setup_boundary) -> None:
    async with main.lifespan(main.app) as state:
        lease = state[ADMISSION_KEY]

        async def elsewhere():
            return main.admission.owns(lease)

        assert await asyncio.to_thread(lambda: asyncio.run(elsewhere())) is False
        assert main.admission.owns(lease)


async def test_owned_exporter_closed_and_previous_client_restored_on_cleanup_failure(
    setup_boundary, monkeypatch
) -> None:
    from unittest.mock import Mock

    previous, owned = Mock(), Mock()
    scope = Mock(client=previous)
    scope.set_client.side_effect = lambda value: setattr(scope, "client", value)
    sdk = Mock()
    sdk.get_global_scope.return_value = scope
    monkeypatch.setattr(main, "sentry_sdk", sdk)

    def initialize():
        scope.client = owned
        return owned

    monkeypatch.setattr(main, "_init_sentry", initialize)
    setup_boundary["stop"].side_effect = RuntimeError("Synthetic scheduler cleanup failure")
    with pytest.raises(RuntimeError, match="cleanup failure"):
        async with main.lifespan(main.app) as state:
            lease = state[ADMISSION_KEY]
            assert scope.client is owned
    assert lease.revoked
    assert scope.client is previous
    owned.close.assert_called_once()
    previous.close.assert_not_called()
