"""Create worker_heartbeats table.

Persisted liveness source for backend workers and scheduled jobs. This is
observability-only state and does not add execution controls, broker calls, or
strategy scheduling behavior.

Revision ID: 0015_worker_heartbeats
Revises: 0014_dca_configs
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_worker_heartbeats"
down_revision = "0014_dca_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("component", sa.String(100), nullable=False),
        sa.Column("worker_name", sa.String(255), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="healthy"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
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
        sa.UniqueConstraint("component", "worker_name", name="uq_worker_heartbeats_component_worker"),
    )
    op.create_index("ix_worker_heartbeats_component", "worker_heartbeats", ["component"])
    op.create_index("ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_last_seen_at", table_name="worker_heartbeats")
    op.drop_index("ix_worker_heartbeats_component", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
