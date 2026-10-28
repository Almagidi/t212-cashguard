"""Read saved startup permissions without configuration, keys or broker imports."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic.script import ScriptDirectory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

SUPPORTED_VENUES = ("kraken", "t212")


class RecoveryRefusal(Exception):
    """Only static, operator-safe messages belong in this exception."""


def supported_revision() -> str:
    directory = Path(__file__).resolve().parents[1] / "db" / "migrations"
    heads = ScriptDirectory(str(directory)).get_heads()
    if len(heads) != 1:
        raise RecoveryRefusal("Source migration inventory is ambiguous.")
    return heads[0]


async def read_controls(connection: AsyncConnection) -> dict[str, Any]:
    """Caller supplies a coherent transaction; this function never writes/seeds."""
    revisions = (
        (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    )
    if revisions != [supported_revision()]:
        raise RecoveryRefusal("Database migration is unsupported; review migrations offline.")
    application = (
        (
            await connection.execute(
                text(
                    "SELECT id, auto_trading_enabled, kill_switch_active, live_trading_unlocked "
                    "FROM app_settings ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    venues = (
        (
            await connection.execute(
                text(
                    "SELECT venue, auto_trading_enabled, kill_switch_active FROM venue_configs ORDER BY venue"
                )
            )
        )
        .mappings()
        .all()
    )
    if len(application) != 1 or application[0]["id"] != 1:
        raise RecoveryRefusal("Expected exactly canonical app_settings id 1; nothing was seeded.")
    if tuple(row["venue"] for row in venues) != SUPPORTED_VENUES:
        raise RecoveryRefusal(
            "Supported venue inventory is missing or ambiguous; nothing was seeded."
        )
    app_flags = {
        key: application[0][key]
        for key in ("auto_trading_enabled", "kill_switch_active", "live_trading_unlocked")
    }
    venue_flags = {
        row["venue"]: {key: row[key] for key in ("auto_trading_enabled", "kill_switch_active")}
        for row in venues
    }
    if any(type(value) is not bool for value in app_flags.values()) or any(
        type(value) is not bool for flags in venue_flags.values() for value in flags.values()
    ):
        raise RecoveryRefusal(
            "Saved control flags are unknown; explicit offline review is required."
        )
    return {"revision": revisions[0], "application": app_flags, "venues": venue_flags}


def is_armed(controls: dict[str, Any]) -> bool:
    return bool(
        controls["application"]["auto_trading_enabled"]
        or controls["application"]["live_trading_unlocked"]
        or any(row["auto_trading_enabled"] for row in controls["venues"].values())
    )


def is_blocked(controls: dict[str, Any]) -> bool:
    return (
        not is_armed(controls)
        and controls["application"]["kill_switch_active"] is True
        and all(row["kill_switch_active"] is True for row in controls["venues"].values())
    )


async def assert_saved_permissions_unarmed(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        raise RecoveryRefusal(
            "Startup permission validation requires the supported PostgreSQL database."
        )
    async with engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        controls = await read_controls(connection)
        if is_armed(controls):
            raise RecoveryRefusal(
                "Saved automation or live unlock is enabled. Stop execution writers, use the "
                "offline recovery status/block/verify workflow, then restart. Saved state was not changed."
            )
