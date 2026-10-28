"""Retain terminal manual paper command identities.

Revision ID: 0022_paper_operation_receipts
Revises: 0021_order_submission_attempts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_paper_operation_receipts"
down_revision = "0021_order_submission_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_operation_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_domain", sa.String(160), nullable=False),
        sa.Column("request_version", sa.SmallInteger(), nullable=False),
        sa.Column("canonical_request", postgresql.JSONB(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=False),
        sa.Column("decision_code", sa.String(100)),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "operation_id", name="uq_paper_operation_owner_identity"),
        sa.CheckConstraint("request_version = 1", name="ck_paper_operation_version"),
        sa.CheckConstraint(
            "(outcome = 'order_recorded' AND order_id IS NOT NULL AND http_status = 201) OR "
            "(outcome = 'blocked' AND order_id IS NULL AND http_status BETWEEN 400 AND 499 "
            "AND decision_code IS NOT NULL AND length(decision_code) > 0 "
            "AND detail IS NOT NULL AND length(detail) > 0)",
            name="ck_paper_operation_terminal_result",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE paper_operation_receipts IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM paper_operation_receipts)")).scalar_one():
        raise RuntimeError("Refusing downgrade: retained paper operation evidence would be lost.")
    op.drop_table("paper_operation_receipts")
