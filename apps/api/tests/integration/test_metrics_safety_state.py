"""PostgreSQL proof that ``/metrics`` reports the persisted safety state, and reports
"unknown" rather than a safe-looking zero when it cannot read that state.

Every scrape goes through the real application and its real routes; the database is a
PostgreSQL schema of the test's own. Nothing here contacts a broker.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import delete, make_url, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.api.metrics as metrics_module
from app.api.metrics_state import (
    STATE_READ_TIMEOUT_SECONDS,
    read_safety_state,
    scrape_safety_state,
)
from app.core.security import create_access_token, hash_password
from app.db.models import AppSettings, User, VenueConfig
from app.db.session import Base, get_db
from app.main import app
from app.risk.engine import activate_kill_switch
from tests.admitted_runtime import admitted_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

original_app = app

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
ADMIN_EMAIL = "metrics-admin@example.test"
VENUES = ("t212", "kraken")
# The route's own read budget, plus scheduling slack for a loaded CI runner.
SCRAPE_DEADLINE_SECONDS = STATE_READ_TIMEOUT_SECONDS + 3.0

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_TEST_DATABASE_URL,
        reason="POSTGRES_TEST_DATABASE_URL is required for the metrics state proof",
    ),
    pytest.mark.asyncio,
]

Samples = dict[tuple[str, tuple[tuple[str, str], ...]], float]


@pytest_asyncio.fixture(autouse=True)
async def _model_admitted_metrics_runtime() -> AsyncGenerator[None, None]:
    """Scrape tests exercise post-startup DB faults, not application admission.

    Keep the real routes/assertions; scope the explicit runtime model ONLY to
    this module. Startup/lifespan tests use the unwrapped app independently.
    """
    global app
    original = app
    async with admitted_runtime() as admitted:
        app = admitted
        try:
            yield
        finally:
            app = original


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[dict[str, Any], None]:
    """A migrated-shape schema with settings, both venues and one admin."""
    assert POSTGRES_TEST_DATABASE_URL is not None
    schema = f"metrics_{uuid.uuid4().hex}"
    engine = create_async_engine(POSTGRES_TEST_DATABASE_URL, poolclass=NullPool)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with scoped.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # Migration 0001 created these columns nullable; create_all does not.
            for column in ("kill_switch_active", "auto_trading_enabled"):
                await connection.execute(
                    text(f'ALTER TABLE "{schema}".app_settings ALTER COLUMN {column} DROP NOT NULL')
                )
        factory = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
        admin_id = uuid.uuid4()
        async with factory() as setup:
            setup.add(AppSettings(id=1, auto_trading_enabled=False, kill_switch_active=False))
            for venue in VENUES:
                setup.add(VenueConfig(venue=venue))
            setup.add(
                User(
                    id=admin_id,
                    email=ADMIN_EMAIL,
                    hashed_password=hash_password("metrics-admin-password"),
                    is_active=True,
                    is_admin=True,
                )
            )
            await setup.commit()
        yield {"engine": engine, "schema": schema, "factory": factory, "admin_id": admin_id}
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


def _serve(state_factory: Any, request_factory: Any) -> None:
    """Point the application at the test schema, as a deployment points it at its database."""

    async def request_db() -> AsyncGenerator[Any, None]:
        async with request_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    original_app.dependency_overrides[get_db] = request_db
    provider = getattr(metrics_module, "get_metrics_session_factory", None)
    if provider is not None:
        original_app.dependency_overrides[provider] = lambda: state_factory


@pytest_asyncio.fixture
async def client(pg: dict[str, Any]) -> AsyncGenerator[AsyncClient, None]:
    _serve(pg["factory"], pg["factory"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    original_app.dependency_overrides.clear()


def _admin_headers(pg: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(pg['admin_id'])}"}


async def _scrape(client: AsyncClient) -> tuple[Samples, str]:
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    samples: Samples = {}
    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            if sample.name.startswith("cashguard_"):
                samples[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return samples, response.text


def _value(samples: Samples, name: str, **labels: str) -> float | None:
    return samples.get((name, tuple(sorted(labels.items()))))


def _known(samples: Samples, control: str, scope: str) -> float | None:
    return _value(samples, "cashguard_safety_state_known", control=control, scope=scope)


async def _set_settings(pg: dict[str, Any], **values: Any) -> None:
    async with pg["factory"]() as session:
        await session.execute(update(AppSettings).where(AppSettings.id == 1).values(**values))
        await session.commit()


def _assert_unknown_everywhere(samples: Samples) -> None:
    """A failed read leaves no safety value behind and marks every control unknown."""
    for name in (
        "cashguard_kill_switch_active",
        "cashguard_auto_trading_enabled",
        "cashguard_venue_kill_switch_active",
        "cashguard_venue_auto_trading_enabled",
        "cashguard_venue_degraded_mode_active",
        "cashguard_kill_switch_last_activation_timestamp_seconds",
    ):
        assert not [key for key in samples if key[0] == name], f"{name} reported while unknown"
    assert _value(samples, "cashguard_safety_state_read_ok") == 0
    for scope in ("global", *VENUES):
        assert _known(samples, "kill_switch", scope) == 0
        assert _known(samples, "auto_trading", scope) == 0
        if scope != "global":
            assert _known(samples, "degraded_mode", scope) == 0


# ── Persisted state is what the scrape reports ────────────────────────────────


async def test_kill_switch_saved_before_startup_is_reported_and_agrees_with_operator_status(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    await _set_settings(pg, kill_switch_active=True)

    samples, _ = await _scrape(client)
    status = await client.get("/v1/operator/status", headers=_admin_headers(pg))

    assert status.status_code == 200, status.text
    assert status.json()["protective_stops"]["global_kill_switch_active"] is True
    assert status.json()["overall_status"] == "blocked"
    assert _value(samples, "cashguard_kill_switch_active") == 1
    assert _known(samples, "kill_switch", "global") == 1
    assert _value(samples, "cashguard_safety_state_read_ok") == 1


async def test_kill_switch_routes_are_reported_by_the_next_scrape(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    before, _ = await _scrape(client)
    assert _value(before, "cashguard_kill_switch_active") == 0
    assert not [k for k in before if k[0].startswith("cashguard_kill_switch_last_activation")]

    started = datetime.now(UTC).timestamp()
    enabled = await client.post("/v1/risk/kill-switch/enable", headers=_admin_headers(pg))
    assert enabled.status_code == 200, enabled.text
    during, body = await _scrape(client)

    assert _value(during, "cashguard_kill_switch_active") == 1
    assert _value(during, "cashguard_auto_trading_enabled") == 0
    stamp = _value(during, "cashguard_kill_switch_last_activation_timestamp_seconds")
    assert stamp is not None and started - 1 <= stamp <= time.time() + 1
    assert ADMIN_EMAIL not in body, "an operator's address must never become a label"

    disabled = await client.post("/v1/risk/kill-switch/disable", headers=_admin_headers(pg))
    assert disabled.status_code == 200, disabled.text
    after, _ = await _scrape(client)
    assert _value(after, "cashguard_kill_switch_active") == 0
    assert _value(after, "cashguard_kill_switch_last_activation_timestamp_seconds") == stamp


async def test_activation_by_another_process_is_reported_only_once_committed(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    """A worker halts trading through its own session; the API scrape sees exactly what is saved."""
    async with pg["factory"]() as worker:
        await worker.execute(
            update(AppSettings).where(AppSettings.id == 1).values(kill_switch_active=True)
        )
        await worker.flush()
        uncommitted, _ = await _scrape(client)
        await worker.commit()
    committed, _ = await _scrape(client)

    # Before the commit no session (the risk engine's included) can see the halt either.
    assert _value(uncommitted, "cashguard_kill_switch_active") == 0
    assert _value(committed, "cashguard_kill_switch_active") == 1

    await _set_settings(pg, kill_switch_active=False)
    async with pg["factory"]() as worker:
        await activate_kill_switch(worker, actor="position_monitor:daily_loss")
    halted, body = await _scrape(client)
    assert _value(halted, "cashguard_kill_switch_active") == 1
    assert "position_monitor" not in body


async def test_venue_halt_is_reported_for_its_venue_and_not_as_the_global_switch(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    async with pg["factory"]() as session:
        await session.execute(
            update(VenueConfig)
            .where(VenueConfig.venue == "t212")
            .values(kill_switch_active=True, degraded_mode_active=True)
        )
        await session.execute(
            update(VenueConfig)
            .where(VenueConfig.venue == "kraken")
            .values(auto_trading_enabled=True)
        )
        await session.commit()

    samples, _ = await _scrape(client)

    assert _value(samples, "cashguard_kill_switch_active") == 0
    assert _value(samples, "cashguard_venue_kill_switch_active", venue="t212") == 1
    assert _value(samples, "cashguard_venue_kill_switch_active", venue="kraken") == 0
    assert _value(samples, "cashguard_venue_degraded_mode_active", venue="t212") == 1
    assert _value(samples, "cashguard_venue_auto_trading_enabled", venue="kraken") == 1
    assert _value(samples, "cashguard_venue_auto_trading_enabled", venue="t212") == 0


async def test_paused_auto_trading_is_reported_as_saved(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    await _set_settings(pg, auto_trading_enabled=True)
    on, _ = await _scrape(client)
    await _set_settings(pg, auto_trading_enabled=False)
    off, _ = await _scrape(client)

    assert _value(on, "cashguard_auto_trading_enabled") == 1
    assert _value(off, "cashguard_auto_trading_enabled") == 0
    assert _known(off, "auto_trading", "global") == 1


# ── Missing or unreadable state is unknown, never zero ────────────────────────


async def test_missing_settings_row_is_unknown_not_inactive(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    async with pg["factory"]() as session:
        await session.execute(delete(AppSettings))
        await session.commit()

    samples, _ = await _scrape(client)

    assert _value(samples, "cashguard_safety_state_read_ok") == 1
    assert _value(samples, "cashguard_kill_switch_active") is None
    assert _value(samples, "cashguard_auto_trading_enabled") is None
    assert _known(samples, "kill_switch", "global") == 0
    assert _known(samples, "auto_trading", "global") == 0
    assert _known(samples, "kill_switch", "t212") == 1


async def test_null_saved_value_is_unknown_not_inactive(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    await _set_settings(pg, kill_switch_active=None, auto_trading_enabled=None)

    samples, _ = await _scrape(client)

    assert _value(samples, "cashguard_kill_switch_active") is None
    assert _value(samples, "cashguard_auto_trading_enabled") is None
    assert _known(samples, "kill_switch", "global") == 0
    assert _known(samples, "auto_trading", "global") == 0


async def test_missing_venue_row_is_unknown_for_that_venue(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    async with pg["factory"]() as session:
        await session.execute(delete(VenueConfig).where(VenueConfig.venue == "kraken"))
        await session.commit()

    samples, _ = await _scrape(client)

    assert _value(samples, "cashguard_venue_kill_switch_active", venue="kraken") is None
    assert _known(samples, "kill_switch", "kraken") == 0
    assert _known(samples, "auto_trading", "kraken") == 0
    assert _known(samples, "kill_switch", "t212") == 1


async def test_unreachable_database_is_reported_as_unknown_within_the_budget(
    pg: dict[str, Any],
) -> None:
    """A database that accepts the connection and never answers must not stall the scrape."""
    silent: list[asyncio.StreamWriter] = []

    async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        silent.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    dead = create_async_engine(
        f"postgresql+asyncpg://nobody:nothing@127.0.0.1:{port}/none", poolclass=NullPool
    )
    try:
        _serve(async_sessionmaker(dead, expire_on_commit=False), pg["factory"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            started = time.monotonic()
            both = await asyncio.gather(_scrape(c), _scrape(c))  # every read slot
            elapsed = time.monotonic() - started
        recovered = await _both_slots_free_within(pg, SCRAPE_DEADLINE_SECONDS)
    finally:
        original_app.dependency_overrides.clear()
        for writer in silent:
            writer.close()
        server.close()
        await server.wait_closed()
        await dead.dispose()

    assert elapsed < SCRAPE_DEADLINE_SECONDS, f"scrape took {elapsed:.1f}s"
    for samples, _ in both:
        _assert_unknown_everywhere(samples)
    assert _all_read(recovered), "a slot was never freed"


@dataclass
class Partition:
    """Sessions whose connection goes quiet once established, and how many reads it cut."""

    factory: Any
    cuts: list[int] = field(default_factory=list)

    async def wait_for_cuts(self, count: int) -> None:
        deadline = time.monotonic() + SCRAPE_DEADLINE_SECONDS
        while len(self.cuts) < count:
            assert time.monotonic() < deadline, f"only {len(self.cuts)} reads reached the relay"
            await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def partitioned(pg: dict[str, Any]) -> AsyncGenerator[Partition, None]:
    """A network partition: a relay to the test database passes the handshake, then
    forwards nothing, either way, from the moment the state query is sent."""
    assert POSTGRES_TEST_DATABASE_URL is not None
    target = make_url(POSTGRES_TEST_DATABASE_URL)
    held: list[asyncio.StreamWriter] = []
    cuts: list[int] = []

    async def relay(
        source: asyncio.StreamReader, sink: asyncio.StreamWriter, cut: asyncio.Event
    ) -> None:
        while not cut.is_set() and (chunk := await source.read(65536)):
            if b"app_settings" in chunk:
                cut.set()
                cuts.append(1)
                return
            sink.write(chunk)
            await sink.drain()

    async def partition(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_reader, server_writer = await asyncio.open_connection(target.host, target.port)
        held.extend([writer, server_writer])
        cut = asyncio.Event()
        await asyncio.gather(relay(reader, server_writer, cut), relay(server_reader, writer, cut))

    proxy = await asyncio.start_server(partition, "127.0.0.1", 0)
    port = proxy.sockets[0].getsockname()[1]
    quiet = create_async_engine(target.set(host="127.0.0.1", port=port), poolclass=NullPool)
    try:
        yield Partition(
            async_sessionmaker(
                quiet.execution_options(schema_translate_map={None: pg["schema"]}),
                expire_on_commit=False,
            ),
            cuts,
        )
    finally:
        original_app.dependency_overrides.clear()
        for writer in held:
            writer.close()
        proxy.close()
        await proxy.wait_closed()
        await quiet.dispose()


async def _both_slots_free_within(pg: dict[str, Any], seconds: float) -> list[Samples]:
    """Scrape the healthy database twice at once until both read it, or time runs out."""
    _serve(pg["factory"], pg["factory"])
    results: list[Samples] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            results = [samples for samples, _ in await asyncio.gather(_scrape(c), _scrape(c))]
            if all(_value(s, "cashguard_safety_state_read_ok") == 1 for s in results):
                break
            await asyncio.sleep(0.1)
    return results


def _all_read(results: list[Samples]) -> bool:
    return bool(results) and all(_value(s, "cashguard_safety_state_read_ok") == 1 for s in results)


async def test_database_falling_silent_mid_read_is_unknown_and_frees_its_slot(
    pg: dict[str, Any], partitioned: Partition
) -> None:
    _serve(partitioned.factory, pg["factory"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        started = time.monotonic()
        samples, _ = await _scrape(c)
        elapsed = time.monotonic() - started
    recovered = await _both_slots_free_within(pg, SCRAPE_DEADLINE_SECONDS)

    assert partitioned.cuts, "the read never reached the partition"
    assert elapsed < SCRAPE_DEADLINE_SECONDS, f"scrape took {elapsed:.1f}s"
    _assert_unknown_everywhere(samples)
    assert _all_read(recovered), "the stuck read kept its slot"
    assert _value(recovered[0], "cashguard_kill_switch_active") == 0


async def test_stuck_reads_cannot_pile_up_database_connections(
    pg: dict[str, Any], partitioned: Partition
) -> None:
    """While every read slot is held by a stuck read, a scrape reports unknown at once."""
    _serve(partitioned.factory, pg["factory"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as stuck:
        held = [asyncio.ensure_future(_scrape(stuck)) for _ in range(2)]
        await partitioned.wait_for_cuts(2)
        _serve(pg["factory"], pg["factory"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            started = time.monotonic()
            crowded, _ = await _scrape(c)
            elapsed = time.monotonic() - started
        await asyncio.gather(*held)
    recovered = await _both_slots_free_within(pg, SCRAPE_DEADLINE_SECONDS)

    assert elapsed < 1.0, f"a crowded scrape waited {elapsed:.1f}s"
    _assert_unknown_everywhere(crowded)
    assert _all_read(recovered), "a stuck read kept its slot"


async def test_abandoned_scrape_stops_its_read(pg: dict[str, Any], partitioned: Partition) -> None:
    """A scraper that disconnects mid-read must not leave the read holding its slot."""
    scrapes = [asyncio.ensure_future(scrape_safety_state(partitioned.factory)) for _ in range(2)]
    await partitioned.wait_for_cuts(2)
    for scrape in scrapes:
        scrape.cancel()
    await asyncio.gather(*scrapes, return_exceptions=True)
    # Well inside the read budget: only stopping the reads themselves frees the slots now.
    recovered = await _both_slots_free_within(pg, STATE_READ_TIMEOUT_SECONDS / 2)

    assert all(scrape.cancelled() for scrape in scrapes)
    assert _all_read(recovered), "an abandoned read kept its slot"


async def test_the_state_read_cannot_write(pg: dict[str, Any]) -> None:
    async with pg["factory"]() as session:
        await read_safety_state(session)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await session.execute(
                update(AppSettings).where(AppSettings.id == 1).values(kill_switch_active=True)
            )
        await session.rollback()


async def test_locked_settings_table_is_unknown_and_recovers_once_released(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    await _set_settings(pg, kill_switch_active=True)
    async with pg["engine"].connect() as migration:
        transaction = await migration.begin()
        await migration.execute(
            text(f'LOCK TABLE "{pg["schema"]}".app_settings IN ACCESS EXCLUSIVE MODE')
        )
        started = time.monotonic()
        locked, _ = await _scrape(client)
        elapsed = time.monotonic() - started
        await transaction.rollback()
    released, _ = await _scrape(client)

    assert elapsed < SCRAPE_DEADLINE_SECONDS, f"scrape took {elapsed:.1f}s"
    _assert_unknown_everywhere(locked)
    assert _value(released, "cashguard_safety_state_read_ok") == 1
    assert _value(released, "cashguard_kill_switch_active") == 1


async def test_concurrent_scrapes_never_report_a_committed_halt_as_inactive(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    for round_number in range(6):
        halted = round_number % 2 == 0
        await _set_settings(pg, kill_switch_active=halted)
        results = await asyncio.gather(*(_scrape(client) for _ in range(8)))
        for samples, _ in results:
            if _value(samples, "cashguard_safety_state_read_ok") == 1:
                assert _value(samples, "cashguard_kill_switch_active") == (1 if halted else 0)
            else:
                _assert_unknown_everywhere(samples)
        reported = [_value(samples, "cashguard_kill_switch_active") for samples, _ in results]
        assert (1 if halted else 0) in reported, "no scrape in the round read the state"


# ── What the endpoint no longer claims ────────────────────────────────────────


async def test_account_figures_are_not_reported_as_placeholder_zeros(
    client: AsyncClient,
) -> None:
    samples, _ = await _scrape(client)

    for name in (
        "cashguard_account_cash_available",
        "cashguard_account_total_value",
        "cashguard_open_positions_count",
    ):
        assert not [key for key in samples if key[0] == name], f"{name} has no producer"


async def test_labels_are_bounded_and_carry_no_identifiers(
    pg: dict[str, Any], client: AsyncClient
) -> None:
    async with pg["factory"]() as session:
        session.add(VenueConfig(venue="unexpected-venue", kill_switch_active=True))
        await session.commit()
    await client.post("/v1/risk/kill-switch/enable", headers=_admin_headers(pg))

    samples, body = await _scrape(client)

    assert ADMIN_EMAIL not in body and str(pg["admin_id"]) not in body
    venues = {dict(labels).get("venue") for (_, labels) in samples} - {None}
    assert venues == set(VENUES)
    scopes = {dict(labels).get("scope") for (_, labels) in samples} - {None}
    assert scopes == {"global", *VENUES}
