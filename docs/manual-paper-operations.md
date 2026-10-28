# Identified manual paper orders

This protocol is for the local PostgreSQL APP_MODE=mock paper ledger. It does not
qualify broker demo/live execution, autonomous strategies or workers.

The supported Orders page saves one operation UUID and the exact submitted input
in this tab's session storage before sending. No credentials, tokens, headers or
DSNs are included. Storage denial or invalid input prevents submission. Pending
inputs are locked. After a lost/uncertain response, use **Check Result** or
**Retry Same Operation**; reload keeps the identity. Only **New Paper Order**,
after a recorded result, creates a genuinely new intent. Equal inputs with a
different UUID are deliberately separate commands.

Recorded responses show actual order status. Replay reports already recorded
without new financial/risk/audit effects. A recorded refusal stays refused on
replay, even if a halt is later removed. A new intent requests a new risk decision.
An existing receipt can be read after kill activation; new commands still pass
all current cash/risk/kill gates. No retry unlocks or resumes automation.

Unknown results or schema-rejected pending inputs must not be cleared for a new
UUID. Keep the reference and reconcile the ledger/technical issue. A missing
receipt does not prove an earlier transaction cannot still commit. Logout removes
financial input but retains an opaque unresolved reference. Closing/clearing the
tab, lost storage or destructive history/backup restoration can lose identity:
manual ledger review is then required. No global exactly-once or cross-browser
intent-recognition guarantee is made.

## API and compatibility

POST /v1/orders/paper accepts optional operation_id. Identified HTTP commands
require X-Paper-Expected-Owner equal to the authenticated user's UUID. This is an
equality precondition, never an authority selector; authentication alone selects
receipt/account ownership. Missing/stale/spoofed values fail before execution and
do not reveal receipts. The client checks its owner before dispatch and does not
queue paper mutations for automatic reconnect dispatch.

Same owner/UUID with changed validated canonical inputs returns409. Version1
binds full defaults/nulls and lossless Decimal values. New Orders return201;
replay returns200 with replayed:true. Recorded refusals retain their original4xx,
code and detail. Only matching schema-valid operation_state:recorded establishes
a known result; status alone is insufficient. No-ID callers retain legacy
new-request behavior and are outside this retry guarantee. Real broker schemas
and execution remain unchanged.

GET /v1/orders/paper/operations/{uuid} reads only the authenticated owner's receipt,
including for admins. It makes no business writes and calls no paper-account
creation helper. Existing global legacy history is not an owner-isolated recovery
facility.
The same expected-owner header is required on lookup; a stale account cannot
complete another owner's saved intent using a coincidentally equal UUID.

## Persistence and maintenance

Run the normal Alembic upgrade first. Receipt and financial/normal audit effects
commit in one PostgreSQL transaction. Late/internal errors roll back; a fresh read
can reconcile uncertain commit evidence but never executes an order. Independent
rejection audits remain separate from proof of financial commit.

Receipts remain with ledger evidence, including refusals without Orders. There is
no TTL/pruner. Restrictive User/Order foreign keys and a locked nonempty-downgrade
refusal protect replay evidence. Future coordinated account/history deletion needs
separate review. Backup/restore includes receipts; normal fail-closed startup and
rearm rules still apply.
