"""PostgreSQL transactional recovery against per-test synthetic schemas only."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import AppSettings
from app.db.session import Base
from app.operations import recovery
from app.services.recovery_controls import (
    RecoveryRefusal,
    assert_saved_permissions_unarmed,
    is_armed,
    is_blocked,
    read_controls,
    supported_revision,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

DSN = os.getenv("POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DATABASE_URL required")


@pytest_asyncio.fixture
async def recovery_db() -> AsyncIterator[AsyncEngine]:
    assert DSN
    schema = "recovery_" + uuid.uuid4().hex
    admin = create_async_engine(DSN, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        DSN, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("CREATE TABLE alembic_version (version_num varchar(100) PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": supported_revision()},
            )
            await connection.execute(
                insert(AppSettings).values(
                    id=1,
                    auto_trading_enabled=False,
                    kill_switch_active=False,
                    live_trading_unlocked=False,
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO venue_configs (venue, auto_trading_enabled, kill_switch_active, degraded_mode_active) VALUES ('kraken', false, false, false), ('t212', false, false, false)"
                )
            )
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def snapshot(engine):
    async with engine.connect() as connection:
        return await read_controls(connection)


async def block(engine, operation_id):
    async with engine.begin() as connection:
        return await recovery.block_controls(connection, operation_id)


async def test_fresh_defaults_preserved_and_armed_restart_refused_without_writes(recovery_db):
    before = await snapshot(recovery_db)
    assert not is_armed(before) and not is_blocked(before)
    await assert_saved_permissions_unarmed(recovery_db)
    assert await snapshot(recovery_db) == before
    async with recovery_db.begin() as connection:
        await connection.execute(
            text(
                "UPDATE app_settings SET auto_trading_enabled = true, live_trading_unlocked = true"
            )
        )
    armed = await snapshot(recovery_db)
    with pytest.raises(RecoveryRefusal, match="Saved automation"):
        await assert_saved_permissions_unarmed(recovery_db)
    assert await snapshot(recovery_db) == armed


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM app_settings",
        "INSERT INTO app_settings (id, theme, timezone, market_data_provider, auto_trading_enabled, kill_switch_active, live_trading_unlocked, daily_stats_reset_time, extra) VALUES (2, 'dark', 'UTC', 'mock', false, false, false, '00:00', '{}')",
        "UPDATE app_settings SET auto_trading_enabled = NULL",
        "DELETE FROM venue_configs WHERE venue = 'kraken'",
        "INSERT INTO venue_configs (venue, auto_trading_enabled, kill_switch_active, degraded_mode_active) VALUES ('unknown', false, false, false)",
        "UPDATE alembic_version SET version_num = 'unsupported'",
    ],
)
async def test_unknown_cardinality_schema_and_flags_refuse_both_startup_and_block(recovery_db, sql):
    async with recovery_db.begin() as connection:
        if "= NULL" in sql:
            await connection.execute(
                text("ALTER TABLE app_settings ALTER COLUMN auto_trading_enabled DROP NOT NULL")
            )
        await connection.execute(text(sql))
    with pytest.raises(RecoveryRefusal):
        await assert_saved_permissions_unarmed(recovery_db)
    with pytest.raises(RecoveryRefusal):
        await block(recovery_db, uuid.uuid4())
    async with recovery_db.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM audit_logs"))).scalar() == 0


async def test_venue_arm_alone_refuses_startup(recovery_db):
    async with recovery_db.begin() as connection:
        await connection.execute(
            text("UPDATE venue_configs SET auto_trading_enabled = true WHERE venue = 'kraken'")
        )
    with pytest.raises(RecoveryRefusal, match="Saved automation"):
        await assert_saved_permissions_unarmed(recovery_db)


async def test_atomic_block_idempotency_and_old_operation_after_rearm(recovery_db):
    operation_id = uuid.uuid4()
    async with recovery_db.begin() as connection:
        await connection.execute(
            text(
                "UPDATE app_settings SET auto_trading_enabled = true, live_trading_unlocked = true"
            )
        )
        await connection.execute(text("UPDATE venue_configs SET auto_trading_enabled = true"))
        before = (
            await connection.execute(text("SELECT updated_at, theme, extra FROM app_settings"))
        ).one()
    result = await block(recovery_db, operation_id)
    assert is_blocked(result["controls"]) and not result["reused"]
    repeated = await block(recovery_db, operation_id)
    assert repeated["reused"]
    async with recovery_db.begin() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM audit_logs"))).scalar() == 1
        assert (
            await connection.execute(text("SELECT updated_at, theme, extra FROM app_settings"))
        ).one() == before
        await connection.execute(text("UPDATE app_settings SET auto_trading_enabled = true"))
    with pytest.raises(RecoveryRefusal, match="NEW operation"):
        await block(recovery_db, operation_id)
    assert is_armed(await snapshot(recovery_db))
    assert is_blocked((await block(recovery_db, uuid.uuid4()))["controls"])


async def test_unrelated_operation_collision_rolls_back(recovery_db):
    operation_id = uuid.uuid4()
    async with recovery_db.begin() as connection:
        await connection.execute(
            text("INSERT INTO audit_logs (id, action, actor) VALUES (:id, 'unrelated', 'other')"),
            {"id": operation_id},
        )
    before = await snapshot(recovery_db)
    with pytest.raises(RecoveryRefusal, match="conflicts"):
        await block(recovery_db, operation_id)
    assert await snapshot(recovery_db) == before


@pytest.mark.parametrize("interruption", [RuntimeError, asyncio.CancelledError])
async def test_audit_failure_or_precommit_interruption_rolls_back_all_controls(
    recovery_db, monkeypatch, interruption
):
    original = recovery.read_controls
    calls = 0

    async def interrupt(connection):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interruption("synthetic interruption after audit insert")
        return await original(connection)

    before = await snapshot(recovery_db)
    monkeypatch.setattr(recovery, "read_controls", interrupt)
    with pytest.raises(interruption):
        await block(recovery_db, uuid.uuid4())
    assert await snapshot(recovery_db) == before
    async with recovery_db.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM audit_logs"))).scalar() == 0


async def test_real_audit_constraint_failure_rolls_back_control_changes(recovery_db):
    async with recovery_db.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE audit_logs ADD CONSTRAINT synthetic_reject CHECK (action <> 'recovery.controls_blocked')"
            )
        )
    from sqlalchemy.exc import IntegrityError

    before = await snapshot(recovery_db)
    with pytest.raises(IntegrityError):
        await block(recovery_db, uuid.uuid4())
    assert await snapshot(recovery_db) == before


async def test_concurrent_same_operation_serializes_to_one_audit(recovery_db):
    operation_id = uuid.uuid4()
    results = await asyncio.gather(
        block(recovery_db, operation_id), block(recovery_db, operation_id)
    )
    assert sorted(r["reused"] for r in results) == [False, True]
    assert is_blocked(await snapshot(recovery_db))


async def test_lock_timeout_is_bounded_and_rolls_back(recovery_db):
    from sqlalchemy.exc import DBAPIError

    before = await snapshot(recovery_db)
    async with recovery_db.begin() as holder:
        await holder.execute(text("LOCK TABLE app_settings IN SHARE ROW EXCLUSIVE MODE"))
        async with recovery_db.begin() as blocked:
            await blocked.execute(text("SET LOCAL lock_timeout = '50ms'"))
            with pytest.raises(DBAPIError):
                await recovery.block_controls(blocked, uuid.uuid4())
    assert await snapshot(recovery_db) == before


@pytest.mark.parametrize(
    "key",
    [
        "synthetic-original-key-" + "x" * 32,
        "short-original-key",
        "change-me-original-key-" + "x" * 32,
    ],
)
async def test_wrong_and_missing_keys_never_prevent_committed_block_or_mutate_credentials(
    recovery_db,
    key,
):
    from cryptography.fernet import Fernet

    cipher = Fernet(
        recovery.base64.urlsafe_b64encode(recovery.hashlib.sha256(key.encode()).digest())
    )
    token = cipher.encrypt(b"synthetic-broker-placeholder").decode()
    async with recovery_db.begin() as connection:
        user_id, credential_id = uuid.uuid4(), uuid.uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, is_admin) VALUES (:id, 'synthetic@example.invalid', 'unchanged', true, true)"
            ),
            {"id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO broker_connections (id, user_id, broker, environment, api_key_encrypted, api_secret_encrypted, is_active) VALUES (:id, :user_id, 'trading212', 'demo', :token, :token, false)"
            ),
            {"id": credential_id, "user_id": user_id, "token": token},
        )
    await block(recovery_db, uuid.uuid4())
    async with recovery_db.connect() as connection:
        for wrong in [None, "synthetic-wrong-key-" + "y" * 32]:
            result = await recovery.verify_credentials(connection, wrong)
            assert result["status"] == "FAIL"
            assert token not in json.dumps(result)
        correct = await recovery.verify_credentials(connection, key)
        assert correct["status"] == "PASS"
        assert bool(correct["warnings"]) == (len(key) < 32 or "change-me" in key)
        after = (
            await connection.execute(
                text(
                    "SELECT api_key_encrypted, api_secret_encrypted, is_active FROM broker_connections"
                )
            )
        ).one()
        assert tuple(after) == (token, token, False)
    assert is_blocked(await snapshot(recovery_db))


async def _credential_snapshot(engine):
    async with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in (
                    await connection.execute(text(f"SELECT * FROM {table} ORDER BY {order}"))
                ).mappings()
            ]
            for table, order in (
                ("broker_connections", "id"),
                ("app_settings", "id"),
                ("venue_configs", "venue"),
                ("audit_logs", "id"),
            )
        }


async def _genuine_paper_connection(engine, active=True):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import User
    from app.execution.paper_engine import PaperExecutionEngine

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"paper-{uuid.uuid4()}@test.invalid",
            hashed_password="not-a-login",
            is_active=True,
            is_admin=True,
        )
        db.add(user)
        await db.flush()
        paper = await PaperExecutionEngine(db)._paper_connection(user)
        assert paper.broker == "paper" and paper.environment == "mock"
        assert (
            paper.api_key_encrypted
            == paper.api_secret_encrypted
            == ("paper-only-no-real-credential")
        )
        paper.is_active = active
        await db.commit()
        return paper.id


async def _encrypted_connection(engine, broker, environment, key, secret, active=False):
    async with engine.begin() as connection:
        owner, identity = uuid.uuid4(), uuid.uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, is_admin) "
                "VALUES (:id, :email, 'not-a-login', true, true)"
            ),
            {"id": owner, "email": f"credential-{owner}@test.invalid"},
        )
        await connection.execute(
            text(
                "INSERT INTO broker_connections "
                "(id, user_id, broker, environment, api_key_encrypted, api_secret_encrypted, "
                "is_active) VALUES (:id, :owner, :broker, :environment, :key, :secret, :active)"
            ),
            {
                "id": identity,
                "owner": owner,
                "broker": broker,
                "environment": environment,
                "key": key,
                "secret": secret,
                "active": active,
            },
        )
        return identity


@pytest.mark.parametrize("paper_active", [True, False])
async def test_genuine_paper_and_inactive_real_credentials_verify_without_mutation(
    recovery_db, paper_active
):
    from cryptography.fernet import Fernet

    master = "synthetic-restored-key-" + "x" * 40
    cipher = Fernet(
        recovery.base64.urlsafe_b64encode(recovery.hashlib.sha256(master.encode()).digest())
    )
    token = cipher.encrypt(b"synthetic-broker-placeholder").decode()
    paper = await _genuine_paper_connection(recovery_db, paper_active)
    real = await _encrypted_connection(recovery_db, "trading212", "demo", token, token)
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        valid = await recovery.verify_credentials(connection, master)
        assert valid["status"] == "PASS"
        assert {row["id"]: row["status"] for row in valid["credentials"]} == {
            str(paper): "not_required",
            str(real): "readable",
        }
        wrong = await recovery.verify_credentials(connection, "synthetic-wrong-key-" + "y" * 40)
        assert wrong["status"] == "FAIL"
        assert {row["id"]: row["status"] for row in wrong["credentials"]} == {
            str(paper): "not_required",
            str(real): "unreadable",
        }
    assert await _credential_snapshot(recovery_db) == before
    for result in (valid, wrong):
        rendered = json.dumps(result)
        assert master not in rendered and token not in rendered
        assert "synthetic-broker-placeholder" not in rendered


@pytest.mark.parametrize("missing", [None, ""])
async def test_paper_only_still_requires_an_explicit_master_key(recovery_db, missing):
    await _genuine_paper_connection(recovery_db)
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        result = await recovery.verify_credentials(connection, missing)
    assert result["status"] == "FAIL" and result["credentials"] == []
    assert await _credential_snapshot(recovery_db) == before


async def test_paper_classification_is_not_a_decryption_or_key_correctness_proof(recovery_db):
    identity = await _genuine_paper_connection(recovery_db)
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        result = await recovery.verify_credentials(connection, "unproven-supplied-key-" + "z" * 40)
    assert result["status"] == "PASS"
    assert result["credentials"] == [
        {"id": str(identity), "active": True, "status": "not_required"}
    ]
    assert await _credential_snapshot(recovery_db) == before


@pytest.mark.parametrize(
    ("broker", "environment", "key", "secret"),
    [
        ("Paper", "mock", "paper-only-no-real-credential", "paper-only-no-real-credential"),
        ("paper", "MOCK", "paper-only-no-real-credential", "paper-only-no-real-credential"),
        ("paper", "demo", "paper-only-no-real-credential", "paper-only-no-real-credential"),
        ("trading212", "mock", "paper-only-no-real-credential", "paper-only-no-real-credential"),
        ("trading212", "demo", "paper-only-no-real-credential", "paper-only-no-real-credential"),
        ("paper", "mock", "paper-only-no-real-credential ", "paper-only-no-real-credential"),
        ("paper", "mock", "paper-only-no-real-credential", " paper-only-no-real-credential"),
        ("paper", "mock", "paper-only-no-real-credential-extra", "paper-only-no-real-credential"),
        ("paper", "mock", "paper-only-no-real-credential", "paper-only-no-real-credential-extra"),
        ("paper", "mock", "PAPER-ONLY-NO-REAL-CREDENTIAL", "paper-only-no-real-credential"),
        ("paper", "mock", "", "paper-only-no-real-credential"),
        ("paper", "mock", "paper-only-no-real-credential", ""),
        ("paper", "mock", "", ""),
    ],
)
async def test_near_match_or_real_broker_placeholders_never_receive_paper_classification(
    recovery_db, broker, environment, key, secret
):
    identity = await _encrypted_connection(recovery_db, broker, environment, key, secret)
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        result = await recovery.verify_credentials(connection, "synthetic-master-" + "x" * 40)
    assert result["status"] == "FAIL"
    assert result["credentials"] == [{"id": str(identity), "active": False, "status": "unreadable"}]
    assert await _credential_snapshot(recovery_db) == before


async def test_encrypted_paper_fields_keep_normal_decrypt_or_fail_behavior(recovery_db):
    from cryptography.fernet import Fernet

    master = "synthetic-original-key-" + "x" * 40
    cipher = Fernet(
        recovery.base64.urlsafe_b64encode(recovery.hashlib.sha256(master.encode()).digest())
    )
    token = cipher.encrypt(b"synthetic-encrypted-paper-value").decode()
    identity = await _encrypted_connection(recovery_db, "paper", "mock", token, token)
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        correct = await recovery.verify_credentials(connection, master)
        assert correct["status"] == "PASS"
        assert correct["credentials"] == [
            {"id": str(identity), "active": False, "status": "readable"}
        ]
        wrong = await recovery.verify_credentials(connection, "synthetic-wrong-key-" + "z" * 40)
        assert wrong["status"] == "FAIL" and wrong["credentials"][0]["status"] == "unreadable"
    assert await _credential_snapshot(recovery_db) == before


@pytest.mark.parametrize("field", ["api_key_encrypted", "api_secret_encrypted"])
async def test_corrupt_inactive_real_ciphertext_is_never_accepted(recovery_db, field):
    from cryptography.fernet import Fernet

    master = "synthetic-original-key-" + "x" * 40
    cipher = Fernet(
        recovery.base64.urlsafe_b64encode(recovery.hashlib.sha256(master.encode()).digest())
    )
    token = cipher.encrypt(b"synthetic-real-broker-value").decode()
    identity = await _encrypted_connection(recovery_db, "trading212", "demo", token, token)
    corrupted = token[:40] + ("A" if token[40] != "A" else "B") + token[41:]
    async with recovery_db.begin() as connection:
        await connection.execute(
            text(f"UPDATE broker_connections SET {field} = :token WHERE id = :id"),
            {"token": corrupted, "id": identity},
        )
    before = await _credential_snapshot(recovery_db)
    async with recovery_db.connect() as connection:
        result = await recovery.verify_credentials(connection, master)
    assert result["status"] == "FAIL"
    assert result["credentials"] == [{"id": str(identity), "active": False, "status": "unreadable"}]
    assert await _credential_snapshot(recovery_db) == before
    assert token not in json.dumps(result) and corrupted not in json.dumps(result)


async def test_unconfirmed_status_is_read_only_and_prior_commit_remains_visible(recovery_db):
    operation_id = uuid.uuid4()
    async with recovery_db.connect() as connection:
        assert await recovery.operation_status(connection, operation_id) == "UNCONFIRMED"
    await block(recovery_db, operation_id)
    async with recovery_db.connect() as connection:
        assert await recovery.operation_status(connection, operation_id) == "COMMITTED"
    assert (await block(recovery_db, operation_id))["reused"]


async def test_connected_server_identity_mismatch_is_refused_before_any_write(recovery_db):
    before = await snapshot(recovery_db)
    target = recovery.Target(recovery_db.url.set(database="wrong_database"), None)
    async with recovery_db.begin() as connection:
        with pytest.raises(RecoveryRefusal, match="Connected database/role"):
            await recovery.verify_identity(connection, target)
    assert await snapshot(recovery_db) == before
