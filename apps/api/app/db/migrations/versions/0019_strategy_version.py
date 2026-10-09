"""Add optimistic-locking version to strategies.

Revision ID: 0019_strategy_version
Revises: 0018_order_fee_amount
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_strategy_version"
down_revision = "0018_order_fee_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("strategies", "version")
