# Selected-account live authorization

This change refuses live submission unless the current active administrator, exact
Trading 212 live connection, account scope and encrypted credential revision match
the saved reviews and the original order intent. It does not enable live trading.

Readiness actions use the authenticated account owner. A caller without an explicit
owner or trusted bound adapter cannot authorize an account by choosing the newest
connection. Changing accounts, reconnecting, rotating keys, losing permission, or
recording another broker test requires applicable fresh review and explicit unlock.
Legacy timestamps in `app_settings.extra` cannot authorize live submission.

Migration `0023_live_readiness_anchors` adds nullable audit references. Existing
rows remain unbound and fail closed; it neither changes credentials nor arms any
account. The protected current readiness record contains the complete snapshot;
older records are provenance only. The current broker-test event has its own UUID,
so repeated tests at an identical timestamp cannot revive an earlier review. Both
current records are excluded from retention and protected by restrictive foreign
keys. A global live lock remains available when prior evidence is corrupt, missing
or future-dated. Its actor is retained without taking an owner foreign-key lock.

The existing 24-hour expiry and supervision checks remain. Readiness is evaluated
after matching row locks. Admission locks are transaction-local, with a two-second
PostgreSQL lock wait limit; no database lock is held while sending an order. The
committed attempt records the authorizing head and broker-test IDs. Immediately
before the actual send, after a rate-limit wait, a fresh read refuses any changed
binding, approval, test, stop, resolved entry source or unresolved required configuration. Its one-use permit
is bound to the originating task, adapter and exact wire request. It cannot recall
an already-sent request or guarantee exactly-once broker execution. The existing
unknown-outcome/reconciliation handling remains necessary. Paper command receipts
provide no live-order idempotency guarantee.

Live startup and factories require a resolvable real market-data configuration;
`auto` cannot fall back to mock. Existing mock objects refuse data reads after a
runtime switch to live. Entry intents retain their configured source and cannot
silently adopt another source. This is **configuration protection**, not proof of
fresh quotes, full-market coverage, vendor entitlement or suitable fallback data.
Those are separate release blockers. Protective sells do not acquire a new feed
configuration prerequisite; existing readiness, cash-only, kill and cancellation
rules still apply. Mock/paper mode remains available for isolated development.

Use the supported PostgreSQL migration workflow with application writers stopped
for planned maintenance. Empty anchors permit downgrade/upgrade. Once either
anchor is populated, downgrade refuses before schema changes; table locks serialize
that precondition with concurrent writers. Do not delete retained authorization
records or clear anchors to force a downgrade. Preserve the database and keys and
use a reviewed recovery/restore plan instead.

Primary contracts consulted:

- [Trading 212 public API](https://docs.trading212.com/api) and
  [official API account support](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key).
- [PostgreSQL 16 row locks and deadlocks](https://www.postgresql.org/docs/16/explicit-locking.html).
- [SQLAlchemy populate_existing](https://docs.sqlalchemy.org/en/21/orm/queryguide/api.html#populate-existing)
  and [SELECT FOR UPDATE](https://docs.sqlalchemy.org/en/21/core/selectable.html#sqlalchemy.sql.expression.Select.with_for_update).
- [Python 3.12 task context inheritance](https://docs.python.org/3.12/library/asyncio-task.html)
  and [context variables](https://docs.python.org/3.12/library/contextvars.html).

Synthetic tests cannot certify real-money readiness. A clean demo observation week,
strategy, broker reconciliation, feed, monitoring, privacy and integrated release
gates remain separate requirements.
