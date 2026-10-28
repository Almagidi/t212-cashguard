"""Protect the current live readiness and broker-test evidence identities."""

import sqlalchemy as sa
from alembic import op

revision = "0023_live_readiness_anchors"
down_revision = "0022_paper_operation_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("live_readiness_audit_id", sa.UUID(), nullable=True))
    op.add_column("broker_connections", sa.Column("last_test_audit_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_settings_live_readiness_audit",
        "app_settings",
        "audit_logs",
        ["live_readiness_audit_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_broker_last_test_audit",
        "broker_connections",
        "audit_logs",
        ["last_test_audit_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Serialize the precondition with all writers before checking retained data.
    connection.execute(
        sa.text("LOCK TABLE broker_connections, app_settings IN ACCESS EXCLUSIVE MODE")
    )
    used = connection.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM app_settings WHERE live_readiness_audit_id IS NOT NULL)"
            " + (SELECT count(*) FROM broker_connections WHERE last_test_audit_id IS NOT NULL)"
        )
    )
    if used:
        raise RuntimeError("Refusing to discard retained live readiness or broker-test anchors.")
    op.drop_constraint("fk_broker_last_test_audit", "broker_connections", type_="foreignkey")
    op.drop_constraint("fk_settings_live_readiness_audit", "app_settings", type_="foreignkey")
    op.drop_column("broker_connections", "last_test_audit_id")
    op.drop_column("app_settings", "live_readiness_audit_id")
