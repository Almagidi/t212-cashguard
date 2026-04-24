"""Add trade journal fields (notes + tags) to trades table.

Allows traders to annotate closed trades with free-text notes and
structured tags (e.g. "revenge_trade", "followed_plan", "FOMO").
These fields power the Trade Journal page in the UI.

Revision ID: 0005_trade_journal
Revises:     0004_cfd_funding_cost
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0005_trade_journal"
down_revision = "0004_cfd_funding_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column(
            "journal_notes",
            sa.Text,
            nullable=True,
            comment="Free-text trader notes for this trade",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "journal_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
            comment="List of string tags, e.g. ['followed_plan', 'FOMO']",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "journal_emotion",
            sa.String(50),
            nullable=True,
            comment="Trader emotion at time of trade: calm, anxious, confident, fearful, greedy",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "journal_rating",
            sa.SmallInteger,
            nullable=True,
            comment="Trader self-rating 1-5 on trade execution quality",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "journal_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the journal entry was last updated",
        ),
    )


def downgrade() -> None:
    op.drop_column("trades", "journal_updated_at")
    op.drop_column("trades", "journal_rating")
    op.drop_column("trades", "journal_emotion")
    op.drop_column("trades", "journal_tags")
    op.drop_column("trades", "journal_notes")
