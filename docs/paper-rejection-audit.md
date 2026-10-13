# Paper rejection audit durability

The paper engine writes these two rejection actions through a separate,
bounded PostgreSQL transaction:

- `paper_follow_up_fill_blocked`
- `paper_sell_fill_blocked`

After that transaction acknowledges its commit, its evidence survives a rollback
of the caller's transaction. The writer never commits or rolls back the caller's
session. Successful fill audits and all other audit actions still belong to the
caller transaction and roll back with its financial effects.

The independent session uses the caller's engine and schema translation. It does
not load connection settings or access any broker. SQLite and StaticPool cannot
supply this independent PostgreSQL transaction contract. They fail explicitly with
`PaperExecutionError` status 503, preserving the rejection reason and reporting
that durable rejection audit is unavailable. There is no transactional fallback.
Tests that assert durable rejection evidence must use disposable PostgreSQL data.

The writer stores `AuditLog.user_id` as NULL. A foreign key to the caller's user
could deadlock behind its row lock or an uncommitted user insertion. Attribution
is retained as the UUID string `payload.audit_user_id`, together with the actor,
order entity identity and rejection payload. That snapshot does not assert that
the user or order creation subsequently committed.

The writer applies a three-second cancellation deadline. Driver or connection
cleanup can extend elapsed time; this is not a strict wall-clock guarantee. Sink
errors or timeouts fail closed with status 503 and do not permit the financial
operation. Cancellation propagates.
Cancellation or a connection failure before commit acknowledgement means the
audit outcome is unknown: a row may already exist. The writer never retries an
uncertain commit, so it cannot produce a duplicate through an internal retry.
The separate transaction is not an outbox, and this change does not promise
evidence for a process killed before it can acknowledge a write.

The follow-up fill method currently serves local paper code and test harnesses;
this change adds no endpoint. Existing account locks, cash/share validation and
halt policy remain responsible for financial safety.
