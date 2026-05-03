"""Add venue column to strategies and orders.

Non-null with server default 't212' so existing rows are backfilled safely.
Kraken strategies/orders must be updated to venue='kraken' after migration.

Revision ID: 0012_venue_columns
Revises: 0011_venue_kill_switches
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_venue_columns"
down_revision = "0011_venue_kill_switches"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _ensure_venue_column(table_name: str, index_name: str) -> None:
    if not _column_exists(table_name, "venue"):
        op.add_column(
            table_name,
            sa.Column("venue", sa.String(50), nullable=False, server_default="t212"),
        )
    else:
        op.execute(
            sa.text(
                f"UPDATE {table_name} "
                "SET venue = 't212' "
                "WHERE venue IS NULL OR venue = 'trading212'"
            )
        )
        op.alter_column(
            table_name,
            "venue",
            existing_type=sa.String(50),
            nullable=False,
            server_default="t212",
        )

    if not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, ["venue"])


def upgrade() -> None:
    _ensure_venue_column("strategies", "ix_strategies_venue")
    _ensure_venue_column("orders", "ix_orders_venue")


def downgrade() -> None:
    if _index_exists("orders", "ix_orders_venue"):
        op.drop_index("ix_orders_venue", table_name="orders")
    if _index_exists("strategies", "ix_strategies_venue"):
        op.drop_index("ix_strategies_venue", table_name="strategies")
    if _column_exists("orders", "venue"):
        op.drop_column("orders", "venue")
    if _column_exists("strategies", "venue"):
        op.drop_column("strategies", "venue")
