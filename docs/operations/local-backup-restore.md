# Local backup and restore boundary

Restore into a separately created destination; keep the original database and
its key material intact. This procedure is for a controlled rehearsal first,
with synthetic data. It does not authorize modifying an owner's database or
credentials. PostgreSQL dumps contain sensitive application data and password
hashes even when broker credentials are encrypted.

1. Stop API, workers and beat for the selected installation. Confirm ownership
   before stopping anything. Keep broker access blocked and do not queue work.
2. Record source SHA, migration head, PostgreSQL version and intended source and
   destination identities without printing connection secrets. Independently
   retain the correct `MASTER_KEY` and `SECRET_KEY` under secure owner custody;
   they are not contained in a database dump or committed source. Do not put
   keys in a source handoff or beside a broadly readable backup.
3. Create a private backup directory. Use PostgreSQL16 `pg_dump --format=custom`
   for the explicitly selected database. Check its exit status and warnings,
   record the archive hash, and use `pg_restore --list` to check readability.
   A database dump excludes cluster roles/tablespaces; provision the intended
   destination owner separately. Never restore an untrusted archive.
4. Create a new empty destination owned by the rehearsal. Restore with
   `pg_restore --exit-on-error --single-transaction --no-owner --no-acl`
   against that exact destination. Do not use `--clean` or `--create` to replace
   an existing database. A truncated/bad archive must fail while the original
   source remains usable; archive listing alone does not prove a successful
   restore.
5. Compare migration version, schema/index/constraint definitions and financial
   history. Compare Decimal values without floating-point conversion, order and
   submission/EOD evidence, audit rows and encrypted credential bytes. Using
   the correct synthetic key must decrypt the fixture; wrong and missing keys
   must fail before any service is permitted to use restored credentials.

## No automatic resumption

Raw restore preserves saved control values, including an enabled automation
flag. Startup now refuses saved automation/live-unlock permission, including an
ordinary armed restart, without changing it. This permission check is not a
backup/key-integrity certificate. Seeding must not be an implicit recovery step.

Before starting the restored API, use the offline tool on the new destination
only. It imports no application runtime, broker or telemetry initializer. Select
the actual private configuration and independently confirm host, port, database
and role. Inherited environment values are ignored and interpolated/ambiguous
connection values refused. Run from `apps/api` with its virtual environment.
Replace this example target with the separately verified new destination:

```bash
python -m app.operations.recovery status \
  --env-file /private/path/recovery.env \
  --expect-host 127.0.0.1 --expect-port 5432 \
  --expect-database cashguard_recovery --expect-role cashguard
```

Stop every execution writer. Generate and privately retain a fresh operation UUID
(for example `python -c 'import uuid; print(uuid.uuid4())'`). Then explicitly block:

```bash
python -m app.operations.recovery block \
  --env-file /private/path/recovery.env \
  --expect-host 127.0.0.1 --expect-port 5432 \
  --expect-database cashguard_recovery --expect-role cashguard \
  --operation-id YOUR_RETAINED_UUID
python -m app.operations.recovery verify \
  --env-file /private/path/recovery.env \
  --expect-host 127.0.0.1 --expect-port 5432 \
  --expect-database cashguard_recovery --expect-role cashguard \
  --operation-id YOUR_RETAINED_UUID
```

Blocking takes bounded ordered control-table/row locks, validates the singleton
and supported venue inventory, sets application kill=true/auto=false/live-unlock=
false and both venue kill=true/auto=false, and commits one audit record atomically.
It preserves credentials, timestamps, other settings, history and sessions. No
encryption key is needed to reduce risk. Verification is separate and read-only;
wrong/missing keys fail verification while committed blocking stays effective.
Only credential IDs/readability are reported, never keys/ciphertext/plaintext.
This does not replace the schema/financial comparison in step 5.
Read-only verification attempts an explicitly supplied original key even if it
is historically weak, and reports a warning separately. It does not approve a
weak key for future use or relax non-mock startup validation. Do not rotate a key
merely to pass verification; that would make preserved ciphertext unreadable.

After a connection/commit interruption, outcome is **UNCONFIRMED**. Do not blindly
retry or generate a new UUID. First run `status` with the same target arguments
and `--operation-id YOUR_RETAINED_UUID`; inspect the receipt AND current controls.
Missing receipt remains unconfirmed, not proof of rollback. An explicit same-UUID
retry serializes behind the earlier transaction and reuses success only when
its matching receipt and currently blocked controls agree. Later rearm causes
refusal and requires a newly reviewed explicit operation. A receipt does not
fence future operator changes.

Verify the expected settings row exists, all intended venue rows remain present,
and every read-back value is blocked. Missing/ambiguous state is a refusal, not
permission to seed or fabricate missing history. Keep workers and beat stopped,
DSNs/exporters empty, runtime mock and live disabled during
the rehearsal. Do not reuse old Redis queues for the restored instance.

Start only the loopback API/site and verify login, persisted blocked controls,
preserved history and safe operator status. Exercise the actual runner's
blocked path with a broker-construction tripwire if included in the rehearsal.
Re-enabling anything is a separate explicit operator decision after recovery;
neither successful restore nor a green health response authorizes it.

Only exact `/v1/health/live` is available before startup admission. Readiness and
every other HTTP/WebSocket operation stay closed if saved permissions are armed,
state/schema is unknown or the database cannot be validated. Lifespan-off startup
stays closed. Admission belongs to that lifespan and is revoked on shutdown or
failure. A later explicit audited rearm preserves readiness, but the next armed
restart requires review/unarming. Fresh unarmed starts preserve clear-kill
defaults. Restored already-unarmed data can pass this check without proving full
recovery verification.

Wrong/missing keys are not repaired by generating another key. After unarming,
mock-mode local login/history do not require every inactive credential to
decrypt. Unreadable credentials remain unavailable to their broker under existing
per-use checks; recover the original key before permitting restored credential
use or declaring recovery complete. Literal paper/demo/live modes retain strict
secret checks. This workflow never rotates keys or activates/deactivates stored
credentials. Intentional rotation/reconnection remains separate.
Restored sessions/JWT validity and deliberate session revocation need a separate
operating policy; this procedure does not claim to revoke existing sessions.

This bounded rehearsal does not qualify PostgreSQL power-loss/WAL recovery,
Redis task durability, Alertmanager/Grafana restoration, external storage,
real broker credentials, production traffic or unattended trading. Record each
unrun gate explicitly. Release recovery evidence must use the final source tree.
API admission does not admit workers/beat: protective EOD exits and timeout
cancellations have distinct policies and are unchanged. Do not start those
processes under this manual-installation qualification.

Primary references: [PostgreSQL16 pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
and [pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html).
