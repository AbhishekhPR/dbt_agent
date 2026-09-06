# Semantic Evidence Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover valid SQL semantic evidence onto a new attempt for reviews whose immutable manifest pair exists but whose older attempt stored no semantic document.

**Architecture:** Keep semantic analysis independent of warehouse evidence. Reuse the existing manifest comparison service only after verifying the review's exact SHA/hash bindings, and persist the result through the existing immutable attempt writer and API projection.

**Tech Stack:** Python 3, unittest, PostgreSQL, sqlglot, React, Vitest.

---

### Task 1: Prove the production lifecycle and local engine behavior

**Files:**
- Inspect: `agent/metadata_evidence/manifest_handoff.py`
- Inspect: `agent/metadata_evidence/recompute.py`
- Test: `test_sql_semantic_diff.py`

- [x] Verify the production deployment SHAs for API, worker, and frontend.
- [x] Query the exact review, attempts, manifest evidence, audit events, and outbox events read-only.
- [x] Confirm both compiled SQL sides, the changed model, the null attempt field, and the lifecycle timestamps.
- [x] Run the risky LEFT JOIN/WHERE and safe projection semantic-diff tests.

### Task 2: Recover evidence only from exact immutable manifests

**Files:**
- Modify: `agent/metadata_evidence/recompute.py`
- Test: `test_semantic_evidence_persistence.py`
- Test: `test_metadata_review_integration.py`

- [x] Add failing coverage for a new attempt recovering evaluated semantic evidence while attempt 1 remains byte-for-byte unchanged.
- [x] Add failing coverage for the safe projection comparison and absence of `LEFT_JOIN_NULLIFIED`.
- [x] Add safety coverage for a missing manifest side, hash mismatch, and missing changed model.
- [x] Implement exact-review manifest recovery only when the previous attempt's semantic field is null.
- [x] Run the PostgreSQL focused tests and confirm they pass.

### Task 3: Cover both ingress paths and the dashboard contract

**Files:**
- Modify: `test_served_webhook_metadata_lifecycle.py`
- Verify: `test_manifest_handoff_postgres.py`
- Verify: `test_metadata_review_integration.py`
- Verify: `src/lib/adapter.test.js` in `relium-app`
- Verify: `src/components/report/SemanticChanges.test.jsx` in `relium-app`

- [x] Add a served direct-webhook test using the exact LEFT JOIN/WHERE manifests and assert persisted semantic evidence plus `LEFT_JOIN_NULLIFIED`.
- [x] Run the existing manifest-resume regression.
- [x] Run the current-attempt API projection and historical-attempt regressions.
- [x] Run frontend adapter/component tests for evaluated, evaluated-empty, unavailable, and current-attempt selection.

### Task 4: Full verification and focused PR

**Files:**
- Review all files changed on `fix/semantic-evidence-recovery`.

- [x] Run the full backend suite against an isolated PostgreSQL instance.
- [x] Run the full frontend suite and production build from the deployed frontend source.
- [x] Review the diff for semantic-only scope and secret leakage.
- [ ] Commit, push, and open one backend PR without merging or deploying it.
- [ ] Report root cause, loss layer, files, test counts, PR, deployment order, and exact post-deploy production verification steps.
