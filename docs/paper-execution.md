# Paper Execution Safety

T-OPS-015 introduces a local paper-only execution path at `POST /v1/orders/paper`.
T-OPS-016 adds read-only history and audit review for that local paper path.

Safety convention:

- Paper execution is available only when `APP_MODE=mock`.
- The endpoint never depends on broker adapters and does not call Trading 212 or Kraken order placement.
- `paper_only` must be `true`; `live=true` and `paper_only=false` payloads are rejected by schema validation.
- Supported venues are `paper` and `mock` only.
- The global kill switch remains authoritative. If it is active, paper execution is blocked and audited as `paper_signal_rejected` plus `paper_risk_check_result`.
- Auto-trading does not need to be enabled for this manual paper endpoint because no automation, scheduler, live unlock, or broker execution is involved.

The simulated lifecycle is:

`paper signal accepted -> risk check result -> local order created -> paper fill simulated -> local paper position snapshot updated -> audit log entries visible in operator status`.

## Read-Only History

Paper execution history is available from:

- `GET /v1/orders/paper`
- `GET /v1/orders/paper/{order_id}/audit`

Both endpoints require auth and are read-only. They query local database rows only:

- local paper orders with `execution_environment=paper_mock`
- local paper audit events with `paper_` actions
- blocked paper attempts from `paper_signal_rejected` audit rows

`GET /v1/orders/paper` defaults to a limit of 25 and caps requests at 100. Results are newest first and include explicit safety fields: `paper_only=true`, `live_order_sent=false`, and `no_broker_order_sent=true`.

`GET /v1/orders/paper/{order_id}/audit` returns audit entries linked to that local paper order where available. Metadata is sanitized so broker credentials, API keys, tokens, passwords, and secrets are not exposed.

Viewing history does not call Trading212, Kraken, or any broker order endpoint. It does not create, submit, cancel, execute, deposit, withdraw, transfer, margin, or leverage anything.

## Operator Manual QA

Use the existing manual QA flow:

```bash
make operator-manual
make operator-manual-check
make operator-manual-stop
```

Open the operator page and inspect the **Paper Execution History** panel. In an empty local database it shows:

`Paper execution history will appear here after mock paper orders are created. No broker order is sent.`

After mock paper orders are created in `APP_MODE=mock`, the panel shows recent paper executions, risk status, source, strategy, venue, simulated fill details, audit counts, and the explicit paper-only/no-broker safety wording.

Manual QA keeps live trading disabled and the kill switch active by default. The history panel is visibility-only; opening it sends GET requests only and sends no broker orders.
