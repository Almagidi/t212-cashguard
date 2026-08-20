"""Add database-backed scheduled-signal idempotency key.

Revision ID: 0017_signal_decision_key
Revises: 0016_repair_venue_configs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_signal_decision_key"
down_revision = "0016_repair_venue_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("decision_key", sa.String(64), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_signals_decision_key",
            "signals",
            ["decision_key"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_signals_decision_key",
            table_name="signals",
            postgresql_concurrently=True,
        )
    op.drop_column("signals", "decision_key")
