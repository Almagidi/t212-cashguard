# Cash reservation

CashGuard is cash-only: a buy order may be sent only if the account has the cash for it.
The broker's free-cash figure alone cannot guarantee that, because it is read before the
order is created. This page describes what is counted on top of that figure, and what is
not.

## The defect this closes

Before this change each buy was compared, alone, with the free cash that had been read for
it. Two requests that read 1,000 at the same moment could each be admitted for 600, and
the scheduled strategy runner, which reads the account once per run, could spend the same
figure once per signal. Both are reproduced by the tests named at the end.

## The rule

A real buy order (not a dry run) is admitted only if

    its estimated cost + the estimated cost of the account's other counted buy orders
        <= the free cash that was read for it

An order is **counted** while a cash figure may fail to show it:

| Order | Counted |
|---|---|
| Open: `pending_intent`, `submitted`, `submission_unknown`, `accepted`, `partially_filled` | Always, at its full estimated cost |
| Ended: `filled`, `cancelled`, `rejected`, `error` | Until the cash figure was read more than 60 seconds after the last time recorded on the order |

"The account's" means the same environment, the same venue and the same broker account.
An order recorded without an account counts against every account, an order recorded
without an environment counts in every environment, and a caller without an account
identity counts every order. Sell orders and dry-run orders reserve nothing and
are never refused by this rule.

The check is made in `ExecutionEngine.create_order_intent`
(`app/execution/cash_reservation.py`), the one place every broker order is created (paper
trading has its own engine and is not covered). Nothing new is stored: the reservation of an order is its own row, so it survives a restart, and
reconciliation ends it by ending the order.

## One admission at a time

On PostgreSQL the check takes a transaction-level advisory lock for the environment and
venue before it counts.

- **Admitted:** the lock stays with the caller's transaction until it commits or rolls
  back, which is also when the new order row becomes visible, so the next admission always
  counts the previous one. A lost connection releases the lock and leaves no order.
- **Refused:** the lock is taken inside a savepoint that is rolled back, so it is given
  back at once, although the caller's transaction goes on.
- **Kept waiting:** the lock is asked for repeatedly, never waited on. An admission that
  cannot have its turn within 10 seconds is refused, so a holder that hangs cannot hang
  every other order.
- **Asked twice:** once it has its turn, a request looks again for an order that already
  stands for it. A retry sent while the first request was still being admitted gets the
  first order back and creates nothing.

The strategy runner and the rebalancer end their transaction when an order they created
is blocked by the safety policy or fails before it is sent, so the turn is not kept for
the rest of the run. On any other error after admission the turn stays with their
transaction until it ends; other admissions are then refused after 10 seconds, not hung.

## What the caller must hand over

`available_cash` and `cash_read_at`, the time just before the figure was requested from the
broker. A figure without a time is treated as 15 minutes old. A real buy that comes with a
cash figure but no price to estimate its cost from is refused.

The manual order route, the strategy runner and the portfolio rebalancer do this. The
rebalancer passes the broker's figure, not its own running estimate, because the estimate
already assumes its earlier orders filled and they would otherwise be counted twice.

## A refusal

The order is not created and nothing is sent. `CashReservationRefused` is a
`RiskViolation` with event type `cash_reservation_block`:

- Manual order: HTTP 422 with the reason. The request's transaction is rolled back, as for
  every refused manual order, so no risk event is kept for it.
- Strategy runner and rebalancer: the signal is marked rejected with the reason, and a
  `cash_reservation_block` risk event records the figures.

## Known limits

- **It refuses more than strictly necessary.** A working order is counted for as long as
  it is open, even after the broker's own figure has started to exclude its cash. A resting
  limit buy therefore reduces what can be admitted by its cost twice. This is deliberate:
  how and when the broker's figure reflects an open order has not been verified against
  the broker.
- **A rebalance does not spend sale proceeds it has not seen.** Buys that depend on the
  proceeds of sells in the same rebalance are refused. The period is then recorded as
  `cash_blocked`, not as rebalanced, so the strategy stays due and a later run, reading the
  cash again, finishes the job. A live rebalance is not planned again while a real order of
  the strategy is at the broker (sent and not yet ended) or changed within the last five
  minutes: the broker's positions would not show it, and the plan would repeat it. Such a
  run is skipped with reason `orders_in_flight`. An intent that was never sent does not
  hold a rebalance up beyond those five minutes, because nothing sends it later. After
  three cash-blocked runs on one decision date the period is recorded and the strategy
  waits for the next one; the count starts again if the decision date changes first.
- **Cost is an estimate.** It is quantity times the price the caller estimated. A manual
  market buy is still estimated at a placeholder price (finding N-02), so its reservation
  is as wrong as that estimate. The strategy runner sends limit orders 0.1% through its
  estimate, so such an order can cost that much more than was reserved for it.
- **No currency conversion.** Cost and cash are compared as numbers, as the cash check
  always did. Instruments not priced in the account currency are not handled.
- **A real buy created without a cash figure is not checked.** It still counts against
  later ones if it has a price to estimate its cost from; without one it does not. The
  three callers that can buy always give a figure and a price, and a test fails if a new
  caller appears; the engine itself does not insist.
- **An order the broker neither reports nor cancels stays open here**, and so keeps its
  cost reserved, blocks its ticker's duplicate check and holds its strategy's rebalance,
  until it is resolved. There is no operator action for that yet.
- **One pool per connection, not per funded account.** The account identity includes the
  user, so two users connected to the same broker account do not count each other's orders.
- **Orders already marked `error` with a broker order id** before this change (a failed
  cancellation used to do that) are not counted.
- **Paper trading** has its own engine and ledger and is not covered here.
- **SQLite** (unit tests) has no advisory lock; the rule is applied without one.

## Two changes outside the reservation that it depends on

- **A failed cancellation no longer finishes the order.** When the broker does not confirm
  a cancellation the order used to become `error`, which is final: reconciliation stopped
  reading it and its cash would have been released while it could still fill. It now keeps
  its status, records a `cancel_failed` event, and the caller is told the cancellation
  failed, as before.
- **A real buy that may not be sent is closed.** When the safety policy blocks a
  submission (kill switch, readiness), the buy intent used to stay `pending_intent` for
  ever if the caller kept its transaction. It is now recorded as `rejected` with a
  `submission_blocked` event. Sell intents are left as they were, because some callers
  persist one and send it on a later attempt.

## Tests

- `tests/integration/test_cash_reservation_manual_orders.py`: the manual order route with
  concurrent requests (fails on the code before this change).
- `tests/integration/test_cash_reservation.py`: the engine across sessions, crashes,
  outcomes and reconciliation.
- `tests/integration/test_cash_reservation_callers.py`: the runner, the rebalancer and an
  inventory of every caller.
- `tests/unit/test_cash_reservation.py`: which rows count.
