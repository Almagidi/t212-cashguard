"""Add explicit order fee amount.

Revision ID: 0018_order_fee_amount
Revises: 0017_signal_decision_key
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_order_fee_amount"
down_revision = "0017_signal_decision_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("fee_amount", sa.Numeric(20, 8), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "fee_amount")
