# Live-Readiness Recency and Attestation Expiry Proposal

This proposal documents current live-readiness behavior and a future expiry
policy for approval before any tiny supervised live-money smoke test. It is a
docs-only proposal: it does not enable live trading, does not change readiness
gates, does not change safety or kill-switch behavior, does not change
broker/provider/execution behavior, and does not touch Kraken/crypto.

## Baseline Verified

- Repository inspected from `/Users/Ameer/Desktop/t212-cashguard-codex`.
- Baseline commit: `b3aa60358aa87b4f82436ec72f7b615efab781bb`.
- `HEAD` was detached and matched `origin/main`.
- Tracked files were clean before this proposal branch was created.
- Open PR queue was empty.
- GitHub CLI authentication was valid.

## Current Behavior Map

Live readiness is evaluated by
`apps/api/app/services/live_readiness.py` and exposed through:

- `GET /v1/settings/live-readiness`
- `POST /v1/settings/live-readiness`
- `GET /v1/operator/status`, nested under
  `trading212.live_readiness_status`

The current readiness checks are:

| Check key | Source | Current pass condition | Timestamp metadata | Current expiry |
| --- | --- | --- | --- | --- |
| `app_mode_live` | `settings.APP_MODE` | `APP_MODE == "live"` | None | Not applicable |
| `live_execution_enabled` | `settings.LIVE_TRADING_ENABLED` | Boolean flag is true | None | Not applicable |
| `live_broker_connected` | active `BrokerConnection` where `environment == "live"` | Active live connection exists | `BrokerConnection.last_test_at` displayed as `verified_at` | No direct expiry |
| `live_broker_test_recent` | active live `BrokerConnection` | `last_test_ok` is true and `last_test_at` is within 24 hours | `BrokerConnection.last_test_at` | 24 hours, enforced by backend |
| `telegram_ready` | `TelegramControlService.status_payload()` | Bot token, alert chat, webhook secret, and control are configured | None | No expiry |
| `demo_validated` | `AppSettings.extra["live_readiness"]` | `demo_validated_at` parses as a timestamp | `demo_validated_at` | No expiry |
| `broker_test_attested` | `AppSettings.extra["live_readiness"]` | `broker_test_verified_at` parses as a timestamp | `broker_test_verified_at` | No expiry after recording |
| `telegram_test_attested` | `AppSettings.extra["live_readiness"]` | `telegram_test_verified_at` parses as a timestamp | `telegram_test_verified_at` | No expiry |
| `kill_switch_tested` | `AppSettings.extra["live_readiness"]` | `kill_switch_tested_at` parses as a timestamp | `kill_switch_tested_at` | No expiry |
| `kill_switch_clear` | `AppSettings.kill_switch_active` | Global kill switch is false | None | Point-in-time state |
| `live_unlock_acknowledged` | `AppSettings.live_trading_unlocked` plus evidence in `extra` | Final unlock boolean is true | `live_unlock_acknowledged_at` | No expiry |

`eligible_for_unlock` is true when every check except
`live_unlock_acknowledged` passes. `ready_for_live` is true when
`eligible_for_unlock` is true and `AppSettings.live_trading_unlocked` is true.
`blockers` are derived from failing readiness checks.

## Confirmed Expiring Checks

- `live_broker_test_recent` expires when the active live broker connection's
  successful `last_test_at` is older than 24 hours.

## Confirmed Non-Expiring Attestations

These manual attestations have timestamps but currently pass forever once set,
unless manually overwritten or cleared by a lock flow:

- `demo_validated`
- `broker_test_attested`
- `telegram_test_attested`
- `kill_switch_tested`
- `live_unlock_acknowledged`

`broker_test_attested` has an important partial guard: recording it requires
`live_broker_test_recent` to be passing at record time. After it is recorded,
the attestation timestamp itself is not evaluated for freshness.

## Missing Timestamps And Metadata

- `telegram_ready` has no last-alert-delivered timestamp in readiness output.
- `kill_switch_clear` has no timestamp, because it represents current persisted
  state.
- `app_mode_live` and `live_execution_enabled` have no timestamp, because they
  represent current runtime configuration.
- The readiness API has no `expires_at`, `age_seconds`, `ttl_seconds`,
  `is_expired`, or status beyond `pass` and `fail`.
- Audit entries record readiness actions, but the readiness evidence blob does
  not store action version, TTL policy version, or source check IDs.

## Operator And Settings Visibility

The settings page renders each readiness check with `pass` or `fail`, detail
text, and `verified_at` where present. It has buttons to record demo review,
broker review, Telegram review, kill-switch drill, and lock or unlock live
trading.

The operator dashboard renders the same readiness checks under the Trading 212
summary. It does not display readiness expiry, TTL, or stale-attestation state
because the backend does not expose those concepts today.

Demo reconciliation freshness is visible separately on the operator dashboard.
The reconciliation card can show stale or warning states, but reconciliation
freshness is not currently part of the live-readiness gate.

## Current Test Coverage

Confirmed coverage includes:

- `apps/api/tests/integration/test_api.py` checks that
  `/v1/settings/live-readiness` returns `ready_for_live: false` by default.
- The same file checks that live unlock is rejected without prerequisites.
- The same file checks a happy path where live mode, live execution flag,
  recent broker test, Telegram configuration, all manual attestations, and
  unlock produce `ready_for_live: true`.
- `apps/api/tests/unit/test_operator_status_api.py` checks that operator status
  includes readiness status, degrades when readiness evaluation is unavailable,
  and blocks on global kill-switch state.
- `apps/web/tests/unit/operator-dashboard.test.tsx` checks that the operator
  dashboard renders individual readiness checks.
- Reconciliation worker and scheduler tests cover demo-only safety state and
  status reporting, but not live-readiness gating.

Missing coverage:

- No dedicated `apps/api/tests/unit/test_live_readiness.py` exists.
- No test asserts that old manual attestation timestamps fail.
- No test asserts explicit future `expires_at`, `ttl_seconds`, or
  `expired`/`stale` API fields.
- No frontend test covers expired readiness visibility, because the API does
  not expose it.

## Risk Assessment

Level A, docs/tests only:

- Document current readiness behavior.
- Add tests that lock in current behavior.
- Add future desired-behavior tests as skipped or xfailed, with no production
  behavior change.

Level B, additive read-only visibility:

- Add read-only freshness metadata to readiness API responses without changing
  `pass`/`fail`, `eligible_for_unlock`, or `ready_for_live`.
- Display freshness metadata in operator/settings UI without changing controls.

Level C, readiness or safety semantics:

- Make manual attestations expire.
- Change `eligible_for_unlock` or `ready_for_live` calculations.
- Change unlock/relock behavior.
- Add reconciliation freshness as a mandatory readiness blocker.
- Change kill-switch test recording or kill-switch enforcement.
- Change broker/provider/execution behavior.

Blocked until explicit approval:

- Schema or migration decisions for storing expiry metadata outside the
  existing `AppSettings.extra["live_readiness"]` blob.
- Product decision on whether demo reconciliation evidence is a mandatory live
  readiness prerequisite or a separate operator prerequisite.
- Product decision on whether final human approval is same-session only.

## Proposed TTL Policy

Conservative target policy for future approval:

| Check | Proposed TTL | Configurable? | Expired behavior |
| --- | --- | --- | --- |
| Active live broker connection test | 24 hours | Yes, with safe default | Check fails and blocks unlock/live readiness |
| Broker-test manual attestation | 24 hours, tied to the same broker connection test ID or timestamp | Yes, with safe default | Check fails and requires re-attestation after a fresh broker test |
| Telegram supervision readiness | Current config remains point-in-time; delivered test alert gets 24 hours | Yes, with safe default | Delivered-test attestation fails; config check still reflects current config |
| Telegram test attestation | 24 hours | Yes, with safe default | Check fails and requires new alert delivery plus review |
| Kill-switch drill before live smoke test | 24 hours | Yes, with safe default | Check fails and requires a new drill |
| Kill-switch drill for ordinary demo readiness | 7 days, if a separate demo-readiness concept is introduced | Yes | Warn or fail depending on the future gate |
| Demo validation / reconciliation evidence | 24 hours or one clean demo cycle | Yes | Check fails until a clean current cycle is recorded |
| Cash/account validation | 24 hours | Yes | Check fails until account state is revalidated |
| Final human live-smoke approval | Same session only | No, or tightly bounded | Unlock expires or is rejected outside session window |

TTL configuration should be server-side only, with defaults that fail closed.
The first implementation should avoid user-editable UI controls for TTLs.

## API And UI Shape

Future read-only API additions should be explicit and machine-testable:

- `verified_at`
- `expires_at`
- `ttl_seconds`
- `age_seconds`
- `is_expired`
- `freshness_status`: `fresh`, `stale`, `expired`, `unknown`, or
  `not_applicable`
- `freshness_detail`
- `policy_version`

The UI should display expired checks as blocking, not as successful historical
events. Operator and settings pages should show the recorded time, expiry time,
and a short reason. Stale but non-blocking states should be visually distinct
from blocking expired states.

## Audit Events

Future implementation should write audit events for:

- Attestation created.
- Attestation refreshed.
- Attestation rejected because prerequisite evidence is missing or expired.
- Previously valid attestation evaluated as expired.
- Live unlock attempted and rejected because of expired evidence.
- Live unlock accepted, including TTL policy version and evidence timestamps.
- Live lock or automatic expiry of live unlock, if implemented.

Audit payloads should include check keys, timestamps, TTL policy version, actor,
and non-secret evidence references. They must not include broker credentials,
tokens, account secrets, or raw provider responses.

## Clock And Timezone Policy

Readiness expiry should use timezone-aware UTC timestamps only. Naive,
unparseable, future-skewed, or timezone-uncertain timestamps should fail closed
for gating and show `freshness_status: unknown` or `expired` with a clear
detail. Tests should simulate time through an injectable clock or frozen-time
helper, not by sleeping.

## Implementation Sequence

PR 1 - docs/design proposal only:

- Add this proposal.
- Optionally link it from docs index.
- No runtime or test behavior changes.

PR 2 - tests documenting current and desired behavior:

- Add backend tests for current broker recency behavior.
- Add tests proving manual attestations currently do not expire.
- Add skipped or xfailed tests for desired expiry behavior.
- Add UI tests for future expired metadata only after API shape is approved.

PR 3 - backend expiry policy implementation:

- Add a clock abstraction or injectable `now`.
- Add TTL constants or server-side config with safe defaults.
- Add expiry evaluation to `LiveReadinessService`.
- Add read-only freshness fields to schemas.
- Decide whether existing `AppSettings.extra["live_readiness"]` is sufficient
  or whether a migration to structured attestation rows is required.
- Preserve fail-closed behavior for missing or malformed timestamps.

PR 4 - operator/settings UI visibility:

- Update frontend types for freshness metadata.
- Show expiry and stale state in settings and operator pages.
- Add frontend unit and E2E coverage.
- Keep UI read-only on operator page.

PR 5 - live smoke-test runbook update:

- Update runbook only after implementation has merged and CI is green.
- Require fresh evidence on the day of any approved live-money smoke test.
- Keep the explicit disable and rollback steps.

## Rollout Plan

1. Land docs-only proposal.
2. Land tests that document current behavior and future expected behavior.
3. Implement backend read-only metadata first, if approved, without changing
   gate semantics.
4. Add UI visibility for metadata.
5. In a separate approved Level C PR, make expiry affect live-readiness gates.
6. Re-run full backend, frontend, E2E, security, secrets, and CodeQL checks.
7. Conduct manual QA in mock and demo modes before any live-mode review.

## Failure Modes

- Existing old attestations may suddenly block live unlock after Level C expiry
  enforcement. This is intentional but requires operator communication.
- Clock skew or malformed timestamps could fail readiness. This should be
  treated as safer than accepting uncertain evidence.
- An expired final unlock could surprise an operator if the UI does not explain
  it clearly.
- Keeping expiry metadata only in JSON may make querying and auditing harder.
  A structured table may be preferable if expiry becomes operationally central.

## Stop Conditions

Stop and do not enable live trading if:

- Any readiness check is expired, unknown, or failed.
- Any required audit event is missing.
- Global or venue kill switch is active.
- Demo reconciliation status is unsafe or stale at review time.
- Git SHA, deployed environment, or operator dashboard state does not match the
  approved runbook.
- Any CI/security/secrets/code-scanning check fails.
- Human supervision is not present for an approved live-money smoke test.

## Safety And Scope Statement

This proposal does not enable live trading. It does not place real orders, add
order controls, alter broker/provider/execution behavior, weaken safety gates,
change kill-switch enforcement, change authentication, modify dependencies, or
touch Kraken/crypto. All Level C behavior changes require separate approval and
separate PRs.
