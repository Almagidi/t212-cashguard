"""Create dca_plan_states table.

Persistent state for KrakenDCAPlanner schedule-driven accumulation.
One row per (ticker, venue) pair stores the minimum state required to
enforce cadence and track allocation history between planner evaluations.

This migration satisfies deployment prerequisite #1 from kraken_dca_planner.py:
  "A dca_plan_states DB table exists storing DCAState fields."

RUNNABLE, scheduler wiring, and paper execution are NOT part of this migration.

Revision ID: 0013_dca_plan_states
Revises: 0012_venue_columns
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_dca_plan_states"
down_revision = "0012_venue_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dca_plan_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("venue", sa.String(50), nullable=False),
        sa.Column("last_buy_at", sa.Date(), nullable=True),
        sa.Column("last_decision_at", sa.Date(), nullable=True),
        sa.Column(
            "total_allocated_usd",
            sa.Numeric(20, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("executions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_decision_code", sa.String(50), nullable=True),
        sa.Column("last_reason", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("ticker", "venue", name="uq_dca_plan_state_ticker_venue"),
    )
    op.create_index(
        "ix_dca_plan_states_ticker_venue",
        "dca_plan_states",
        ["ticker", "venue"],
    )


def downgrade() -> None:
    op.drop_index("ix_dca_plan_states_ticker_venue", table_name="dca_plan_states")
    op.drop_table("dca_plan_states")
