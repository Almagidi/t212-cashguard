"""Safety state for ``/metrics``, read from the database at every scrape.

The database is the system of record for the kill switches and the auto-trading switches:
the API, Telegram and the Celery workers all write them there, and the risk engine and the
strategy runner read them there before acting. A value held in this process would miss
every change made by another process and every change made before this process started, so
nothing here is cached between scrapes.

Contract (one scrape = one short read-only transaction):

``cashguard_safety_state_read_ok``
    1 when this scrape read the state within its budget, 0 when it did not (database down,
    slow, locked, or too many scrapes at once). Always present.
``cashguard_safety_state_known{control, scope}``
    1 when the saved value of ``control`` (kill_switch, auto_trading; degraded_mode for a
    venue) for ``scope`` (global, or a venue) was read and is a real true/false; 0 when the
    read failed, the row is missing or the value is NULL. Always present, fixed label set.
``cashguard_kill_switch_active`` / ``cashguard_auto_trading_enabled``
    The saved global switches (``app_settings`` row 1). The global kill switch blocks every
    order that goes through the risk engine.
``cashguard_venue_kill_switch_active{venue}`` / ``..._auto_trading_enabled{venue}`` /
``..._degraded_mode_active{venue}``
    The saved per-venue switches (``venue_configs``), which the strategy runner obeys for
    strategies on that venue. Kept separate from the global switch: either can halt trading.
``cashguard_kill_switch_last_activation_timestamp_seconds``
    When the newest kill-switch activation still held in ``risk_events`` happened, from any
    writer. A timestamp, not a count: retention purges old events, and a count that went
    down would look like a counter reset.

A value series is present only when its value is known. Unknown is never reported as 0.
Nothing here contacts a broker, writes, or labels a series with a person, account or order.
Safety decisions never read these metrics; they read the database themselves.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select, text

from app.db.models import AppSettings, RiskEvent, VenueConfig

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from prometheus_client.metrics_core import Metric
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# The venues the operator view expects; a row for any other venue is not exported, so the
# label set stays fixed.
EXPECTED_VENUES = ("t212", "kraken")
STATE_READ_TIMEOUT_SECONDS = 2.0
_STATEMENT_TIMEOUT_MS = 1500
_LOCK_TIMEOUT_MS = 1000
# Scrapes beyond this many at once report "unknown" instead of queueing database connections.
_CONCURRENT_READS = 2
_reads_in_flight = asyncio.Semaphore(_CONCURRENT_READS)


@dataclass(frozen=True)
class Controls:
    """The saved switches of one scope. ``None`` means not known."""

    kill_switch_active: bool | None = None
    auto_trading_enabled: bool | None = None
    degraded_mode_active: bool | None = None


@dataclass(frozen=True)
class SafetyState:
    global_controls: Controls
    venues: dict[str, Controls] = field(default_factory=dict)
    last_activation_at: datetime | None = None


def _known(value: bool | None) -> bool | None:
    return None if value is None else bool(value)


async def read_safety_state(session: AsyncSession) -> SafetyState:
    """Read the saved switches in one read-only transaction, bounded on PostgreSQL."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
        await session.execute(text(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}"))

    settings_row = (
        await session.execute(
            select(AppSettings.kill_switch_active, AppSettings.auto_trading_enabled).where(
                AppSettings.id == 1
            )
        )
    ).one_or_none()
    global_controls = (
        Controls(_known(settings_row[0]), _known(settings_row[1]))
        if settings_row is not None
        else Controls()
    )

    venue_rows = (
        await session.execute(
            select(
                VenueConfig.venue,
                VenueConfig.kill_switch_active,
                VenueConfig.auto_trading_enabled,
                VenueConfig.degraded_mode_active,
            ).where(VenueConfig.venue.in_(EXPECTED_VENUES))
        )
    ).all()
    venues = {
        row[0]: Controls(_known(row[1]), _known(row[2]), _known(row[3])) for row in venue_rows
    }

    last_activation_at = (
        await session.execute(
            select(func.max(RiskEvent.occurred_at)).where(RiskEvent.event_type == "kill_switch_on")
        )
    ).scalar_one()
    return SafetyState(global_controls, venues, last_activation_at)


async def _read_once(
    session_factory: async_sessionmaker[AsyncSession], opened: list[Any]
) -> SafetyState:
    async with session_factory() as session:
        try:
            connection = await session.connection()
            opened.append((await connection.get_raw_connection()).driver_connection)
            state = await read_safety_state(session)
        except BaseException:
            # Drop the connection rather than roll back over it: after a failure or a
            # cancellation the server may not answer a rollback at all.
            await session.invalidate()
            raise
        await session.rollback()
        return state


def _abandon(read: asyncio.Future[SafetyState], opened: list[Any]) -> None:
    """Cut the read's connection, then cancel it, without waiting for either."""
    if read.done():
        return
    for driver_connection in opened:
        terminate = getattr(driver_connection, "terminate", None)
        if callable(terminate):
            terminate()
    read.cancel()


def _read_finished(task: asyncio.Task[SafetyState]) -> None:
    _reads_in_flight.release()
    if not task.cancelled() and task.exception() is not None:
        log.warning("metrics.safety_state_unread", reason=type(task.exception()).__name__)


async def scrape_safety_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> SafetyState | None:
    """The saved state, or ``None`` when it could not be read within the budget.

    The read runs as a task of its own so that the scrape answers within the budget even
    when the read cannot be stopped quickly. A cancelled query on a connection whose server
    has gone silent waits for that server, so on timeout the connection itself is cut first
    (asyncpg ``terminate()``: no waiting for pending data). The task holds its slot until it
    has finished, so stuck reads cannot pile up.
    """
    if _reads_in_flight.locked():
        log.warning("metrics.safety_state_unread", reason="concurrent_scrapes")
        return None
    await _reads_in_flight.acquire()  # immediate: locked() was False and nothing awaited since
    opened: list[Any] = []
    read = asyncio.ensure_future(_read_once(session_factory, opened))
    read.add_done_callback(_read_finished)
    try:
        done, _ = await asyncio.wait({read}, timeout=STATE_READ_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        # The scrape itself was abandoned (the scraper disconnected): stop the read too,
        # or it could hold its slot for as long as a silent server keeps the socket open.
        _abandon(read, opened)
        raise
    if read not in done:
        _abandon(read, opened)
        log.warning("metrics.safety_state_unread", reason="timeout")
        return None
    if read.cancelled() or read.exception() is not None:
        return None
    return read.result()


class _SafetyStateCollector(Collector):
    def __init__(self, state: SafetyState | None) -> None:
        self._state = state

    def collect(self) -> Iterator[Metric]:
        state = self._state
        read_ok = GaugeMetricFamily(
            "cashguard_safety_state_read_ok",
            "1 if this scrape read the saved safety state from the database, else 0",
        )
        read_ok.add_metric([], 1 if state is not None else 0)
        yield read_ok

        scopes: dict[str, Controls] = {
            "global": state.global_controls if state is not None else Controls()
        }
        for venue in EXPECTED_VENUES:
            scopes[venue] = state.venues.get(venue, Controls()) if state is not None else Controls()

        known = GaugeMetricFamily(
            "cashguard_safety_state_known",
            "1 if the saved value of a safety control was read and is set, else 0",
            labels=["control", "scope"],
        )
        for scope, controls in scopes.items():
            values = {
                "kill_switch": controls.kill_switch_active,
                "auto_trading": controls.auto_trading_enabled,
            }
            if scope != "global":
                values["degraded_mode"] = controls.degraded_mode_active
            for control, value in values.items():
                known.add_metric([control, scope], 0 if value is None else 1)
        yield known

        yield from _global_values(scopes["global"])
        yield from _venue_values({v: scopes[v] for v in EXPECTED_VENUES})

        if state is not None and state.last_activation_at is not None:
            yield GaugeMetricFamily(
                "cashguard_kill_switch_last_activation_timestamp_seconds",
                "Unix time of the newest kill-switch activation held in risk_events",
                value=state.last_activation_at.timestamp(),
            )


def _global_values(controls: Controls) -> Iterator[Metric]:
    for name, documentation, value in (
        (
            "cashguard_kill_switch_active",
            "Saved global kill switch (1=active, 0=inactive); absent when not known",
            controls.kill_switch_active,
        ),
        (
            "cashguard_auto_trading_enabled",
            "Saved global auto-trading switch (1=on, 0=off); absent when not known",
            controls.auto_trading_enabled,
        ),
    ):
        if value is not None:
            yield GaugeMetricFamily(name, documentation, value=int(value))


def _venue_values(venues: dict[str, Controls]) -> Iterator[Metric]:
    for name, documentation, attribute in (
        (
            "cashguard_venue_kill_switch_active",
            "Saved kill switch of one venue (1=active); absent when not known",
            "kill_switch_active",
        ),
        (
            "cashguard_venue_auto_trading_enabled",
            "Saved auto-trading switch of one venue (1=on); absent when not known",
            "auto_trading_enabled",
        ),
        (
            "cashguard_venue_degraded_mode_active",
            "Saved degraded mode of one venue (1=degraded); absent when not known",
            "degraded_mode_active",
        ),
    ):
        family = GaugeMetricFamily(name, documentation, labels=["venue"])
        for venue, controls in venues.items():
            value = getattr(controls, attribute)
            if value is not None:
                family.add_metric([venue], int(value))
        if family.samples:
            yield family


def render_safety_state(state: SafetyState | None) -> bytes:
    """Exposition text for one scrape's state, from a registry of its own."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_SafetyStateCollector(state))
    return generate_latest(registry)
