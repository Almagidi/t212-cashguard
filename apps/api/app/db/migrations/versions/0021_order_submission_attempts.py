"""Add durable order submission attempts.

Revision ID: 0021_order_submission_attempts
Revises: 0020_eod_flatten_operations

Adds one table. No existing row is read or rewritten. The order status
``submission_unknown`` introduced with this revision fits the existing ``orders.status``
column and needs no schema change.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_order_submission_attempts"
down_revision = "0020_eod_flatten_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_submission_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("execution_environment", sa.String(20), nullable=True),
        sa.Column("broker_environment", sa.String(20), nullable=True),
        sa.Column("broker_account_scope", sa.String(160), nullable=True),
        sa.Column("expected_cost", sa.Numeric(20, 8), nullable=True),
        sa.Column("dispatch_committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("outcome_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_evidence", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("order_id", name="uq_order_submission_attempts_order_id"),
    )


def downgrade() -> None:
    # Never delete evidence of a submission whose outcome is still unknown. An order is
    # unresolved while its status is submission_unknown; the attempt row of an order that
    # reconciliation has since resolved is history and does not block the downgrade.
    bind = op.get_bind()
    unresolved_orders = bind.execute(
        sa.text("SELECT count(*) FROM orders WHERE status = 'submission_unknown'")
    ).scalar_one()
    if unresolved_orders:
        raise RuntimeError(
            f"Refusing to downgrade: {unresolved_orders} order(s) still have an unknown "
            "submission outcome. Their status must first become accepted, filled, cancelled, "
            "rejected or error through reconciliation."
        )
    op.drop_table("order_submission_attempts")
