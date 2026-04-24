"""Initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # sessions
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.Text()),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)

    # broker_connections
    op.create_table(
        "broker_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker", sa.String(50), nullable=False, server_default="trading212"),
        sa.Column("environment", sa.String(10), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("api_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_ok", sa.Boolean()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("account_id", sa.String(100)),
        sa.Column("account_currency", sa.String(10)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("user_id", "broker", "environment", name="uq_broker_user_env"),
    )

    # broker_accounts_snapshots
    op.create_table(
        "broker_accounts_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("broker_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshotted_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("total_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("cash", sa.Numeric(20, 8), nullable=False),
        sa.Column("free_funds", sa.Numeric(20, 8), nullable=False),
        sa.Column("invested", sa.Numeric(20, 8), nullable=False),
        sa.Column("result", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_broker_snapshots_connection_time", "broker_accounts_snapshots", ["connection_id", "snapshotted_at"])

    # instruments
    op.create_table(
        "instruments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("currency_code", sa.String(10), nullable=False),
        sa.Column("isin", sa.String(20)),
        sa.Column("extended_hours", sa.Boolean(), server_default="false"),
        sa.Column("working_schedule_id", sa.Integer()),
        sa.Column("min_trade_value", sa.Numeric(20, 8)),
        sa.Column("max_open_quantity", sa.Numeric(20, 8)),
        sa.Column("buy_lot_size", sa.Numeric(20, 8)),
        sa.Column("sell_lot_size", sa.Numeric(20, 8)),
        sa.Column("trading_enabled", sa.Boolean(), server_default="true"),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_instruments_ticker", "instruments", ["ticker"], unique=True)

    # exchange_schedules
    op.create_table(
        "exchange_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(50), nullable=False),
        sa.Column("open_time", sa.String(10), nullable=False),
        sa.Column("close_time", sa.String(10), nullable=False),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
    )

    # risk_profiles
    op.create_table(
        "risk_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("max_risk_per_trade_pct", sa.Numeric(5, 2), nullable=False, server_default="1.0"),
        sa.Column("max_daily_loss_pct", sa.Numeric(5, 2), nullable=False, server_default="3.0"),
        sa.Column("max_open_positions", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_position_size_pct", sa.Numeric(5, 2), nullable=False, server_default="10.0"),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("stop_after_consecutive_losses", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("symbol_cooldown_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("force_flat_eod", sa.Boolean(), server_default="true"),
        sa.Column("is_default", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # strategies
    op.create_table(
        "strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("is_enabled", sa.Boolean(), server_default="false"),
        sa.Column("is_live", sa.Boolean(), server_default="false"),
        sa.Column("risk_profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("risk_profiles.id")),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("allowed_tickers", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("session_start", sa.String(10), server_default="09:30"),
        sa.Column("session_end", sa.String(10), server_default="16:00"),
        sa.Column("extended_hours", sa.Boolean(), server_default="false"),
        sa.Column("eod_flatten", sa.Boolean(), server_default="true"),
        sa.Column("last_signal_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # strategy_runs
    op.create_table(
        "strategy_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("signals_generated", sa.Integer(), server_default="0"),
        sa.Column("orders_placed", sa.Integer(), server_default="0"),
        sa.Column("error", sa.Text()),
    )

    # signals
    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("signal_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("entry_price", sa.Numeric(20, 8)),
        sa.Column("stop_price", sa.Numeric(20, 8)),
        sa.Column("take_profit_price", sa.Numeric(20, 8)),
        sa.Column("suggested_quantity", sa.Numeric(20, 8)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("reason", sa.Text()),
        sa.Column("risk_rejected", sa.Boolean(), server_default="false"),
        sa.Column("risk_rejection_reason", sa.Text()),
        sa.Column("params_snapshot", postgresql.JSONB()),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_signals_strategy_ticker", "signals", ["strategy_id", "ticker"])
    op.create_index("ix_signals_status", "signals", ["status"])

    # orders
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.id")),
        sa.Column("client_order_key", sa.String(100), nullable=False),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8)),
        sa.Column("stop_price", sa.Numeric(20, 8)),
        sa.Column("time_validity", sa.String(10), server_default="DAY"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending_intent"),
        sa.Column("broker_order_id", sa.String(100)),
        sa.Column("filled_quantity", sa.Numeric(20, 8)),
        sa.Column("avg_fill_price", sa.Numeric(20, 8)),
        sa.Column("is_dry_run", sa.Boolean(), server_default="false"),
        sa.Column("cash_used", sa.Numeric(20, 8)),
        sa.Column("available_cash_at_submission", sa.Numeric(20, 8)),
        sa.Column("broker_request", postgresql.JSONB()),
        sa.Column("broker_response", postgresql.JSONB()),
        sa.Column("error_message", sa.Text()),
        sa.Column("retry_count", sa.Integer(), server_default="0"),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_orders_client_order_key", "orders", ["client_order_key"], unique=True)
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_ticker_status", "orders", ["ticker", "status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])

    # order_events
    op.create_table(
        "order_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("from_status", sa.String(30)),
        sa.Column("to_status", sa.String(30)),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # positions_snapshots
    op.create_table(
        "positions_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("broker_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("avg_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8)),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8)),
        sa.Column("quantity_available", sa.Numeric(20, 8)),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("snapshotted_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_positions_connection_ticker", "positions_snapshots", ["connection_id", "ticker"])
    op.create_index("ix_positions_snapshotted_at", "positions_snapshots", ["snapshotted_at"])

    # trades
    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(50), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("open_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id")),
        sa.Column("close_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id")),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("open_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("close_price", sa.Numeric(20, 8)),
        sa.Column("realized_pnl", sa.Numeric(20, 8)),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategies.id")),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("is_dry_run", sa.Boolean(), server_default="false"),
    )
    op.create_index("ix_trades_ticker_opened", "trades", ["ticker", "opened_at"])

    # risk_events
    op.create_table(
        "risk_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("ticker", sa.String(50)),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("message", sa.Text()),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_risk_events_type", "risk_events", ["event_type"])
    op.create_index("ix_risk_events_occurred", "risk_events", ["occurred_at"])

    # alerts
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_type", sa.String(50), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("is_read", sa.Boolean(), server_default="false"),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])

    # app_settings (single-row config)
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("theme", sa.String(20), server_default="dark"),
        sa.Column("timezone", sa.String(50), server_default="UTC"),
        sa.Column("market_data_provider", sa.String(50), server_default="mock"),
        sa.Column("auto_trading_enabled", sa.Boolean(), server_default="false"),
        sa.Column("kill_switch_active", sa.Boolean(), server_default="false"),
        sa.Column("live_trading_unlocked", sa.Boolean(), server_default="false"),
        sa.Column("daily_stats_reset_time", sa.String(10), server_default="00:00"),
        sa.Column("extra", postgresql.JSONB(), server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", sa.String(100)),
        sa.Column("actor", sa.String(100), nullable=False, server_default="system"),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_occurred", "audit_logs", ["occurred_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])


def downgrade() -> None:
    tables = [
        "audit_logs", "app_settings", "alerts", "risk_events", "trades",
        "positions_snapshots", "order_events", "orders", "signals",
        "strategy_runs", "strategies", "risk_profiles", "exchange_schedules",
        "instruments", "broker_accounts_snapshots", "broker_connections",
        "sessions", "users",
    ]
    for table in tables:
        op.drop_table(table)
