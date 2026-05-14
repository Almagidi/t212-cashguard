# Trading 212 DEMO Pending Order Follow-Up 2 QA

## Date

2026-05-14 / 2026-05-15 session

## Branch

feature/t212-demo-pending-order-follow-up-2

## Purpose

Document a second read-only follow-up check for the Trading 212 DEMO order created during the controlled multi-order placement smoke.

## Related Order

- Ticker: AAPL_US_EQ
- Quantity: 0.01
- Local order ID: 8975cc0d-8d1b-407c-81a4-538864d23362
- Broker order ID: 48950280036
- Original local status: accepted
- Execution environment: demo
- is_dry_run: false

## Read-Only Pending Order Check

A read-only pending-order check was run with:

- APP_MODE=demo
- T212_ENVIRONMENT=demo
- LIVE_TRADING_ENABLED=false
- broker_environment=demo

Result:

- pending_count: 1
- matched_pending_order: true
- broker_order_id: 48950280036
- ticker: AAPL_US_EQ
- quantity: 0.01
- filledQuantity: 0
- broker status: NEW
- side: BUY
- type: MARKET
- initiatedFrom: API
- read_only: true
- live_trading_enabled: false

## Interpretation

The broker order is still pending in Trading 212 DEMO with status NEW and filledQuantity 0.

No additional demo orders should be placed while this broker order remains open.

The correct next action remains read-only only: check pending orders again later, and only rerun multi-order reconciliation smoke if the order no longer appears in pending orders.

## Safety Confirmation

- No live trading was enabled.
- No live endpoint was used.
- The check was read-only.
- No broker write method was called.
- Reconciliation was not rerun because the order remained pending.

## Next Follow-Up

1. Check pending orders again later.
2. If the order is no longer pending, rerun the multi-order reconciliation smoke.
3. Capture whether the order reconciles to filled, cancelled, expired, or another broker terminal state.
