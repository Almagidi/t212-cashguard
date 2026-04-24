"""Add overnight CFD funding cost tracking table.

Creates a new table ``cfd_funding_costs`` that records the daily financing
charge applied to each CFD position held overnight.  The Celery beat task
``track_cfd_funding`` runs at 22:00 UTC (before NYSE close) to estimate and
persist the charge before EOD flatten decisions are made.

Formula:
  daily_charge = notional × funding_rate / 360
  notional     = quantity × current_price
  funding_rate = broker_overnight_rate (T212 calls this 'overnightFee')

Scientific basis:
  Chan (2013): overnight funding cost is the primary drag on intraday
  mean-reversion strategies — it reduces net edge when positions are
  carried overnight and must be modelled explicitly in the P&L simulation.

Revision ID: 0004_cfd_funding_cost
Revises:     0003_cfd_risk_profile
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_cfd_funding_cost"
down_revision = "0003_cfd_risk_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cfd_funding_costs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(50), nullable=False, index=True),
        sa.Column(
            "strategy_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price_at_close", sa.Numeric(20, 8), nullable=False,
                  comment="Price used to compute notional at end-of-day"),
        sa.Column("notional", sa.Numeric(20, 8), nullable=False,
                  comment="quantity × price_at_close"),
        sa.Column("annual_rate_pct", sa.Numeric(8, 5), nullable=False,
                  comment="Annualised funding rate as a % (e.g. 5.25)"),
        sa.Column("daily_charge", sa.Numeric(20, 8), nullable=False,
                  comment="notional × annual_rate / 360"),
        sa.Column("currency", sa.String(10), nullable=False, server_default="USD"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )
    op.create_index(
        "ix_cfd_funding_ticker_date",
        "cfd_funding_costs",
        ["ticker", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cfd_funding_ticker_date", "cfd_funding_costs")
    op.drop_table("cfd_funding_costs")
