"""Add durable EOD flatten operation identity.

Revision ID: 0020_eod_flatten_operations
Revises: 0019_strategy_version
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_eod_flatten_operations"
down_revision = "0019_strategy_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("broker_account_scope", sa.String(160), nullable=True))
    op.create_index("ix_orders_broker_account_scope", "orders", ["broker_account_scope"])
    op.create_table(
        "eod_flatten_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "operation_kind",
            sa.String(30),
            nullable=False,
            server_default="eod_flatten",
        ),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("venue", sa.String(50), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("execution_environment", sa.String(20), nullable=False),
        sa.Column("broker_account_scope", sa.String(160), nullable=True),
        sa.Column("exchange_session_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("attributable_quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(40), nullable=False, server_default="claimed"),
        sa.Column(
            "requires_manual_reconciliation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "operation_kind",
            "strategy_id",
            "venue",
            "exchange_session_date",
            "ticker",
            name="uq_eod_flatten_operation_identity",
        ),
        sa.UniqueConstraint("order_id", name="uq_eod_flatten_operation_order"),
    )
    op.create_index(
        "ix_eod_flatten_operations_session",
        "eod_flatten_operations",
        ["exchange", "exchange_session_date", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Check under the same exclusive locks required by the destructive DDL. Otherwise
    # a writer can commit evidence after the check while DROP waits for its lock.
    bind.execute(sa.text("LOCK TABLE orders, eod_flatten_operations IN ACCESS EXCLUSIVE MODE"))
    has_evidence = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM eod_flatten_operations) "
            "OR EXISTS (SELECT 1 FROM orders WHERE broker_account_scope IS NOT NULL)"
        )
    ).scalar_one()
    if has_evidence:
        # Completed operations remain financial history; terminal order status does
        # not authorize deleting an account identity or an EOD recovery record.
        raise RuntimeError(
            "Refusing to downgrade: EOD operation history or order account identity "
            "would be lost. Preserve the current schema and use forward recovery."
        )
    op.drop_index(
        "ix_eod_flatten_operations_session",
        table_name="eod_flatten_operations",
    )
    op.drop_table("eod_flatten_operations")
    op.drop_index("ix_orders_broker_account_scope", table_name="orders")
    op.drop_column("orders", "broker_account_scope")
