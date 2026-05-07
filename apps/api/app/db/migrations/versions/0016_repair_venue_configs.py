"""Repair missing venue_configs table on stale local databases.

Some long-lived local databases can be stamped at the current Alembic head while
missing the venue_configs table introduced by 0011. Re-create only that safety
table when absent and seed fail-safe venue rows without overwriting existing
operator choices.

Revision ID: 0016_repair_venue_configs
Revises: 0015_worker_heartbeats
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_repair_venue_configs"
down_revision = "0015_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("venue_configs"):
        op.create_table(
            "venue_configs",
            sa.Column("venue", sa.String(50), primary_key=True),
            sa.Column("kill_switch_active", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("auto_trading_enabled", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("degraded_mode_active", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    op.execute(
        sa.text(
            "INSERT INTO venue_configs "
            "(venue, kill_switch_active, auto_trading_enabled, degraded_mode_active) "
            "VALUES "
            "('t212', false, false, false), "
            "('kraken', false, false, false) "
            "ON CONFLICT (venue) DO NOTHING"
        )
    )


def downgrade() -> None:
    # Do not drop venue_configs during a repair migration downgrade. The table
    # belongs to 0011; dropping it here could erase valid local safety settings.
    pass
