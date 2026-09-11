# Safety Remediation Baseline

**Observed baseline:** `origin/main` at `6a51dab100aa162e283682a1a9e4434ede600dc8` on 2026-09-11.

**Execution boundary:** All local validation used `APP_MODE=mock`, `MARKET_DATA_PROVIDER=mock`, disabled broker-writing flags, empty outbound-integration variables, fake test-only application/database values, and explicit broker/network tripwires. No broker credential was read and no real or demo broker request was allowed.

## Release gate summary

| Gate | Result | Evidence |
|---|---|---|
| Repository merge governance | **Fail** | Active `Guard` ruleset `17091691` had no ref include condition; `gh ruleset check main` reported zero applicable rules. Classic branch protection was absent. |
| Backend behavior | Pass | Python 3.12 + PostgreSQL 16 + Redis 7: `2067 passed, 4 skipped`; coverage 87.81%. |
| Backend whole-tree quality | **Fail** | Ruff reported 171 errors; Ruff format reported 92 files; mypy reported 49 errors in 21 files. |
| Frontend behavior | Pass | Node 20: lint and typecheck passed; Jest passed 17 suites / 151 tests; production mock build passed with 19 routes. |
| Mock E2E | Pass with retry debt | 79 passed; two tests passed only after Playwright retry. |
| Real-worker safety proof | **Fail** | Real Celery/Redis/PostgreSQL harness made zero signals/orders because regime was unknown. Broker/network tripwires did not fire. The proof is not a required CI check. |
| Migrations | Pass | One head (`0020_eod_flatten_operations`); empty upgrade and downgrade-to-base/re-upgrade passed on PostgreSQL 16. |
| Infrastructure syntax/config | Pass with debt | Default/prod Compose validation, Prometheus rules, and nginx syntax passed using fake required values and temporary local certificates. Production defaults and worker-startup validation remain incomplete. |
| Secret boundary | **Fail / inconsistent** | Current secret-file script passed and GitHub reports zero open secret alerts, but local all-history Gitleaks found nine redacted generic-key findings and the referenced `.gitleaks.toml` is absent. |
| Dependencies | **Fail** | Python audit found no known vulnerabilities. Node audit found 4 findings: 1 low, 2 high, 1 critical; GitHub Dependabot exposes six open alerts. |
| CodeQL | Pass at baseline | Latest default-branch CodeQL run passed for JavaScript/TypeScript, Actions, and Python; zero open code-scanning alerts. |

## Current finding matrix

The attached programme contains the current definitions for N-01 through N-09, but not the definitions of F-01 through F-44. F identifiers below therefore retain the programme's historical hypothesis only; they are not marked resolved without a recoverable definition and current-code proof.

| Finding | Current status | Current evidence | Tests | Required PR / dependency | Severity | Confidence |
|---|---|---|---|---|---|---|
| F-01 | Partial / definition missing | Current EOD implementation has exchange-session handling and stable operation evidence; exact finding text is absent. | EOD schedule/service suites pass in full baseline. | Definition recovery, then re-audit; Unit 8 if gaps remain. | Unknown | Medium |
| F-02 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-03 | Partial / definition missing | EOD commits intent before broker I/O; generic manual/emergency execution only flushes before the POST. | Existing EOD durability tests; generic path lacks equivalent proof. | Unit 1, then Unit 3. | P0 | High |
| F-04 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-05 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-06 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-07 | Open / historically claimed resolved | Programme says substantially resolved, but exact definition is absent, so the claim cannot be independently verified. | Cannot map responsibly. | Recover definition and re-audit. | Unknown | Low |
| F-08 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-09 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-10 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-11 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-12 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-13 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-14 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-15 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-16 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-17 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-18 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-19 | Partial / definition missing | Some financially meaningful beat tasks use owner-token Redis locks; account snapshot, daily reset, morning scan, purge, and funding paths remain unprotected. | Existing lock tests cover only protected tasks. | Unit 11; depends on task inventory. | P1 | High |
| F-20 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-21 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-22 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-23 | Open / historically claimed resolved | Programme says substantially resolved, but exact definition is absent, so the claim cannot be independently verified. | Cannot map responsibly. | Recover definition and re-audit. | Unknown | Low |
| F-24 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-25 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-26 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-27 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-28 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-29 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-30 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-31 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-32 | Open / historically claimed resolved | Programme says substantially resolved, but exact definition is absent, so the claim cannot be independently verified. | Cannot map responsibly. | Recover definition and re-audit. | Unknown | Low |
| F-33 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-34 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-35 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-36 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-37 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-38 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-39 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-40 | Open / historically claimed resolved | Programme says substantially resolved, but exact definition is absent, so the claim cannot be independently verified. | Cannot map responsibly. | Recover definition and re-audit. | Unknown | Low |
| F-41 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-42 | Open / historically claimed resolved | Programme says substantially resolved, but exact definition is absent, so the claim cannot be independently verified. | Cannot map responsibly. | Recover definition and re-audit. | Unknown | Low |
| F-43 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| F-44 | Open / definition missing | Historical programme says open; exact finding text is absent. | Cannot map responsibly. | Recover definition; assign owning unit. | Unknown | Low |
| N-01 | Partial | GitHub ruleset `17091691` now targets `~DEFAULT_BRANCH` and applies seven rules; repository `CODEOWNERS` and protected-PR proof remain unmerged. | GitHub ruleset API/CLI proof; 13 representative CODEOWNERS path matches. | Unit 0 PR. | P0 governance | High |
| N-02 | Confirmed open | `apps/api/app/api/v1/routes/orders.py:323` substitutes `Decimal("100")` for a manual market order. | Current tests do not disprove fabricated-price behavior. | Unit 7; depends on Unit 6. | P0 | High |
| N-03 | Confirmed open | `apps/api/app/api/v1/routes/orders.py:325-329` counts historical accepted/filled orders instead of current positions. | Current tests encode route behavior but not authoritative position semantics. | Unit 7; depends on Unit 6. | P1 | High |
| N-04 | Confirmed open | Manual preflight around `apps/api/app/api/v1/routes/orders.py:323-340` compares price/cash without an explicit currency compatibility or FX contract. | No mismatch fail-closed test. | Unit 7; depends on Unit 6. | P1 | High |
| N-05 | Confirmed open | `apps/api/app/market_data/mock_provider.py:97-100` prepends the prior close and slices the earliest timestamps, omitting newer completed bars. | Existing tests characterize the early-prefix behavior. | Unit 19 after ledger/execution units. | P2 quantitative | High |
| N-06 | Confirmed open | `apps/api/app/core/config.py:14-29` walks ancestors for `.env`, allowing developer-machine state to affect tests. | No focused parent-env hermeticity test. | Unit 15. | P1 | High |
| N-07 | Confirmed open | `.github/workflows/ci.yml:230-399` runs service E2E but no real-worker job; the sterile `apps/api/scripts/real_worker_paper_smoke.py` run produced no signal/order because regime was unknown. | Harness failed its required scenario; tripwires remained silent. | Unit 13 after Units 1–3 and ledger dependencies. | P1 | High |
| N-08 | Confirmed open | `apps/api/app/db/models/__init__.py:372-484` has unconstrained order lifecycle/financial strings and values; `:488-503` cascades order events, while `:511-548` cascades EOD evidence with strategy deletion. | Migration round-trip passes but direct-invalid-SQL coverage is absent. | Unit 4 after Unit 1 vocabulary. | P1 | High |
| N-09 | Confirmed open | `apps/api/app/execution/engine.py:195-219,346-407,514-555` flushes generic intent/evidence around broker I/O; `apps/api/app/db/session.py:34-41` commits only after the route returns. | `apps/api/tests/unit/test_execution_engine.py:348-370` expects broad exceptions to become terminal `error`. | Unit 1. | P0 | High |

## Execution-path evidence for Unit 1

- `apps/api/app/execution/engine.py`: intent, audit and request-event records are flushed before the broker call; the broad exception path maps all failures to terminal `error`.
- `apps/api/app/db/session.py`: request transactions commit only after the endpoint returns.
- `apps/api/app/workers/tasks.py`: worker transactions commit after the enclosing service finishes.
- `apps/api/app/execution/state_machine.py`: `error` is terminal and is absent from active duplicate-blocking states.
- Reconciliation currently requires active state plus a broker ID, so an ambiguous submission without an acknowledgement cannot be recovered.
- EOD flatten is the positive reference: it commits its operation and order identity before broker transmission.

## Unit 0 setting action and remaining proof

Repository ruleset `Guard` (`17091691`) was updated after this baseline was recorded. Its branch condition is now `~DEFAULT_BRANCH`; `gh ruleset check main` reports seven applicable rules. It has no bypass actors and requires the exact current CI contexts `Secrets Scan`, `Backend`, `Frontend`, `Security`, and `E2E Mock Smoke`; CodeQL remains required through its dedicated code-scanning rule.

The repository currently exposes only one collaborator, `@Almagidi`. Because the ruleset requires both code-owner review and approval of the last push by someone other than the pusher, the Unit 0 PR must remain unmerged until an authorized independent reviewer is available. This is an intended fail-closed result, not grounds to weaken the ruleset.

## Readiness at this baseline

- Mock: usable for supervised development, with known flaky E2E and quality/dependency debt.
- Paper: not safe for unattended use until ambiguous outcomes, ledger constraints, oversell and reservations are resolved.
- Supervised read-only demo: configuration-dependent; no external demo validation was attempted.
- Order-enabled supervised demo: not safe.
- Unattended demo: not safe.
- Live: unsupported and disabled.
