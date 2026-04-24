"""Add version column to orders and cooldown tracking

Revision ID: 0002_order_version
Revises: 0001_initial
Create Date: 2025-01-02 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_order_version"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Optimistic locking version column on orders
    op.add_column("orders", sa.Column("version", sa.Integer(), server_default="1", nullable=False))

    # Index for version-based lookups
    op.create_index("ix_orders_version", "orders", ["id", "version"])

    # Track which strategy generated a signal (for exit logic)
    op.add_column(
        "signals",
        sa.Column("parent_signal_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)
    )

    # Cooldown tracking index on trades
    op.create_index("ix_trades_ticker_closed", "trades", ["ticker", "closed_at"])


def downgrade() -> None:
    op.drop_index("ix_trades_ticker_closed", "trades")
    op.drop_column("signals", "parent_signal_id")
    op.drop_index("ix_orders_version", "orders")
    op.drop_column("orders", "version")
