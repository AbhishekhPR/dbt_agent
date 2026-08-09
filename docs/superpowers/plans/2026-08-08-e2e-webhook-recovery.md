# Dedicated E2E Webhook Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover and independently verify the dedicated `relium-e2e` GitHub App webhook, then permanently prevent cleanup from claiming an unproved restoration.

**Architecture:** Treat Run 11's persisted preservation and GitHub-verified cleanup records as the recovery anchor. Perform all webhook reads and writes in the existing GitHub Actions job using only the dedicated E2E App credentials, then preserve, temporarily repoint, restore, and re-read the exact non-secret configuration. Keep fixture cleanup and the fresh-process webhook cleanup result fail-closed.

**Tech Stack:** Python 3.11, `unittest`, GitHub Actions, GitHub App REST API, Cloudflare quick tunnel.

---

### Task 1: Prove the recovery anchor

**Files:**
- Inspect: `C:/Users/Abhishekh/tmp/relium-webhook-recovery/run-31085032785/webhook-recovery-record.json`
- Inspect: `C:/Users/Abhishekh/tmp/relium-webhook-recovery/run-31085032785/cleanup-verification.json`
- Inspect: `C:/Users/Abhishekh/tmp/relium-webhook-recovery/run-31246080645/cleanup-verification.json`

- [x] **Step 1: Parse only non-secret preservation fields**

Read `url`, `content_type`, field names, `matches_original`, and `verified_through_github`; never print or request a secret.

- [x] **Step 2: Cross-check genuine delivery evidence**

Confirm Run 11 accepted a signature-verified `pull_request` delivery and the corrupting governance run used the known dead tunnel.

### Task 2: Keep cleanup fail-closed

**Files:**
- Modify: `scripts/e2e/metadata_review_e2e.py`
- Modify: `.github/workflows/governance-e2e.yml`
- Test: `test_e2e_driver_fail_closed.py`

- [x] **Step 1: Run the existing fresh-process regression test**

Run: `python -m unittest test_e2e_driver_fail_closed.CleanupCompletenessTests.test_fresh_outer_cleanup_never_claims_webhook_restoration -v`

Expected: PASS, with `restored` equal to `None` and `verified_through_github` false.

- [x] **Step 2: Verify the test detects the historical defect**

Temporarily apply the historical unconditional-success behavior only in an isolated copied tree, run the same test there, and require a failure. Do not modify the active worktree for the red proof.

- [x] **Step 3: Keep the minimal implementation**

The no-record path must remain equivalent to:

```python
{"restored": None, "verified_through_github": False,
 "note": "no recovery record; no webhook mutation was attributed to this fresh process"}
```

The always-run recovery cleanup must restore only from an existing preservation record and must not need the fixture PAT.

### Task 3: Harden exact restoration and fixture verification

**Files:**
- Modify: `scripts/e2e/metadata_review_e2e.py`
- Modify: `scripts/e2e/webhook_recovery_e2e.py`
- Modify: `scripts/e2e/cleanup_stale_fixtures.py`
- Test: `test_e2e_driver_fail_closed.py`

- [x] **Step 1: Require exact GitHub confirmation**

Require PATCH HTTP 200 plus exact URL and content-type matches before setting `verified_through_github` or `matches_original` true.

- [x] **Step 2: Gate configuration access by App identity and installation scope**

Read `/app`, require slug `relium-e2e`, require `pull_request` subscription, and require installation scope exactly `AbhishekhPR/relium-e2e-dbt` before webhook mutation.

- [x] **Step 3: Verify fixture absence through the App installation token**

Paginate open pull requests and ephemeral refs, and fail if any remain. Do not use the fixture PAT for webhook administration.

- [x] **Step 4: Run focused tests**

Run: `python -m unittest test_e2e_driver_fail_closed.CleanupCompletenessTests test_e2e_driver_fail_closed.WebhookRecoveryHarnessTests test_e2e_driver_fail_closed.StaleFixtureCleanupHarnessTests -v`

Expected: all selected tests pass with zero failures.

- [x] **Step 5: Run the relevant driver suite**

Run: `python -m unittest test_e2e_driver_fail_closed -v`

Expected: suite passes with zero failures.

### Task 4: Execute only the focused secure proof

**Files:**
- Verify: `.github/workflows/governance-e2e.yml`
- Produce through Actions: `webhook-recovery-cleanup-proof.json`

- [x] **Step 1: Commit and push the scoped harness hardening**

Commit only the five existing recovery/cleanup files, their test, and this plan; push `governance-live-e2e`.

- [x] **Step 2: Dispatch webhook-recovery mode only**

Use the workflow's `webhook-recovery` input. Do not dispatch governance or the full metadata-review product E2E.

- [x] **Step 3: Verify the uploaded evidence**

Require original URL before mutation, a distinct temporary URL, final URL equal to original, matching content type/events/TLS state, intended active state supported by genuine delivery evidence, `verified_through_github: true`, no fixture PRs/branches, stopped tunnel/listener, no secret capture, and `relium_pilot_touched: false`.

- [x] **Step 4: Commit any evidence-only test correction separately if required**

No evidence-only correction was required. Make no unrelated product, frontend,
decision, or fixture changes.
