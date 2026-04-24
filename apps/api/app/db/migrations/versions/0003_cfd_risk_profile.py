"""Add CFD-specific risk profile fields.

Adds five new columns to risk_profiles:
  cfd_max_risk_per_trade_pct    — tighter per-trade risk for leveraged CFDs
  cfd_max_daily_loss_pct        — tighter daily loss limit for CFDs
  max_overnight_cfd_exposure_pct — max notional CFD held overnight (% of equity)
  min_free_margin_pct            — margin guard (block new CFDs if margin thin)
  cfd_max_leverage               — hard leverage cap per CFD position

Scientific basis:
  FCA/ESMA product intervention (2018): retail CFD leverage capped 5–30x by asset class.
  Chan (2013): overnight funding cost erodes mean-reversion edge — force_flat_eod
  should be True for most CFD strategies.

Revision ID: 0003_cfd_risk_profile
Revises:     0002_order_version
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_cfd_risk_profile"
down_revision = "0002_order_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "risk_profiles",
        sa.Column(
            "cfd_max_risk_per_trade_pct",
            sa.Numeric(5, 2),
            nullable=True,
            comment="CFD per-trade risk cap (NULL = inherit equity limit)",
        ),
    )
    op.add_column(
        "risk_profiles",
        sa.Column(
            "cfd_max_daily_loss_pct",
            sa.Numeric(5, 2),
            nullable=True,
            comment="CFD intraday loss limit (NULL = inherit equity limit)",
        ),
    )
    op.add_column(
        "risk_profiles",
        sa.Column(
            "max_overnight_cfd_exposure_pct",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0.0",
            comment="Max notional CFD held overnight as % of equity; 0 = force flat",
        ),
    )
    op.add_column(
        "risk_profiles",
        sa.Column(
            "min_free_margin_pct",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="30.0",
            comment="Block new CFD entries if free_margin/equity < this %",
        ),
    )
    op.add_column(
        "risk_profiles",
        sa.Column(
            "cfd_max_leverage",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="5.0",
            comment="Max leverage (notional / equity) per CFD position",
        ),
    )


def downgrade() -> None:
    op.drop_column("risk_profiles", "cfd_max_leverage")
    op.drop_column("risk_profiles", "min_free_margin_pct")
    op.drop_column("risk_profiles", "max_overnight_cfd_exposure_pct")
    op.drop_column("risk_profiles", "cfd_max_daily_loss_pct")
    op.drop_column("risk_profiles", "cfd_max_risk_per_trade_pct")
