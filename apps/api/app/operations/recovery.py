"""Offline status/block/verify, independent of app.main and environment settings.

Requires a selected dotenv file AND independently confirmed database identity.
No automatic retry after a connection/COMMIT error: status --operation-id checks
the receipt/current controls without writing; absence is still unconfirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from dotenv.parser import parse_stream
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services.recovery_controls import RecoveryRefusal, is_blocked, read_controls

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

ACTION = "recovery.controls_blocked"
ACTOR = "offline_recovery"
PAYLOAD = {"version": 1, "app_rows": 1, "venue_rows": 2, "venues": ["kraken", "t212"]}
# Exact persisted representation emitted by PaperExecutionEngine._paper_connection.
# Keep this offline module independent of engine/settings/broker imports.
_PAPER_CREDENTIAL_MARKER = "paper-only-no-real-credential"


@dataclass(frozen=True)
class Target:
    url: URL
    master_key: str | None


def load_target(path: Path, *, host: str, port: int, database: str, role: str) -> Target:
    """No .env search, inherited values or variable interpolation."""
    values: dict[str, str | None] = {}
    try:
        with path.open(encoding="utf-8") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise RecoveryRefusal("Selected configuration contains an invalid dotenv line.")
                if binding.key is None:
                    continue
                key = binding.key.upper()
                if key in values:
                    raise RecoveryRefusal("Selected configuration has duplicate keys.")
                values[key] = binding.value
        raw = values.get("DATABASE_URL")
        if not raw or "${" in raw:
            raise RecoveryRefusal(
                "DATABASE_URL must be explicit and fully resolved in the selected file."
            )
        url = make_url(raw)
        if (
            url.drivername != "postgresql+asyncpg"
            or url.query
            or not url.password
            or (url.host, url.port or 5432, url.database, url.username)
            != (host, port, database, role)
        ):
            raise RecoveryRefusal(
                "Selected DATABASE_URL does not match the independently confirmed target."
            )
        master_key = values.get("MASTER_KEY")
        if master_key and "${" in master_key:
            master_key = None
        return Target(url, master_key)
    except (OSError, ValueError) as exc:
        raise RecoveryRefusal(
            "Selected configuration is unreadable or invalid; no target was changed."
        ) from exc


async def verify_identity(connection: AsyncConnection, target: Target) -> None:
    row = (await connection.execute(text("SELECT current_database(), current_user"))).one()
    if tuple(row) != (target.url.database, target.url.username):
        raise RecoveryRefusal("Connected database/role does not match the confirmed target.")
    await connection.execute(text("SET LOCAL search_path TO public"))
    await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
    await connection.execute(text("SET LOCAL lock_timeout = '3s'"))


async def operation_status(connection: AsyncConnection, operation_id: uuid.UUID) -> str:
    result = await connection.execute(
        text(
            "SELECT action, actor, entity_type, entity_id, payload FROM audit_logs WHERE id = :id"
        ),
        {"id": operation_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return "UNCONFIRMED"
    if (row["action"], row["actor"], row["entity_type"], row["entity_id"], row["payload"]) != (
        ACTION,
        ACTOR,
        "app_settings",
        "1",
        PAYLOAD,
    ):
        raise RecoveryRefusal("Operation UUID conflicts with an unrelated audit record.")
    return "COMMITTED"


async def block_controls(connection: AsyncConnection, operation_id: uuid.UUID) -> dict[str, Any]:
    """Caller owns ONE transaction. Locks/counts/update/audit/read-back are atomic."""
    # Table locks prevent missing/extra control-row phantoms as well as serialize
    # retry writers. Row locks alone cannot protect the singleton/inventory.
    await connection.execute(text("LOCK TABLE app_settings IN SHARE ROW EXCLUSIVE MODE"))
    await connection.execute(text("LOCK TABLE venue_configs IN SHARE ROW EXCLUSIVE MODE"))
    await connection.execute(text("SELECT id FROM app_settings ORDER BY id FOR UPDATE"))
    await connection.execute(text("SELECT venue FROM venue_configs ORDER BY venue FOR UPDATE"))
    controls = await read_controls(connection)
    prior = await operation_status(connection, operation_id)
    if prior == "COMMITTED":
        if not is_blocked(controls):
            raise RecoveryRefusal(
                "Controls changed after this operation. Review and use a NEW operation UUID."
            )
        return {"operation": "COMMITTED", "reused": True, "controls": controls}
    app_rows = (
        await connection.execute(
            text(
                "UPDATE app_settings SET kill_switch_active = true, auto_trading_enabled = false, "
                "live_trading_unlocked = false WHERE id = 1 RETURNING id"
            )
        )
    ).all()
    venue_rows = (
        await connection.execute(
            text(
                "UPDATE venue_configs SET kill_switch_active = true, auto_trading_enabled = false "
                "RETURNING venue"
            )
        )
    ).all()
    if len(app_rows) != 1 or len(venue_rows) != 2:
        raise RecoveryRefusal("Unexpected control update count; transaction must roll back.")
    await connection.execute(
        text(
            "INSERT INTO audit_logs (id, action, actor, entity_type, entity_id, payload) "
            "VALUES (:id, :action, :actor, 'app_settings', '1', CAST(:payload AS jsonb))"
        ),
        {"id": operation_id, "action": ACTION, "actor": ACTOR, "payload": json.dumps(PAYLOAD)},
    )
    after = await read_controls(connection)
    if not is_blocked(after):
        raise RecoveryRefusal("Control read-back failed; transaction must roll back.")
    return {"operation": "COMMITTED", "reused": False, "controls": after}


async def verify_credentials(connection: AsyncConnection, master_key: str | None) -> dict[str, Any]:
    rows = (
        (
            await connection.execute(
                text(
                    "SELECT id, broker, environment, is_active, api_key_encrypted, "
                    "api_secret_encrypted "
                    "FROM broker_connections ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    if not master_key:
        return {
            "status": "FAIL",
            "reason": "Original explicit MASTER_KEY is required.",
            "credentials": [],
        }
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest()))
    result = []
    for row in rows:
        if (
            row["broker"] == "paper"
            and row["environment"] == "mock"
            and row["api_key_encrypted"] == _PAPER_CREDENTIAL_MARKER
            and row["api_secret_encrypted"] == _PAPER_CREDENTIAL_MARKER
        ):
            state = "not_required"
        else:
            try:
                cipher.decrypt(row["api_key_encrypted"].encode()).decode()
                cipher.decrypt(row["api_secret_encrypted"].encode()).decode()
                state = "readable"
            except (InvalidToken, UnicodeError, AttributeError):
                state = "unreadable"
        result.append({"id": str(row["id"]), "active": row["is_active"], "status": state})
    return {
        "status": (
            "FAIL"
            if any(r["status"] not in {"readable", "not_required"} for r in result)
            else "PASS"
        ),
        "credentials": result,
        "warnings": (
            [
                "Original key is weak/default-like. Verification does not approve future use or rotate it."
            ]
            if len(master_key.strip()) < 32
            or any(
                fragment in master_key.lower()
                for fragment in ("change-me", "change_me", "changeme")
            )
            else []
        ),
        "scope": (
            "Local decryption or exact credential-free paper classification only. "
            "Paper-only data cannot prove key correctness; compare backup/schema/financial "
            "evidence separately."
        ),
    }


async def run(target: Target, command: str, operation_id: uuid.UUID | None) -> dict[str, Any]:
    engine = create_async_engine(target.url, poolclass=NullPool, connect_args={"timeout": 5})
    try:
        async with engine.connect() as connection, connection.begin():
            if command == "block":
                await connection.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            else:
                await connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            await verify_identity(connection, target)
            if command == "block":
                if operation_id is None:
                    raise RecoveryRefusal("Block requires a retained explicit operation UUID.")
                result = await block_controls(connection, operation_id)
            else:
                controls = await read_controls(connection)
                result = {"controls": controls}
                if operation_id is not None:
                    result["operation"] = await operation_status(connection, operation_id)
                    result["currently_blocked"] = is_blocked(controls)
                if command == "verify":
                    result["verification"] = await verify_credentials(connection, target.master_key)
        # Nothing reports COMMITTED until the transaction context actually commits.
        result["target"] = {
            "host": target.url.host,
            "port": target.url.port or 5432,
            "database": target.url.database,
            "role": target.url.username,
        }
        return result
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "block", "verify"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--expect-port", type=int, required=True)
    parser.add_argument("--expect-database", required=True)
    parser.add_argument("--expect-role", required=True)
    parser.add_argument("--operation-id", type=uuid.UUID)
    args = parser.parse_args()
    try:
        target = load_target(
            args.env_file,
            host=args.expect_host,
            port=args.expect_port,
            database=args.expect_database,
            role=args.expect_role,
        )
        result = asyncio.run(run(target, args.command, args.operation_id))
    except RecoveryRefusal as exc:
        print(json.dumps({"status": "REFUSED", "detail": str(exc)}))
        return 2
    except (Exception, KeyboardInterrupt):
        print(
            json.dumps(
                {
                    "status": "UNCONFIRMED",
                    "detail": "Operation did not return confirmed success. Stop writers; inspect status with the SAME "
                    "operation UUID and current controls before any retry. No automatic retry occurred.",
                }
            )
        )
        return 3
    print(json.dumps(result))
    return 1 if result.get("verification", {}).get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
