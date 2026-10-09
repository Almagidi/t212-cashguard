# Unit 0 Merge Governance Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Make the repository's existing `Guard` ruleset actually protect the default branch and add review ownership for every named safety-critical path without weakening or bypassing any control.

**Architecture:** Keep governance in two independently verifiable layers: GitHub's active repository ruleset protects `~DEFAULT_BRANCH`, while repository-owned `CODEOWNERS` declares the human review boundary. The current workflow job names are used exactly; CodeQL remains enforced through the existing code-scanning rule rather than a nonexistent synthetic status-check name.

**Tech Stack:** GitHub REST API/CLI, repository rulesets, CODEOWNERS, GitHub Actions, CodeQL.

---

### Task 1: Record the pre-change governance failure

**Files:**
- Create: `docs/superpowers/specs/2026-09-11-safety-remediation-baseline.md`

- [x] Fetch and pin current `origin/main`.
- [x] Inspect ruleset `17091691`, classic branch protection, default branch, exact CI job names, and CodeQL state.
- [x] Prove `gh ruleset check main` reports zero applicable rules.
- [x] Record security, dependency, test, migration, infrastructure, and worker-proof baselines.

### Task 2: Add repository-owned safety review boundaries

**Files:**
- Create: `.github/CODEOWNERS`

- [x] Add owners for execution, broker, risk, reconciliation, workers, models, migrations, workflows, infrastructure, Compose, and Dockerfiles.
- [x] Validate that all 13 representative required paths resolve to `@Almagidi`.
- [x] Confirm no unrelated ownership patterns were added.

### Task 3: Correct the active default-branch ruleset

**GitHub setting:** repository ruleset `Guard` (`17091691`)

- [x] Preserve deletion, non-fast-forward, signatures, code-quality and CodeQL code-scanning rules.
- [x] Set the ref condition to `~DEFAULT_BRANCH` with no bypass actors.
- [x] Require pull requests, one approval, stale-approval dismissal, last-push approval, code-owner review, review-thread resolution, and required attribution.
- [x] Require exact current CI contexts: `Secrets Scan`, `Backend`, `Frontend`, `Security`, and `E2E Mock Smoke`.
- [x] Use strict required-status-check policy and avoid blocking initial branch creation.
- [x] Re-read the resulting API representation; prove seven rules apply to `main`.

### Task 4: Create programme tracking and protected proof PR

- [x] Create GitHub tracking issue #258 with all 28 units (Unit 0 plus Units 1–27) and current finding evidence.
- [ ] Commit and push only the governance/baseline files.
- [ ] Open a focused PR using the mandated body structure and link it to the tracking issue.
- [ ] Verify the PR triggers exact required checks and requires an independent code-owner approval.
- [ ] Do not approve, bypass, or merge the PR as its author.
- [x] Confirm the repository currently has only one collaborator; preserve the resulting independent-approval blocker rather than weakening governance.

### Task 5: Review and hand off

- [ ] Run `git diff --check` and inspect every changed line.
- [ ] Run a fresh read-only independent review against the base/head SHAs.
- [ ] Record PASS/FAIL with path/line evidence.
- [ ] Record rollback: revert the CODEOWNERS commit and restore the captured pre-change ruleset payload through the same API only if governance itself causes an unintended lockout.
- [ ] Keep all stronger protections in place while any review/CI blocker is resolved.

## Explicit non-goals

- No product or trading behavior changes.
- No workflow job implementation or dependency upgrade.
- No broker, credential, demo, Kraken, DCA, or live activity.
- No merge without the newly required independent approval and green checks.
