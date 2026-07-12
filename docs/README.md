# Documentation Index

Navigation hub for the T212 CashGuard documentation set. This is an additive
index only — every link points to a file that already exists in `docs/`.

> Project-level entry point: the repository [`README.md`](../README.md).
> CashGuard is a safety-first **demo / paper** trading platform. Live trading is
> disabled by default and is out of scope for these docs.

## Safety & Release

| Document | Purpose |
| --- | --- |
| [Safety Model](SAFETY_MODEL.md) | Core safety architecture: gates, kill switch, fail-closed policies. |
| [Paper Execution Safety](paper-execution.md) | Safety boundaries around paper/demo order execution. |
| [Release Readiness](RELEASE_READINESS.md) | Readiness criteria before a release candidate. |
| [Demo RC Checklist](DEMO_RC_CHECKLIST.md) | Release-candidate checklist for the demo build. |
| [Manual Demo RC Runbook](MANUAL_DEMO_RC_RUNBOOK.md) | Step-by-step manual demo release-candidate runbook. |
| [Live Smoke Test Runbook](LIVE_SMOKE_TEST_RUNBOOK.md) | Future tiny supervised live-money smoke test procedure — **do not run yet**. |
| [Live-Readiness Recency and Attestation Expiry Proposal](live-readiness-recency-expiry-proposal.md) | Docs-only proposal for future live-readiness freshness and attestation expiry policy. |
| [Security](security.md) | Security posture, controls, and review notes. |

## Architecture

| Document | Purpose |
| --- | --- |
| [Architecture](architecture.md) | System architecture overview. |
| [Broker Provider Design](architecture/broker-provider-design.md) | Broker provider abstraction and construction boundaries. |
| [Broker Interface Readiness Audit](architecture/broker-interface-readiness-audit.md) | Readiness audit of the broker interface. |
| [PositionMonitor Unrealized P&L Failure Policy](architecture/position-monitor-unrealized-pnl-failure-policy.md) | Fail-closed policy for unrealized-P&L snapshot failures. |
| [Implementation Roadmap](implementation-roadmap.md) | Planned implementation phases and direction. |

## Operations & Runbooks

| Document | Purpose |
| --- | --- |
| [Runbook](runbook.md) | Operational runbook for running the platform. |
| [Operator Dashboard — Local Manual QA Gate](operator-manual-qa.md) | Manual QA gate for the operator dashboard. |
| [Troubleshooting](troubleshooting.md) | Common issues and resolutions. |

## Setup & Testing

| Document | Purpose |
| --- | --- |
| [Local Setup Guide](local-setup.md) | Local development environment setup. |
| [Local E2E](LOCAL_E2E.md) | Running the end-to-end suite locally. |
| [Testing](testing.md) | Testing strategy and conventions. |

## Integrations

| Document | Purpose |
| --- | --- |
| [Telegram Integration](telegram-integration.md) | Telegram control/alerting integration. |

## QA Records

Point-in-time manual QA evidence, mostly for Trading 212 DEMO order/reconciliation flows.

| Document | Purpose |
| --- | --- |
| [Manual QA Record — 2026-05-11](qa/manual-qa-2026-05-11.md) | Dated manual QA record. |
| [T-OPS-017 — Manual Paper Execution QA](qa/t-ops-017-manual-paper-execution-qa.md) | Manual paper-execution QA. |
| [Trading 212 Demo Controlled Order QA](qa/trading212-demo-controlled-order-qa.md) | Single controlled-order demo QA. |
| [Trading 212 DEMO Controlled Multi-Order Placement QA](qa/trading212-demo-controlled-multi-order-placement-qa.md) | Multi-order placement demo QA. |
| [Trading 212 DEMO Controlled Multi-Order Real Smoke QA](qa/trading212-demo-controlled-multi-order-real-smoke-qa.md) | Multi-order real smoke demo QA. |
| [Trading 212 DEMO Multi-Order Reconciliation Smoke QA](qa/trading212-demo-multi-order-reconciliation-smoke-qa.md) | Multi-order reconciliation smoke demo QA. |
| [Trading 212 DEMO Pending Order Follow-Up QA](qa/trading212-demo-pending-order-follow-up-qa.md) | Pending-order follow-up demo QA. |
| [Trading 212 DEMO Pending Order Follow-Up 2 QA](qa/trading212-demo-pending-order-follow-up-2-qa.md) | Pending-order follow-up (part 2) demo QA. |
| [Trading 212 Demo Reconciliation QA](qa/trading212-demo-reconciliation-qa.md) | Reconciliation demo QA. |
| [Trading 212 DEMO Reconciliation Worker QA](qa/trading212-demo-reconciliation-worker-qa.md) | Reconciliation worker demo QA. |
| [Trading 212 DEMO Scheduled Reconciliation QA](qa/trading212-demo-scheduled-reconciliation-qa.md) | Scheduled reconciliation demo QA. |

## Design Specs & Plans

UI/feature design specs and implementation plans (authored under `superpowers/`).

| Document | Purpose |
| --- | --- |
| [UI Redesign — Full Visual Identity Overhaul (spec)](superpowers/specs/2026-04-26-ui-redesign-design.md) | Visual identity overhaul design spec. |
| [UI Redesign — Full Visual Identity Overhaul (plan)](superpowers/plans/2026-04-26-ui-redesign.md) | Implementation plan for the overhaul. |
| [UI Design Pass — Inner Pages (spec)](superpowers/specs/2026-04-26-ui-design-pass-design.md) | Inner-pages design spec. |
| [UI Design Pass — Inner Pages (plan)](superpowers/plans/2026-04-26-ui-design-pass.md) | Inner-pages implementation plan. |
| [Intelligence Portfolio System — Design Spec](superpowers/specs/2026-04-27-intelligence-portfolio-design.md) | Intelligence portfolio system design. |

## Historical

| Document | Purpose |
| --- | --- |
| [Debug Report — 2026-04-18](debug-report-2026-04-18.md) | Point-in-time debug report. |
