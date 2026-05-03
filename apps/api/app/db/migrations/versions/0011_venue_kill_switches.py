"""Create venue_configs table with kill-switch and auto-trading controls.

Each venue row holds the independent safety controls for that execution venue.
Safe defaults: kill_switch=false, auto_trading=false, degraded=false.
Seed rows created for t212 and kraken.

Revision ID: 0011_venue_kill_switches
Revises: 0007_execution_quality_analytics
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_venue_kill_switches"
down_revision = "0007_execution_quality_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            server_default=sa.text("NOW()"),
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO venue_configs (venue, kill_switch_active, auto_trading_enabled, degraded_mode_active)"
            " VALUES"
            " ('t212',   false, false, false),"
            " ('kraken', false, false, false)"
        )
    )


def downgrade() -> None:
    op.drop_table("venue_configs")
