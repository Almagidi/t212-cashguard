"""Read-only inventory of the historical ``cfd_funding_costs`` table.

The task that wrote this table is retired (see docs/CFD_FUNDING_RETIREMENT.md). Its rows
are estimates that were recorded against ordinary share positions, so they are not
broker-confirmed costs. This script only counts and summarises them. It runs SELECT
statements inside a read-only transaction and never changes a row.

Usage, from ``apps/api`` with the application's database settings in the environment:

    python scripts/inventory_cfd_funding_costs.py > cfd-funding-inventory.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

TABLE = "cfd_funding_costs"
REPEATED_DAY_LIMIT = 200
QUERIES: dict[str, str] = {
    "totals": (
        "SELECT count(*) AS row_count, min(recorded_at) AS first_recorded_at, "
        "max(recorded_at) AS last_recorded_at, coalesce(sum(daily_charge), 0) AS total_charge, "
        "sum(CASE WHEN strategy_id IS NULL THEN 1 ELSE 0 END) AS rows_without_strategy "
        f"FROM {TABLE}"
    ),
    "by_currency": (
        "SELECT currency, count(*) AS row_count, coalesce(sum(daily_charge), 0) AS total_charge "
        f"FROM {TABLE} GROUP BY currency ORDER BY currency"
    ),
    "by_annual_rate": (
        f"SELECT annual_rate_pct, count(*) AS row_count FROM {TABLE} "
        "GROUP BY annual_rate_pct ORDER BY annual_rate_pct"
    ),
    "by_ticker": (
        "SELECT ticker, count(*) AS row_count, coalesce(sum(daily_charge), 0) AS total_charge, "
        "min(recorded_at) AS first_recorded_at, max(recorded_at) AS last_recorded_at "
        f"FROM {TABLE} GROUP BY ticker ORDER BY ticker"
    ),
    "by_day": (
        f"SELECT date(recorded_at) AS day, count(*) AS row_count FROM {TABLE} "
        "GROUP BY date(recorded_at) ORDER BY day"
    ),
    "repeated_ticker_days": (
        f"SELECT ticker, date(recorded_at) AS day, count(*) AS row_count FROM {TABLE} "
        "GROUP BY ticker, date(recorded_at) HAVING count(*) > 1 "
        f"ORDER BY day, ticker LIMIT {REPEATED_DAY_LIMIT}"
    ),
}
PROVENANCE = (
    "Every row was written by the retired track_cfd_funding task as quantity x price x "
    "annual rate / 360. None was reported by a broker. Rows written before the cash-only fix "
    "were recorded against ordinary share positions. Days are in the database session's "
    "time zone."
)


async def inventory(connection: AsyncConnection) -> dict[str, Any]:
    """Summarise the table using SELECT statements only."""
    report: dict[str, Any] = {"table": TABLE, "provenance": PROVENANCE}
    for name, statement in QUERIES.items():
        rows = (await connection.execute(text(statement))).mappings().all()
        report[name] = [dict(row) for row in rows]
    return report


async def run_inventory(engine: AsyncEngine) -> dict[str, Any]:
    """Run the inventory in its own transaction, read-only on PostgreSQL, then roll back."""
    async with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return await inventory(connection)
        finally:
            await connection.rollback()


async def _run() -> dict[str, Any]:
    from app.db.session import engine

    return await run_inventory(engine)


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    report = asyncio.run(_run())
    json.dump(report, sys.stdout, indent=2, default=str, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
