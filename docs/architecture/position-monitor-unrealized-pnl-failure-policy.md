# PositionMonitor Unrealized P&L Failure Policy

## Current Behaviour

`PositionMonitor._check_daily_loss_with_unrealized(...)` includes unrealized P&L from broker positions in the daily-loss check. If broker position retrieval or unrealized P&L calculation fails, the current runtime behaviour is to:

- log `position_monitor.unrealized_pnl_error` at error level;
- include `error=str(exc)`, `exc_info=True`, and `unrealized_assumed=0.0` in the log payload;
- continue with `unrealized = 0.0`;
- return `False` when realised P&L alone does not breach the daily-loss threshold.

This document intentionally does not change that behaviour. It records the current fail-open policy so a later runtime PR can change it with explicit acceptance criteria.

## Why This Is Risky

Assuming zero unrealized P&L is observable but not safe enough for live automation. A broker snapshot failure during drawdown can hide open-position losses from the daily-loss calculation. If realised losses have not breached the limit yet, the monitor can continue running even though total realised plus unrealized loss would have required a halt.

The risk is highest when:

- broker `get_positions()` is unavailable or returns malformed payloads;
- market losses are concentrated in still-open positions;
- the account has little realised P&L for the day;
- automated exits or strategy runners remain able to produce broker orders after the failed snapshot.

## Recommended Future Behaviour

Future live-capable automation should fail closed when the daily-loss monitor cannot obtain a trustworthy unrealized P&L snapshot. The exact response needs an explicit runtime policy decision because each safer option has operational trade-offs.

## Proposed Fail-Closed Options

### Option A: Return Breach On Snapshot Failure

Treat unrealized P&L snapshot failure as `breach=True` from `_check_daily_loss_with_unrealized(...)`.

This is the narrowest code change and keeps the decision local to the daily-loss check. It may create false-positive trading halts when the broker API is flaky, but it prevents a missing snapshot from silently weakening the loss gate.

### Option B: Activate Kill Switch

Activate the kill switch when broker snapshot retrieval or unrealized P&L calculation fails during the daily-loss check.

This is safest for live automation because it blocks further automated trading until an operator reviews the system. It is also operationally disruptive: transient broker outages could halt trading and require manual recovery even when positions are healthy.

### Option C: Configurable Failure Policy

Introduce a policy setting such as:

```text
POSITION_MONITOR_UNREALIZED_PNL_FAILURE_POLICY =
  "assume_zero" | "block_trading" | "activate_kill_switch"
```

`assume_zero` would preserve the current behaviour and is intended only for migration and test environments, not live automation. `block_trading` could return `True` from `_check_daily_loss_with_unrealized(...)`, blocking automated trading without activating the global kill switch. `activate_kill_switch` would take the strongest safety action.

Config-driven behaviour needs careful defaults. A safer default should not be enabled for live automation until tests prove the selected app mode, live-trading flag, audit/log behaviour, and operator recovery semantics.

## Acceptance Criteria For A Future Runtime PR

A future runtime PR that changes this policy should meet all of these criteria:

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

## Tests Required Before Changing Behaviour

Before replacing the current `assume_zero` path, tests should explicitly cover:

- current fail-open behaviour, including the error log payload and `unrealized_assumed=0.0`;
- fail-closed behaviour for broker position snapshot failures;
- fail-closed behaviour for malformed position payloads, such as non-numeric `ppl`;
- realised-only breach behaviour with no broker snapshot failure;
- realised-only non-breach behaviour with no broker snapshot failure;
- behaviour when the kill switch is already active;
- behaviour when auto-trading is disabled;
- live-mode and non-live-mode defaults if policy is configurable;
- absence of broker placement, cancellation, or modification calls in failure-policy tests.

## Non-Goals

This policy design does not:

- change `app/services/position_monitor.py` runtime behaviour;
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
