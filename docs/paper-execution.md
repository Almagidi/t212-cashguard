# Paper Execution Safety

T-OPS-015 introduces a local paper-only execution path at `POST /v1/orders/paper`.

Safety convention:

- Paper execution is available only when `APP_MODE=mock`.
- The endpoint never depends on broker adapters and does not call Trading 212 or Kraken order placement.
- `paper_only` must be `true`; `live=true` and `paper_only=false` payloads are rejected by schema validation.
- Supported venues are `paper` and `mock` only.
- The global kill switch remains authoritative. If it is active, paper execution is blocked and audited as `paper_signal_rejected` plus `paper_risk_check_result`.
- Auto-trading does not need to be enabled for this manual paper endpoint because no automation, scheduler, live unlock, or broker execution is involved.

The simulated lifecycle is:

`paper signal accepted -> risk check result -> local order created -> paper fill simulated -> local paper position snapshot updated -> audit log entries visible in operator status`.
