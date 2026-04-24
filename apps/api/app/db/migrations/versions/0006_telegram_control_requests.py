"""Add Telegram control confirmations.

Stores confirmation-gated Telegram control requests such as pause/resume,
kill switch, cancel-all, and flatten-all so mobile supervision has an audit
trail and survives process restarts.

Revision ID: 0006_telegram_control_requests
Revises:     0005_trade_journal
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_telegram_control_requests"
down_revision = "0005_trade_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_control_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("confirmation_code", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_telegram_control_requests_chat_id", "telegram_control_requests", ["chat_id"])
    op.create_index("ix_telegram_control_requests_user_id", "telegram_control_requests", ["user_id"])
    op.create_index("ix_telegram_control_requests_action", "telegram_control_requests", ["action"])
    op.create_index("ix_telegram_control_requests_confirmation_code", "telegram_control_requests", ["confirmation_code"], unique=True)
    op.create_index("ix_telegram_control_requests_status", "telegram_control_requests", ["status"])
    op.create_index(
        "ix_telegram_control_status_expiry",
        "telegram_control_requests",
        ["status", "expires_at"],
    )
    op.create_index("ix_telegram_control_requests_requested_at", "telegram_control_requests", ["requested_at"])


def downgrade() -> None:
    op.drop_index("ix_telegram_control_requests_requested_at", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_status_expiry", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_requests_status", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_requests_confirmation_code", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_requests_action", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_requests_user_id", table_name="telegram_control_requests")
    op.drop_index("ix_telegram_control_requests_chat_id", table_name="telegram_control_requests")
    op.drop_table("telegram_control_requests")
