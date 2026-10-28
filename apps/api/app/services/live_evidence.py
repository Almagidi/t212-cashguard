"""Anchored live evidence snapshots, independent of mutable display JSON."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select

from app.db.models import AppSettings, AuditLog, BrokerConnection
from app.services.live_account import LiveAccountBinding, binding_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

FIELDS = {
    "record_demo_validation": "demo_validated_at",
    "record_broker_test": "broker_test_verified_at",
    "record_telegram_test": "telegram_test_verified_at",
    "record_kill_switch_test": "kill_switch_tested_at",
    "unlock_live": "live_unlock_acknowledged_at",
}
_HEAD_KEYS = {
    "schema",
    "kind",
    "sequence",
    "action",
    "binding",
    "evidence",
    "references",
    "broker_test_id",
    "recorded_at",
    "prior_head_id",
    "notes",
    "actors",
    "test_references",
    "unlock_references",
}


class LiveEvidenceError(RuntimeError):
    """An authoritative evidence record is unavailable or malformed."""


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def normalize_db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fresh(value: Any, now: datetime) -> bool:
    at = parse_time(value)
    return at is not None and timedelta(0) <= now - at <= timedelta(hours=24)


def _audit_action(action: str) -> str:
    return {
        "unlock_live": "live_trading_unlocked",
        "lock_live": "live_trading_locked",
    }.get(action, "live_readiness_recorded")


def _valid_binding(data: Any) -> bool:
    from app.services.live_account import LiveAccountBinding

    if not isinstance(data, dict) or set(data) != set(LiveAccountBinding.__dataclass_fields__):
        return False
    if any(not isinstance(value, str) or not value for value in data.values()):
        return False
    try:
        uuid.UUID(data["owner_id"])
        uuid.UUID(data["connection_id"])
    except ValueError:
        return False
    return bool(data["broker"] == "trading212" and data["environment"] == "live")


def _validate_record(row: AuditLog) -> dict[str, Any]:
    payload = row.payload
    if not isinstance(payload, dict) or set(payload) != _HEAD_KEYS:
        raise LiveEvidenceError("Current live-readiness evidence is missing or malformed.")
    if (
        type(payload["schema"]) is not int
        or payload["schema"] != 2
        or payload["kind"] != "live_readiness"
    ):
        raise LiveEvidenceError("Current live-readiness evidence version is unsupported.")
    if type(payload["sequence"]) is not int or payload["sequence"] < 1:
        raise LiveEvidenceError("Current live-readiness sequence is invalid.")
    action = payload["action"]
    if (
        not isinstance(action, str)
        or action not in {*FIELDS, "lock_live"}
        or row.action != _audit_action(action)
    ):
        raise LiveEvidenceError("Current live-readiness action is invalid.")
    binding = payload["binding"]
    if binding is None:
        if action != "lock_live":
            raise LiveEvidenceError("Live evidence has no account binding.")
    elif not _valid_binding(binding) or str(row.user_id) != binding["owner_id"]:
        raise LiveEvidenceError("Live evidence ownership is invalid.")
    evidence, references = payload["evidence"], payload["references"]
    if not isinstance(evidence, dict) or not isinstance(references, dict):
        raise LiveEvidenceError("Current live-readiness records are invalid.")
    if not set(evidence) <= set(FIELDS.values()) or set(evidence) != set(references):
        raise LiveEvidenceError("Current live-readiness references are invalid.")
    for value in evidence.values():
        timestamp = parse_time(value)
        if timestamp is None or timestamp > datetime.now(UTC):
            raise LiveEvidenceError(
                "Live evidence timestamps are missing, malformed or future-dated."
            )
    actors, tests, unlock = (
        payload["actors"],
        payload["test_references"],
        payload["unlock_references"],
    )
    if (
        not isinstance(actors, dict)
        or set(actors) != set(evidence)
        or any(not isinstance(actor, str) for actor in actors.values())
    ):
        raise LiveEvidenceError("Live evidence attribution is invalid.")
    if not isinstance(tests, dict) or set(tests) != (
        set(evidence) & {FIELDS["record_broker_test"], FIELDS["unlock_live"]}
    ):
        raise LiveEvidenceError("Live broker-test review references are invalid.")
    if not isinstance(unlock, dict) or not set(unlock) <= set(FIELDS.values()):
        raise LiveEvidenceError("Live unlock references are invalid.")
    required_references = [*references.values(), *tests.values(), *unlock.values()]
    optional_references = [
        value
        for value in [payload["prior_head_id"], payload["broker_test_id"]]
        if value is not None
    ]
    for reference in [*required_references, *optional_references]:
        try:
            uuid.UUID(reference)
        except (ValueError, TypeError, AttributeError) as exc:
            raise LiveEvidenceError(
                "Current live-readiness reference identity is invalid."
            ) from exc
    field = FIELDS.get(action)
    if field is not None and references.get(field) != str(row.id):
        raise LiveEvidenceError("Current live evidence does not identify its recorded action.")
    if payload["notes"] is not None and not isinstance(payload["notes"], str):
        raise LiveEvidenceError("Current live-readiness notes are malformed.")
    at = parse_time(payload["recorded_at"])
    if at is None or at > datetime.now(UTC) or normalize_db_time(row.occurred_at) != at:
        raise LiveEvidenceError("Current live-readiness record time is untrusted.")
    if action == "lock_live":
        if binding is not None or evidence or references or actors or tests or unlock:
            raise LiveEvidenceError("Global lock snapshot contains permission evidence.")
    elif evidence.get(field) != payload["recorded_at"] or actors.get(field) != row.actor:
        raise LiveEvidenceError("Current live action time or attribution is inconsistent.")
    return payload


async def load_head(db: AsyncSession, app: AppSettings) -> tuple[AuditLog, dict[str, Any]] | None:
    if app.live_readiness_audit_id is None:
        return None
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.id == app.live_readiness_audit_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise LiveEvidenceError("Current live-readiness evidence is unavailable.")
    return row, _validate_record(row)


async def current_test_valid(
    db: AsyncSession,
    connection: BrokerConnection,
    binding: LiveAccountBinding,
    now: datetime,
) -> bool:
    if connection.last_test_audit_id is None or connection.last_test_ok is not True:
        return False
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.id == connection.last_test_audit_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    data = row.payload if row is not None else None
    if (
        row is None
        or not isinstance(data, dict)
        or set(data) != {"schema", "kind", "binding", "success", "tested_at"}
    ):
        return False
    at = parse_time(data["tested_at"])
    return bool(
        row.action == "live_broker_test_completed"
        and row.user_id == connection.user_id
        and type(data["schema"]) is int
        and data["schema"] == 2
        and data["kind"] == "live_broker_test"
        and data["binding"] == binding.payload()
        and data["success"] is True
        and fresh(data["tested_at"], now)
        and normalize_db_time(connection.last_test_at) == at
        and normalize_db_time(row.occurred_at) == at
    )


async def validated_evidence(
    db: AsyncSession,
    app: AppSettings,
    binding: LiveAccountBinding | None,
    connection: BrokerConnection | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    head = await load_head(db, app)
    if head is None or binding is None or connection is None:
        return {}, head[1] if head else None
    _, state = head
    if state["binding"] != binding.payload():
        return {}, state
    evidence: dict[str, Any] = {}
    # The protected current head is a complete snapshot. Historical action IDs
    # are provenance, not authority that can disappear or resurrect older grants.
    for action, field in FIELDS.items():
        if field not in state["evidence"] or field not in state["references"]:
            continue
        if action in {"record_broker_test", "unlock_live"} and state["test_references"].get(
            field
        ) != str(connection.last_test_audit_id):
            continue
        if action == "unlock_live" and state["unlock_references"] != state["references"]:
            continue
        evidence[field] = state["evidence"][field]
        evidence[field.removesuffix("_at") + "_by"] = state["actors"][field]
    return evidence, state


async def write_head(
    db: AsyncSession,
    app: AppSettings,
    *,
    binding: LiveAccountBinding | None,
    connection: BrokerConnection | None,
    action: str,
    actor: str,
    owner_id: uuid.UUID | None,
    notes: str | None = None,
) -> None:
    # Caller holds the settings row lock. A global stop must survive malformed history.
    try:
        old = await load_head(db, app)
    except LiveEvidenceError:
        if action != "lock_live":
            raise
        old = None
    before = old[1] if old else None
    same = before is not None and binding is not None and before["binding"] == binding.payload()
    evidence = dict(before["evidence"]) if same and before is not None else {}
    references = dict(before["references"]) if same and before is not None else {}
    actors = dict(before["actors"]) if same and before is not None else {}
    tests = dict(before["test_references"]) if same and before is not None else {}
    unlock_refs = dict(before["unlock_references"]) if same and before is not None else {}
    event_id = uuid.uuid4()
    now = datetime.now(UTC)
    field = FIELDS.get(action)
    if field is not None:
        evidence[field] = now.isoformat()
        references[field] = str(event_id)
        actors[field] = actor
        if action in {"record_broker_test", "unlock_live"}:
            tests[field] = str(connection.last_test_audit_id) if connection else None
        if action == "unlock_live":
            unlock_refs = dict(references)
    else:
        evidence.pop("live_unlock_acknowledged_at", None)
        references.pop("live_unlock_acknowledged_at", None)
        actors.pop("live_unlock_acknowledged_at", None)
        tests.pop("live_unlock_acknowledged_at", None)
        unlock_refs = {}
    data = {
        "schema": 2,
        "kind": "live_readiness",
        "sequence": before["sequence"] + 1 if before else 1,
        "action": action,
        "binding": binding.payload() if binding else None,
        "evidence": evidence,
        "references": references,
        "broker_test_id": str(connection.last_test_audit_id)
        if connection and connection.last_test_audit_id
        else None,
        "recorded_at": now.isoformat(),
        "prior_head_id": str(app.live_readiness_audit_id) if app.live_readiness_audit_id else None,
        "notes": notes,
        "actors": actors,
        "test_references": tests,
        "unlock_references": unlock_refs,
    }
    event = AuditLog(
        id=event_id,
        user_id=owner_id,
        action=_audit_action(action),
        actor=actor,
        payload=data,
        occurred_at=now,
    )
    db.add(event)
    await db.flush([event])
    app.live_readiness_audit_id = event_id
    await db.flush()


async def record_broker_test(
    db: AsyncSession,
    connection: BrokerConnection,
    *,
    success: bool,
    actor: str,
) -> None:
    now = datetime.now(UTC)
    binding = binding_for(connection)
    event = AuditLog(
        id=uuid.uuid4(),
        user_id=connection.user_id,
        action="live_broker_test_completed",
        actor=actor,
        occurred_at=now,
        payload={
            "schema": 2,
            "kind": "live_broker_test",
            "binding": binding.payload(),
            "success": success,
            "tested_at": now.isoformat(),
        },
    )
    db.add(event)
    await db.flush([event])
    connection.last_test_at, connection.last_test_ok = now, success
    connection.last_test_audit_id = event.id
    await db.flush()


def unanchored_audit_condition() -> Any:
    """Retention must not remove a current authorization or broker-test head."""
    return ~exists().where(AppSettings.live_readiness_audit_id == AuditLog.id) & ~exists().where(
        BrokerConnection.last_test_audit_id == AuditLog.id
    )
