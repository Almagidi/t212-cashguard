"""Arm the disposable Trading 212 controlled demo-order SQLite DB.

This only disables the kill switch in the local disposable DB used by
`t212-demo-controlled-order-start`.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def main() -> int:
    db_path = Path(
        os.environ.get(
            "T212_DEMO_ORDER_DB_PATH",
            "/tmp/t212_demo_controlled_order.db",
        )
    )

    if not db_path.exists():
        raise SystemExit(f"Controlled demo-order DB does not exist: {db_path}")

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(app_settings)").fetchall()}

        if "kill_switch_active" not in columns:
            raise SystemExit("app_settings.kill_switch_active column not found")

        if "auto_trading_enabled" in columns:
            conn.execute(
                "UPDATE app_settings "
                "SET kill_switch_active = 0, auto_trading_enabled = 1 "
                "WHERE id = 1"
            )
        else:
            conn.execute("UPDATE app_settings SET kill_switch_active = 0 WHERE id = 1")

        row = conn.execute("SELECT kill_switch_active FROM app_settings WHERE id = 1").fetchone()

    if row is None:
        raise SystemExit("app_settings row id=1 not found")

    print("  kill_switch_active=0")
    print("  auto_trading_enabled=1")
    print("  live trading remains disabled by server env")
    print("  scope=local disposable SQLite demo-order DB only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
