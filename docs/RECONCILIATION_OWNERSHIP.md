# Reconciliation ownership

Reconciliation asks the broker what happened to an order and updates the local order row.
It must have one owner at a time, so that two runs never decide the fate of the same order
at the same moment.

## What starts a reconciliation

| Entry point | Where |
|---|---|
| Scheduled task `reconcile_pending_orders` | Celery worker, `app/workers/tasks.py` |
| Demo reconciliation worker, run by the in-process scheduler | API process, `app/services/demo_reconciliation_scheduler.py` |
| Operator routes `POST /v1/broker/trading212/reconciliation/run-once` and `.../scheduler/run-once` | API process |
| Terminal scripts `t212_demo_reconciliation_worker.py`, `t212_demo_multi_order_reconciliation_smoke.py`, `t212_demo_reconcile_order.py` | `apps/api/scripts` |

## The rule

Every entry point takes the same distributed lock (`app/services/reconciliation_owner.py`,
Redis key `celery:task_lock:reconcile_pending_orders`) before it selects orders or reads
the broker. It commits its changes before it lets the lock go, and a run that fails or is
stopped discards its unsaved changes before it lets the lock go.

- **Not the owner:** nothing is selected and the broker is not read. The demo worker
  returns outcome `skipped_locked` and writes one audit row; the scheduler records a
  skipped tick (reason `reconciliation_owner_busy`), not a successful run; the scheduled
  task returns `already_running`.
- **Redis unavailable:** nobody can tell whether the lock is held, so nothing is
  reconciled. The demo worker raises; the operator route answers 503; the scheduler
  records a failed run. The scheduled task returns `owner_lock_unavailable` and logs an
  error; it does not call this `already_running`.
- **Time limit:** a demo run is stopped at `DEMO_RECONCILIATION_SCHEDULER_MAX_RUNTIME_SECONDS`
  (60 by default), always before the lock's lease ends (90 by default, never above 300
  unless the run limit itself is longer). A stopped run discards its unsaved work; if the
  stop lands while the run is saving, some results may already be saved. The operator
  route answers 504 and the next run checks the orders again.
- The lock name is the one the scheduled task has always used, so workers already running
  exclude the new code.

## Whose orders the scheduled task reads

An order carries three facts written when it was created: `execution_environment`, `venue`
and `broker_account_scope` (a hash of environment, user and the broker's account id, set by
the broker provider). The scheduled task uses them, and nothing else, to decide who may be
asked about an order.

| Situation | What happens |
|---|---|
| Order's environment is not the running `APP_MODE`, venue is not `t212`, order is a dry run, or its status is final | Never read |
| Order has an account scope and an active connection has the same scope | Read at that connection only |
| Order has an account scope and no active connection has it (another user, a disabled connection, a rotated account) | Not read. Counted as `out_of_scope.other_account` |
| Order has no account scope, has a broker order id, and exactly one connection is active | Read at that connection, by that exact id |
| Order has no account scope and no broker order id (an unknown submission), and the one active connection has no account id either | Looked up in that connection's history |
| Order has no account scope and no broker order id, and the one active connection knows its account | Not read. History is matched loosely (ticker, quantity, side, time), and nothing shows the order was placed in that account. Counted as `out_of_scope.no_account_identity` |
| Order has no account scope and several connections are active | Not read. Counted as `out_of_scope.no_account_identity` |
| Order has no recorded environment | Not read. Counted as `out_of_scope.no_environment` |
| A connection has no account id while other connections are active | Reads nothing. Counted as `connections_without_account_identity` |
| A connection's credentials cannot be decrypted | Switched off as before; the other connections still run. Counted as `connections_unusable` |

Connections are the active `trading212` connections for `APP_MODE`, in creation order. No
credentials are decrypted when no order is waiting.

Two kinds of order are read. An active order with a broker order id is looked up by that
id (see "Settling an order from the broker's record" below). An order whose submission
outcome is unknown and that has no broker order id is looked up in broker history; it is
never sent again. A legacy order stuck in `submitted` with no
broker order id cannot be looked up at all: it is logged on every run
(`execution.orphaned_submitted_order`) and counted as `orphaned_submitted`.

Reconciliation reaches the broker through an object that exposes two reads only
(`app/services/reconciliation_broker.py`). No write method of the adapter can be reached
through it.

The operator's demo paths apply a narrower version of the same rule: an order recorded for
one account is refused when the adapter is bound to a different one. When the adapter
carries no account identity (the terminal scripts build one directly) they behave as
before; they match by exact broker order id in the demo account's history.

## Settling an order from the broker's record

The broker is asked about an order by its exact id, in two places.

1. **The open orders** (`GET /equity/orders/{id}`). If the order is listed there and not
   final, it stays as it is (only this read can say an order is still open). If the
   answer is final and complete, the order is settled from it.
2. **The order history** (`GET /equity/history/orders`), when the broker answers "no such
   order" (HTTP 404: an order that has ended is no longer among the open orders), or when
   the open-orders answer is final but leaves out a number. The newest page of the whole
   account is read first; one page serves every order of the run (and the unknown
   submissions of an instrument share theirs) and is read from the broker once per run.
   The instrument's own history is read as well only when older records exist beyond that
   page: when the broker gives a `nextPagePath`, or, if it gives none, when the page is
   full (50 records). A record counts if its id, with surrounding space removed, is the
   order's id; a whole-number id is read as its digits.

A final state is written only from a record that passes every one of these checks
(`app/execution/broker_record.py`):

- it is about this order: the id, and whatever it states about instrument, side, order
  type and ordered quantity, agree with the order. What it does not state is not held
  against it; an order type this code does not know is not compared;
- it does not contradict itself: a fact written in two places (nested and flat) is the
  same in both;
- it states how much filled, even when that is nothing. A fill on its own (`fill.quantity`)
  is never taken for the total. Only the field names above are read; a record that uses
  others is refused, not guessed at;
- every number is finite. Amounts are compared at the stored scale of 8 decimal places,
  so float noise such as `0.30000000000000004` is the `0.3` that was sent. A sell may be
  written with negative quantities; the amount is kept. A negative amount for a buy is
  refused;
- the filled amount is not more than was ordered. `FILLED` needs the whole quantity and a
  price; a cancelled or rejected order with a fill needs that fill's price;
- every price of a fill is at least 0.00000001 and fits `orders.avg_fill_price`;
- several history records for one id are accepted only as the order's separate fills,
  and only when they add up to the total the broker states. The price kept is their
  average; all the records are kept as the evidence;
- a history record has a final status (`FILLED`, `CANCELLED`, `REJECTED`). Any other
  status, or none, is refused;
- when the history was read to complete an open-orders answer, it says the same status
  and, where the answer gave one, the same filled amount (`reads_disagree` otherwise).

A record that fails a check settles nothing. The order keeps its state, its cash stays
reserved, the reason is written to the `reconciliation_backoff` event (`reason`), and the
order waits as below. A record that contradicts the order is never looked past. The
broker's time of the fill is kept when it lies between the time the order was sent and
now; a cancellation or rejection takes the time of the read. A latency longer than its
32-bit column holds (24.8 days) is recorded at that ceiling, so an old order can still be
settled.

The same checks apply when an unknown submission is matched to a history record. A record
that, read in full, is about another order is not adopted. Otherwise the match gives the
order its broker id, and the order is settled only if the record passes the checks; if it
does not, the event `unknown_submission_identified` says why (`not_settled_because`). The
same fill-time, read-time and latency rules apply as above.

The broker's immediate answer to an order request has to agree with the order in the same
way. One that says it is about another instrument, side, type or quantity leaves the order
`submission_unknown` without a broker id; the id it named is kept in the attempt's
evidence only (`named_broker_order_id`), so nothing can later settle or cancel by it.

## The order of work

Each account has its own queue. Within it, orders that are due are taken longest-waiting
first: orders never looked at, oldest first, then orders by the time they were last looked
at, with creation time and id as tie-breakers. The accounts take turns, one order each. One
run handles at most 50 orders in all and starts no new order after 30 seconds; the broker
reads for one order may together take 10 seconds. What a run could not take is reported as `left_for_next_run`.

An order the broker was asked about moves to the back of its queue once the result is saved,
whatever the answer, so a backlog is worked through in turn. An order that was selected but not asked about stays
where it was: one whose account was left alone for the run (`deferred`), one another
writer held (`skipped_locked`), and an unknown submission dispatched under 90 seconds ago.
Because the queues are separate, an account that is being left alone does not take places
from the others.

## Waiting after an attempt that settled nothing

The wait is stored on the order: `retry_count` is the number of attempts in a row that
settled nothing, `last_reconciled_at` is when the last attempt was recorded. No new column
is used. An order that is waiting is left out before the 50 are chosen, so it does not
take a place.

| Attempts in a row | 1 | 2 | 3 | 4 | 5 | 6 or more |
|---|---|---|---|---|---|---|
| Wait | 30 s | 60 s | 120 s | 240 s | 480 s | 900 s |

| What happened | Effect on the order | Effect on the run |
|---|---|---|
| Broker answered and the order changed, or it is still working | Wait cleared | — |
| Unknown submission: history read, no single matching record | One step up | — |
| Unknown submission dispatched under 90 seconds ago | None; not counted as an attempt | — |
| Broker rate limit (429) | Stepped up until the wait covers a usable `Retry-After` | That account is left alone for the rest of the run |
| Broker refused the credentials (401/403) | One step up | That account is left alone for the rest of the run. The connection is not switched off |
| Broker answered that it will not serve this request (another 4xx) | One step up | — |
| Broker has no record: not among the open orders (404) and not in the history read (`no_broker_record`) | One step up | Counted as `no_broker_record`, not as `failed` |
| No usable answer: a 5xx, a 408 or 425, a network error, no answer in 10 seconds | One step up | After three in a row, that account is left alone for the rest of the run |
| Broker answered with a record that failed a check above (`broker_answer_refused`, with the `reason`) | One step up | — |
| The order's own save failed (`local_error`) | Rolled back whole, then one step up in a transaction of its own | The run stops if that cannot be saved either |
| Run cancelled or killed mid-order | Nothing recorded | Run ends; the row lock is released |

A `Retry-After` counts only if it is a finite number above zero, and never more than 900
seconds. A last attempt dated more than 60 seconds ahead of the worker's clock is
disregarded, so a wrong timestamp cannot hold an order back. Each step up writes one `reconciliation_backoff` order event with the outcome, the
error type, the count and the wait. No broker payload is stored in it.

An order that is at the longest wait and still does not settle for a reason of its own
(its record is missing or refused, an unknown submission still has no single matching
record, the broker refuses the read of it, or its settlement cannot be saved; not because
the broker cannot be reached) is raised as a critical risk
event, `order_not_settling`, with the outcome and reason. How the wait got to the longest
step does not matter. Reconciliation goes on asking every 15 minutes; the event is raised
again only if the order is still not settled a day later.

The broker's wait is applied to the order that received it. There is no place to store a
wait for the whole account without a schema change, so the next run may send one more
request, for a different order, before the account is left alone again.

## One order, one transaction, one row lock

For each order the scheduled task:

1. locks the row (`SELECT ... FOR UPDATE SKIP LOCKED`) and reads it again, replacing any
   copy the session still holds and checking that it is still in scope and still due;
2. asks the broker while it holds the lock;
3. writes the result, the wait and any event, and commits. The commit releases the lock.

So:

- A row another writer holds is skipped (`skipped_locked`); the broker is not asked.
- A row that was settled since it was selected is skipped (`no_longer_due`); the broker is
  not asked.
- A writer that arrives while the broker is being asked waits, then reads the committed
  result. A cancellation that finds the order filled is refused before the broker is
  asked to cancel.
- A run that lost the Redis lease and carries on cannot write from stale state: every
  write is made under the row lock from a row read under that lock.
- A crash loses at most the order in progress. Orders already handled are saved.
- The heartbeat is written after the last order, with no order lock held.

Every writer of `orders.status` goes through `app/execution/state_machine.py` on a loaded
row, and the `orders.version` column makes a flush from a stale copy fail instead of
overwriting. Writers that take the row lock first: `ExecutionEngine.cancel_order` (used by
the cancel route, `cancel_timed_out_orders` and the emergency stop), the recording of a
submission's outcome, and the paper engine for paper orders. The operator's demo
reconciliation relies on the version check alone.

The scheduled task holds one order lock at a time and asks for no other row lock while it
holds it. The rows it inserts (events, audit rows, a risk event) take the usual foreign-key
share locks on the rows they point to; no writer was found that holds one of those and then
waits for an order row, so no lock cycle is known.

`retry_count` is returned by the orders API. It was never written before; it now means
"reconciliation attempts in a row that settled nothing".

## What this does not do yet

- The Redis lock is still a lease without renewal and without a fencing token. Two owners
  at once are possible after a stall. For the scheduled task that is now harmless to order
  rows (see above); it can still mean duplicate read requests to the broker.
- The operator's demo reconciliation does not take row locks. It reads each order again
  before asking the broker and relies on the version check: a concurrent change makes the
  whole demo run fail and roll back rather than overwrite.
- The scheduled task and the demo worker still reconcile differently (lookup by id versus
  history) and can transition an order differently.
- Nothing here has been checked against the broker itself: whether an ended order is
  answered with 404 by id, how soon it appears in the history, the history's exact field
  names, and the sign of a sell's quantities. Both layouts the repository has seen are
  read, and anything else is refused rather than guessed.
- An order found neither among the open orders nor in the history stays active locally,
  waits up to 15 minutes between attempts, and is raised (`order_not_settling`) at most
  once a day. No operator action exists yet to close such an order by hand.
- The open-orders read counts any status other than `FILLED`, `CANCELLED` or `REJECTED`
  as still open, and only `WORKING` or `PENDING` move an unknown submission to
  `accepted`. Which status names the broker really uses has not been checked.
- A history page on which a record of the order fails a check ends the search; the
  instrument's own page is not read then.
- Only one page of history is read per search. An order that ended more than 50 orders of
  its own instrument ago is not found.
- A partial fill of an order that is still open is not recorded until the order ends.
- The history page of a run can be as old as the run (about 40 seconds). It can lack a record that has
  just appeared; that order is asked about again on a later run.
- The risk event is not sent as an alert. A fill settled from the history can send the
  abnormal-slippage alert inside the order's transaction, as a fill settled by the
  open-orders read already could.
- A rate limit is remembered per order, not per account (see above).
- The lock is held across the order's broker reads, at most 10 seconds together, plus the
  database work.
  A cancellation of that order waits that long. Two paths are longer: a fill whose
  slippage alert is sent (above), and the first time an
  unknown submission is looked at with no alarm yet raised, the engine raises it and sends
  the alert inside the same transaction (as it did before this change). Alert delivery is
  not covered by the 10 second limit: its web channels allow 10 seconds each and its e-mail
  channel has no time limit of its own. If that transaction then fails, the alert has gone
  out and will be sent again on the next attempt.
- A stop request (the task's soft time limit) is honoured between orders and during a
  broker read. If it lands inside the engine elsewhere, the engine's own error handling can
  absorb it; the 30 second budget and the task's hard limit then end the run.
- The demo paths read an order with no recorded account for any adapter, by exact broker
  order id; the "one connection only" rule above applies to the scheduled task.
- The demo worker fetches the same order history once per candidate order.
- An API process still running the previous version takes no lock; during a rolling
  deployment two owners are possible until every API process is replaced.
- A stop that lands while the Redis lock is being taken or released can leave the lock in
  place until its lease ends (90 seconds by default), with nobody reconciling meanwhile.
- The Redis connection has no socket timeout of its own; the run limit is what cuts off a
  Redis server that does not answer.
- Several accounts are supported by this task only. The strategy runner, position monitor
  and account sync still use one connection.
