# Trading 212 DEMO Controlled Multi-Order Real Smoke QA

## Date

2026-05-14 / 2026-05-15 session

## Branch

feature/controlled-demo-multi-order-real-qa

## Purpose

Capture real Trading 212 DEMO evidence from the controlled multi-order placement smoke and follow-up read-only reconciliation checks.

## Placement Smoke Result

Command:

T212_DEMO_MULTI_ORDER_CONFIRM=PLACE_MULTI_DEMO_ORDERS
T212_DEMO_MULTI_ORDER_PLAN="AAPL_US_EQ:0.01,MSFT_US_EQ:0.01"
make t212-demo-controlled-multi-order

Result:

- Requested orders: 2
- Attempted: 2
- Accepted: 1
- Failed: 1
- Accepted ticker: AAPL_US_EQ
- Accepted quantity: 0.01
- Local order ID: 8975cc0d-8d1b-407c-81a4-538864d23362
- Broker order ID: 48950280036
- Local status after placement: accepted
- Execution environment: demo
- is_dry_run: false
- Failed ticker: MSFT_US_EQ
- Failure reason: broker_rate_limited / HTTP 429

Safety markers confirmed:

- live_trading_enabled=false
- broker_environment=demo
- no_live_endpoint=true
- operator_confirmed=true
- bounded_order_count=true

## Reconciliation Smoke Results

Immediate reconciliation smoke:

- candidates_found: 1
- attempted: 1
- succeeded: 0
- missing: 1
- rate_limited: 0
- failed: 0
- broker_write_calls: []
- no_broker_order_sent: true
- read_only_broker_calls: true
- local order stayed accepted

Second reconciliation smoke after 60 seconds:

- candidates_found: 1
- attempted: 1
- succeeded: 0
- missing: 1
- rate_limited: 0
- failed: 0
- broker_write_calls: []
- no_broker_order_sent: true
- read_only_broker_calls: true

## Pending Broker Order Check

Read-only pending-order check result:

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
- broker_environment: demo
- live_trading_enabled: false

## Interpretation

The controlled multi-order placement smoke safely placed one DEMO broker-backed order and stopped after the second order hit Trading 212 rate limiting.

The accepted AAPL order was not reconciled to filled because Trading 212 still reported it as pending with status NEW and filledQuantity 0.

This is a safe state. Reconciliation correctly preserved the local order status as accepted and did not mark the order filled without broker-history evidence.

## Safety Confirmation

- No live trading was enabled.
- No live Trading 212 endpoint was used.
- The placement smoke was terminal-only and explicitly confirmed.
- The order count was bounded.
- The reconciliation smoke was read-only.
- No broker write calls occurred during reconciliation.
- The scheduler remained disabled during placement and reconciliation.

## Follow-up

Do not place additional orders until this pending order either fills, cancels, or expires.

The next check should be read-only only:

1. Check pending orders again.
2. If no longer pending, rerun the multi-order reconciliation smoke.
3. Capture whether the order reconciles to filled, cancelled, or expired from Trading 212 history.
