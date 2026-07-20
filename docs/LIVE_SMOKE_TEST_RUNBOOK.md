# Trading 212 Live-Money Smoke Test Runbook — DO NOT RUN YET

> **STATUS: NOT APPROVED FOR EXECUTION.**
>
> This runbook documents a *future*, tiny, supervised Trading 212 live-money
> smoke test. It exists so the procedure is agreed, reviewed, and rehearsed
> **before** anyone is in a position to improvise with real money.
>
> As of this writing the project is in Trading 212 DEMO / paper-mode
> hardening. Live trading is disabled and not live-ready:
>
> - `LIVE_TRADING_ENABLED=false` (default, must stay false until approved)
> - `APP_MODE=mock` is the safe default; `demo` is the current working mode
> - `T212_ENVIRONMENT=demo`
> - `CASH_ONLY_MODE=true`
> - `AppSettings.live_trading_unlocked=false`
>
> Nothing in this document is approval to change any of those values.
> Executing this runbook requires the separately approved Level C
> enablement work plus an explicit, recorded owner sign-off.

---

## 1. Purpose and scope

Prove, with the smallest possible real-money exposure, that:

1. an order created by CashGuard reaches the live Trading 212 account,
2. the local order record and the broker's record reconcile exactly,
3. the kill switch and live-disable procedures work under real conditions.

Scope is **one order, one instrument, one session, fully supervised**.
This is not a performance test, not a strategy test, and not a soak test.

## 1a. Evidence already in place (demo/paper)

The following is already proven in mock/paper mode and does **not** require live
money. It is the baseline the live smoke test builds on, not a substitute for it:

- `apps/api/tests/integration/test_paper_dry_run_validation.py` proves the
  automated paper-trade dry-run chain end to end at the service layer: a
  dry-run signal flows into a local paper-only fill with no broker adapter
  constructed, the kill switch blocks both the paper path and the automated
  `StrategyRunner`, the risk/mode/oversell gates fail closed, and the demo
  reconciler refuses paper orders before any broker read.
- What that suite does **not** cover, and what this runbook exists to prove with
  a tiny supervised order, is listed below. Do not treat paper validation as
  evidence that any of section 2's live prerequisites are met.

Remaining gaps before an automated *paper* trade could even run unattended
(still no live money): a real scheduler/Celery-beat dry run wired to the paper
path, and an end-to-end operator-dashboard read of a scheduler-produced paper
fill. Remaining gaps before this live smoke test are in section 2.

## 2. Prerequisites (all must be true before scheduling the test)

Do not schedule a test date until every item below is true:

- [ ] All Level C live-enablement PRs (order path, readiness enforcement,
      credentials) have merged through the high-risk verification process,
      each with its own review and green CI.
- [ ] `GET /v1/settings/live-readiness` reports every check `pass`,
      including:
  - `app_mode_live` — server intentionally started in live mode
  - `live_execution_enabled` — `LIVE_TRADING_ENABLED=true` set on purpose
  - `live_broker_connected` — an active live Trading 212 connection exists
  - `live_broker_test_recent` — successful live connection test in the
    last 24 hours (recency is enforced by the backend)
  - `telegram_ready`, `telegram_test_attested` — supervision alerts proven,
    with the alert review recorded in the last 24 hours
  - `demo_validated` — demo soak reviewed and recorded in the last 24 hours
  - `broker_test_attested` — live broker test manually reviewed in the last
    24 hours
  - `kill_switch_tested` — a kill-switch drill recorded in the last 24 hours
    (section 4)
  - `kill_switch_clear` — kill switch currently inactive
  - `live_unlock_acknowledged` — explicit admin unlock recorded in the last
    24 hours

  All manual attestations and the final unlock acknowledgement expire 24
  hours after recording. The backend enforces this fail-closed: missing,
  malformed, or stale timestamps block readiness, so evidence must be
  re-recorded on the day of the test.
- [ ] The demo reconciliation worker and scheduler have run cleanly for at
      least one full week: `GET /v1/broker/trading212/reconciliation/status`
      and `.../reconciliation/scheduler/status` show no unexplained
      `missing`, `failed`, or rate-limit backoff at review time.
- [ ] The operator dashboard (`/app/operator`) shows `Overall ok` with no
      `why_blocked` entries other than the intentional live-unlock state.
- [ ] A second human (supervisor) has agreed to be present for the entire
      test window.
- [ ] The live account is funded with **only** the amount the owner is
      prepared to lose in this test (see section 7).

## 3. Pre-flight checks (day of test, before enabling anything)

Run in order. Any failure is a stop condition (section 12).

1. Verify baseline health:
   - `GET /v1/health/live` → `status: ok`
   - `GET /v1/health/deps` → database ok, redis ok, broker reachable
2. Verify the readiness checklist is still fully green:
   - `GET /v1/settings/live-readiness` → `eligible_for_unlock: true`
3. Verify the operator dashboard end to end:
   - `/app/operator` loads, `Overall` is not `blocked`, protective-stops
     card shows `ok`, CashGuard card shows expected cash figures.
4. Verify audit logging is recording:
   - `GET /v1/audit` shows the readiness actions you just performed.
5. Record the exact git SHA deployed, the time, and the two humans present
   in the test log (any shared note is fine; it will be attached to the
   post-test review).

## 4. Kill-switch checks

Perform a fresh drill in the live-configured environment **before** the
first live order (do not reuse an old drill record):

1. Activate: `POST /v1/emergency/kill-switch` (or Emergency page → Kill
   Switch → Execute).
2. Confirm effects:
   - `AppSettings.kill_switch_active = true`, `auto_trading_enabled = false`
   - operator dashboard shows `blocked` with `global_kill_switch_active`
   - a `kill_switch_on` risk event and audit entry exist
3. Deactivate through the documented recovery path, confirm the dashboard
   returns to the expected state.
4. Record the drill via the live-readiness action `record_kill_switch_test`.

Remember what the kill switch does **not** do: it does not cancel pending
orders (`POST /v1/emergency/cancel-all`) and does not close positions
(`POST /v1/emergency/flatten-all`). Know all three before proceeding.

## 5. Credential checks

1. Confirm the live Trading 212 API key was created for this purpose, is
   stored only through the Broker page (encrypted at rest via
   `MASTER_KEY`), and appears in no shell history, env file, or log.
2. Run the broker connection test from the Broker page; it must pass and
   show the expected live account id and currency.
3. Confirm `credential_source` on the operator dashboard shows the real
   credential store (not mock).
4. Confirm the key's permissions are the minimum Trading 212 offers for
   order placement plus read, nothing more.

## 6. Account / cash / reconciliation checks

1. `GET /v1/account/summary` and `GET /v1/account/cash-guard` figures match
   the Trading 212 app to the cent.
2. Reserved/available split is sane and `cash_only_mode` reflects the
   intended configuration.
3. Reconciliation is healthy *right now*: worker/scheduler status endpoints
   show a recent successful run, zero unexplained `missing`/`failed`.
4. There are no open positions and no pending orders in either the local DB
   or the Trading 212 app. The test starts from a flat account.

## 7. One-order maximum constraints

The entire test is bounded by these hard limits:

- **Exactly one order.** If it fails, is rejected, or anything is unclear —
  the test is over. Do not retry. Diagnose offline in demo mode.
- Instrument: one large-cap, high-liquidity equity chosen in advance.
- Size: the minimum order size Trading 212 accepts for that instrument,
  targeting the smallest practical notional (single-digit currency units
  if the instrument allows fractional shares).
- Order type: the simplest supported type (market or day limit), chosen in
  advance and written in the test log before submission.
- No leverage, no CFD, no short side. Buy only.
- Risk settings (`max_daily_loss_pct`, position caps) remain at their
  conservative values; do not raise any limit "for the test".

## 8. Immediate post-order reconciliation

Within minutes of the order reaching a terminal state:

1. Confirm the local order record and broker state agree: status, fill
   quantity, fill price, fees.
2. Run/inspect a reconciliation pass and confirm the order is matched
   (`matched: true`, no `missing` outcome) via the reconciliation status
   endpoints.
3. Confirm `GET /v1/account/cash-guard` cash movement matches the fill
   exactly against the pre-order snapshot.
4. Screenshot/record both the Trading 212 app view and the CashGuard order
   view in the test log.

Any discrepancy — even one that "looks like rounding" — is a stop
condition and must be investigated before live trading is ever re-enabled.

## 9. Immediate live-disable procedure

Run this **unconditionally** at the end of the test, success or failure:

1. Lock live trading: live-readiness action `lock_live`
   (`POST /v1/settings/live-readiness`) so
   `AppSettings.live_trading_unlocked = false`.
2. Set `LIVE_TRADING_ENABLED=false` in the server environment and restart.
3. Return `APP_MODE` and `T212_ENVIRONMENT` to their demo values.
4. Verify: `GET /v1/settings/live-readiness` shows live locked;
   the operator dashboard shows `Live disabled`.
5. Record the disable in the test log with a timestamp.

## 10. Emergency rollback procedure

If anything goes wrong mid-test, in this order:

1. `POST /v1/emergency/kill-switch` — halt all automation immediately.
2. `POST /v1/emergency/cancel-all` — cancel any pending order at broker.
3. If a position was opened and must not be held:
   `POST /v1/emergency/flatten-all`, otherwise decide explicitly (and
   record) that the position is being kept.
4. Execute the full live-disable procedure (section 9).
5. Verify in the Trading 212 app directly — not only through CashGuard —
   that there are no pending orders and positions match expectations.
6. If the API is unreachable, use the Trading 212 app itself to cancel or
   close, then rotate the API key (Broker page → Disconnect → new key)
   before any further use.
7. Preserve everything: do not delete or "clean up" any local order rows,
   audit entries, or logs from the failed window.

## 11. Audit-log review procedure

After the test (same day):

1. Pull `GET /v1/audit` for the full test window.
2. Confirm every expected event is present and correctly attributed:
   readiness actions, unlock, order lifecycle, reconciliation entries,
   kill-switch drill, lock/disable.
3. Confirm no unexpected actor performed any action during the window.
4. Confirm no sensitive values (API keys, tokens) appear in any payload.
5. Attach the review outcome to the test log; both humans sign off.

## 12. Stop conditions

Abort immediately — kill switch first, then rollback (section 10) — if any
of these occur:

- any readiness check regresses from `pass` at any point
- the broker connection test fails or flaps
- account/cash figures disagree with the Trading 212 app before the order
- the order is rejected, errors, or ends in any unexpected state
- reconciliation reports the order `missing`, unmatched, or mismatched
- any cash discrepancy after the fill
- Telegram supervision alerts do not arrive
- either human wants to stop, for any reason, no justification needed
- anything at all feels unclear — unclear means stop

A stopped test is a successful outcome for this runbook: it means the
safety net worked. Diagnose in demo mode; never debug against live money.

## 13. After the test

- Complete the audit review (section 11) and file the test log.
- Live trading stays disabled (section 9) until a separate, explicit
  decision is made about what comes next.
- Feed every surprise — however small — back into the demo/paper test
  suite as a regression test before considering any further live activity.

---

*This runbook is Level A documentation. It changes no runtime behavior,
enables nothing, and must not be cited as approval for any Level C work.*
