"""Create dca_configs table.

Persistent paper-only configuration for KrakenDCAPlanner schedule-driven
accumulation. One row per (ticker, venue) pair stores the policy inputs needed
by the dedicated DCA scheduler.

This migration does not make DCA runnable through the main strategy runner and
does not introduce any live execution path.

Revision ID: 0014_dca_configs
Revises: 0013_dca_plan_states
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_dca_configs"
down_revision = "0013_dca_plan_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dca_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("venue", sa.String(50), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column(
            "fixed_cash_amount",
            sa.Numeric(20, 8),
            nullable=False,
            server_default="100",
        ),
        sa.Column("dip_buy_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("dip_threshold_pct", sa.Numeric(10, 4), nullable=False, server_default="5.0"),
        sa.Column("dip_buy_multiplier", sa.Numeric(10, 4), nullable=False, server_default="2.0"),
        sa.Column("dip_ema_period", sa.Integer(), nullable=False, server_default="20"),
        sa.Column(
            "min_cash_reserve",
            sa.Numeric(20, 8),
            nullable=False,
            server_default="500",
        ),
        sa.Column("max_position_percent", sa.Numeric(10, 4), nullable=False, server_default="25.0"),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("ticker", "venue", name="uq_dca_config_ticker_venue"),
    )
    op.create_index(
        "ix_dca_configs_ticker_venue",
        "dca_configs",
        ["ticker", "venue"],
    )
    op.create_index(
        "ix_dca_configs_enabled",
        "dca_configs",
        ["enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_dca_configs_enabled", table_name="dca_configs")
    op.drop_index("ix_dca_configs_ticker_venue", table_name="dca_configs")
    op.drop_table("dca_configs")
