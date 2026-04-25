"""Add execution-quality analytics fields.

Tracks expected price, slippage, broker acknowledgement/fill/reconciliation
timing, and normalized execution-quality scores on orders.

Revision ID: 0007_execution_quality_analytics
Revises:     0006_telegram_control_requests
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_execution_quality_analytics"
down_revision = "0006_telegram_control_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("execution_environment", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("expected_fill_price", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("slippage_pct", sa.Numeric(10, 4), nullable=True))
    op.add_column("orders", sa.Column("slippage_value", sa.Numeric(20, 8), nullable=True))
    op.add_column("orders", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("first_ack_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("broker_latency_ms", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("fill_latency_ms", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("reconciliation_latency_ms", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("execution_quality_score", sa.Numeric(5, 2), nullable=True))
    op.add_column("orders", sa.Column("execution_quality_grade", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("execution_quality_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(
        "ix_orders_execution_quality",
        "orders",
        ["execution_environment", "ticker", "order_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_execution_quality", table_name="orders")
    op.drop_column("orders", "execution_quality_notes")
    op.drop_column("orders", "execution_quality_grade")
    op.drop_column("orders", "execution_quality_score")
    op.drop_column("orders", "reconciliation_latency_ms")
    op.drop_column("orders", "fill_latency_ms")
    op.drop_column("orders", "broker_latency_ms")
    op.drop_column("orders", "rejected_at")
    op.drop_column("orders", "cancelled_at")
    op.drop_column("orders", "filled_at")
    op.drop_column("orders", "first_ack_at")
    op.drop_column("orders", "submitted_at")
    op.drop_column("orders", "slippage_value")
    op.drop_column("orders", "slippage_pct")
    op.drop_column("orders", "expected_fill_price")
    op.drop_column("orders", "execution_environment")
