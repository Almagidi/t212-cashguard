# PositionMonitor Unrealized P&L Failure Policy

## Runtime Policy

`PositionMonitor._check_daily_loss_with_unrealized(...)` includes unrealized P&L from broker positions in the daily-loss check. It returns a private typed outcome so `run()` can distinguish a normal daily-loss breach from an unrealized-P&L policy halt without relying on hidden instance state. If broker position retrieval or unrealized P&L calculation fails, the current runtime behaviour is to:

- read `POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY`;
- default to `block_trading`;
- fail closed for unrecognised policy values.

Accepted values are:

```text
assume_zero | block_trading | activate_kill_switch
```

`assume_zero` preserves the legacy behaviour: log `position_monitor.unrealized_pnl_error` with `error=str(exc)`, `exc_info=True`, `unrealized_assumed=0.0`, and `failure_policy="assume_zero"`, then continue with `unrealized = 0.0`. This is available only for migration and test compatibility and is not suitable for live automation.

`block_trading` is the default. It logs `position_monitor.unrealized_pnl_error` with `failure_policy="block_trading"` and `fail_closed=True`, logs `position_monitor.realized_pnl_skipped` with `reason="unrealized_pnl_failure_policy_halt"`, then returns the policy block outcome. The caller halts monitoring without activating the global kill switch and returns `halted="unrealized_pnl_failure_block_trading"` with `failure_policy="block_trading"` and `fail_closed=True`.

`activate_kill_switch` logs `position_monitor.unrealized_pnl_error` with `failure_policy="activate_kill_switch"`, `fail_closed=True`, and `kill_switch_activated=True`; calls the existing `activate_kill_switch(...)` helper; calls `alert_kill_switch_activated(...)`; logs `position_monitor.realized_pnl_skipped`; and returns the policy kill-switch outcome. The caller returns `halted="unrealized_pnl_failure_kill_switch"` with policy metadata and does not activate the kill switch a second time.

If the configured policy is invalid, PositionMonitor logs `position_monitor.unrealized_pnl_failure_policy_invalid` with `configured_policy`, `fallback_policy="block_trading"`, `fail_closed=True`, `error=str(exc)`, and `exc_info=True`, logs `position_monitor.realized_pnl_skipped`, and fails closed with the `block_trading` behaviour. The setting is also typed as a `Literal` for startup validation, while the runtime branch remains defensive for tests and later mutation.

## Why This Is Risky

Assuming zero unrealized P&L is observable but not safe enough for live automation. A broker snapshot failure during drawdown can hide open-position losses from the daily-loss calculation. If realised losses have not breached the limit yet, the monitor can continue running even though total realised plus unrealized loss would have required a halt.

The risk is highest when:

- broker `get_positions()` is unavailable or returns malformed payloads;
- market losses are concentrated in still-open positions;
- the account has little realised P&L for the day;
- automated exits or strategy runners remain able to produce broker orders after the failed snapshot.

## Acceptance Criteria

- no provider migration mixed into the policy change;
- tests for `broker.get_positions()` failure;
- tests for malformed position payloads during unrealized P&L calculation;
- tests for realised P&L breached and not breached cases;
- tests for app mode and live trading settings if behaviour differs by environment;
- audit event or log assertions for the selected failure path;
- no direct broker writes in tests;
- clear summary/result semantics for caller behaviour;
- updated safety and architecture docs;
- CI checks pass.

## Non-Goals

This policy does not:

- migrate `PositionMonitor._get_broker`;
- change broker provider or factory design;
- change route schemas;
- change frontend code;
- expand live trading;
- add another broker;
- change credential storage or decryption;
- place, cancel, or modify broker orders;
- suppress dependency audits;
- refactor broad architecture.
