# Review Truthfulness and Direct Blast Radius Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the promoted review report agree with its durable attempt and prove the existing direct-downstream blast-radius capability with a dedicated real dbt fixture.

**Architecture:** Add a small allowlisted review-plan projection and enforce the Request Changes publication identity invariant in the backend. Bind all frontend review facts to one explicit attempt, render governance and change data without inference, then map the backend's direct downstream model list into a real list-only blast-radius view.

**Tech Stack:** Python 3, PostgreSQL migrations and lifecycle store, Starlette API, unittest, React 19, Vitest, Testing Library, Vite, dbt manifests, GitHub Actions E2E harness.

---

### Task 1: Bind primary reason and checks to the visible attempt

**Files:**
- Modify: `relium-app/src/lib/adapter.js`
- Modify: `relium-app/src/lib/adapter.test.js`
- Modify: `relium-app/src/components/report/Findings.jsx`
- Modify: `relium-app/src/pages/ChangeReport.jsx`
- Test: `relium-app/src/components/report/Findings.test.jsx`

- [ ] Add failing adapter tests for BLOCK over info, WARN over info, ALLOW safe text, deterministic equal-severity order, exact findings attempt selection, and exact coverage-attempt counting.
- [ ] Run `npm test -- src/lib/adapter.test.js` and confirm each new assertion fails for the current first-item/latest-attempt behavior.
- [ ] Add a shared backend-severity comparator and decision-aware primary-reason selector. Select findings and coverage by `review.attempt`, and expose `checkCounts` separately from finding `counts`.
- [ ] Run `npm test -- src/lib/adapter.test.js` and confirm the adapter cases pass.
- [ ] Add a failing component test that renders two findings and five passed checks and expects `2 findings · 5 checks passed`.
- [ ] Run the focused component test and confirm it fails with `0 checks passed`.
- [ ] Update `Findings.jsx` and `ChangeReport.jsx` to consume `change.checkCounts.passed`.
- [ ] Run both focused frontend tests and confirm they pass.

### Task 2: Enforce verifiable Request Changes publication

**Files:**
- Modify: `agent/metadata_evidence/change_request.py`
- Modify: `agent/postgres_lifecycle_store.py`
- Create: `agent/migrations/postgres/0008_change_request_publication_identity.sql`
- Modify: `scripts/live_e2e/recording.py`
- Modify: `test_change_request.py`
- Modify: `test_postgres_lifecycle_store.py`
- Modify: `test_metadata_migration_upgrade.py`
- Modify: `relium-app/src/components/report/Governance.jsx`
- Test: `relium-app/src/components/report/Governance.test.jsx`

- [ ] Add failing backend tests proving an empty/object/missing publisher ID cannot produce PUBLISHED and that a valid scalar ID is persisted.
- [ ] Run the focused change-request tests and confirm malformed success currently becomes PUBLISHED.
- [ ] Add failing migration/store tests proving legacy PUBLISHED-null rows become FAILED with `publication identity missing; publication success cannot be verified`, and that new PUBLISHED-null rows violate the database invariant.
- [ ] Run focused PostgreSQL tests and confirm the invariant is absent.
- [ ] Validate remote IDs before completion, add the migration normalization/check constraint, and make the recording transport return a stable synthetic review ID for POST pull-request reviews.
- [ ] Run focused backend tests and confirm they pass.
- [ ] Add failing UI tests for PENDING, PUBLISHED with identity, FAILED with reason, and a defensive invalid legacy record.
- [ ] Run the governance test and confirm current wording reports an invalid record as not yet submitted.
- [ ] Render state-specific persisted truth and reserve successful wording for PUBLISHED with identity.
- [ ] Run the governance test and confirm it passes.

### Task 3: Make Slack preview semantic values safe

**Files:**
- Modify: `relium-app/src/components/surfaces/Surfaces.jsx`
- Test: `relium-app/src/components/surfaces/Surfaces.test.jsx`

- [ ] Add failing tests for a real metric, a real changed model, empty arrays, null, and object values; assert no preview contains `undefined`, `null`, or `[object Object]`.
- [ ] Run the focused test and confirm the empty and malformed cases fail.
- [ ] Add a small non-empty-string validator and truthful metric/model/generic sentence selection.
- [ ] Run the focused test and confirm all cases pass.

### Task 4: Render durable exception history visibly

**Files:**
- Modify: `relium-app/src/components/report/Governance.jsx`
- Modify: `relium-app/src/components/report/Governance.test.jsx`

- [ ] Add failing tests using active and revoked exception records, asserting visible rows for action, actor, reason, attempt, timestamp, scope, state, and revocation details.
- [ ] Run the focused test and confirm the closed/incomplete history presentation fails.
- [ ] Render the history table visibly and map only API-provided fields.
- [ ] Run the focused test and confirm it passes.

### Task 5: Expose and render the allowlisted change plan

**Files:**
- Modify: `agent/api/routes.py`
- Modify: `test_public_api.py`
- Modify: `relium-app/src/lib/adapter.js`
- Modify: `relium-app/src/lib/adapter.test.js`
- Modify: `relium-app/src/components/report/WhatChanged.jsx`
- Test: `relium-app/src/components/report/WhatChanged.test.jsx`

- [ ] Add a failing public API test asserting `change_plan` includes only changed models, dependency lists, downstream models, and safe target fields while excluding arbitrary review payload fields.
- [ ] Run the focused API test against PostgreSQL and confirm `change_plan` is absent.
- [ ] Add the allowlisted projection to `_review_view`.
- [ ] Run the focused API test and confirm it passes.
- [ ] Add failing adapter/component tests for changed model, target, SHA/hash rendering and explicit unavailable SQL/file/semantic statements.
- [ ] Run the focused frontend tests and confirm the section is currently absent.
- [ ] Map `review.change_plan` and implement the honest reduced `WhatChanged` view without a fake diff.
- [ ] Run the focused frontend tests and confirm they pass.

### Task 6: Verify Phase 1 against real attempt 10

**Files:**
- No product-file changes expected.

- [ ] Run the focused backend tests for API, migration, store, and change-request behavior.
- [ ] Run the focused frontend adapter/component tests.
- [ ] Run `npm run build` in `relium-app`.
- [ ] Start only the isolated local E2E database, API, and promoted dashboard.
- [ ] Capture the real review response and browser screenshots proving BLOCK plus separate exception, blocking primary reason, two findings/five checks, state-aware Request Changes, safe Slack wording, visible exception rows, real What Changed fields, and zero current downstream assets.
- [ ] Stop all local processes and remove only diagnostic artifacts created by this verification.

### Task 7: Prove direct downstream planning with a deterministic dbt fixture

**Files:**
- Modify: `agent/metadata_evidence/collection_plan.py` only if a typed direct-downstream projection is required
- Modify: `test_metadata_collection_plan.py`
- Modify: `relium-app/src/lib/adapter.js`
- Modify: `relium-app/src/components/report/BlastRadius.jsx`
- Test: `relium-app/src/components/report/BlastRadius.test.jsx`
- Modify/Create: dedicated fixture files under `scripts/e2e/` and the dedicated E2E dbt repository workflow inputs

- [ ] Add a failing collection-plan test with source -> staging -> changed fact -> two direct reports plus an exposure; expect exactly the two direct report model identities.
- [ ] Run the focused plan test and record whether existing output already satisfies the direct-only contract. If it passes, retain production logic and use the test as capability proof; if a typed safe projection is missing, add only that projection under a separate failing assertion.
- [ ] Add failing adapter/component tests that render the two backend-provided report models without frontend fixture lookup and show Graph as unavailable.
- [ ] Run the focused frontend tests and confirm the current fixture-asset lookup drops the real models.
- [ ] Implement the real list mapping with model type, direct relationship, and depth 1; disable Graph.
- [ ] Run focused backend and frontend blast-radius tests.
- [ ] Create the minimal dbt fixture branch and manifest using real `source()`, `ref()`, and exposure syntax.
- [ ] Run only the dedicated blast-radius E2E through the fixed webhook preservation path.
- [ ] Independently verify manifest nodes/edges, persisted plan lineage, API response, adapter result, and rendered list.
- [ ] Close the unmerged fixture PR, remove only its branch, restore and verify the exact webhook through GitHub, stop tunnel/listeners, and run the evidence secret scan.

### Task 8: Final verification

**Files:**
- No new behavior changes expected.

- [ ] Run the complete backend unit/integration suite with the required PostgreSQL test DSN and record exact tests, skips, failures, and elapsed time.
- [ ] Run the complete frontend test suite and record the exact count.
- [ ] Run the frontend production build and record exit status and output summary.
- [ ] Inspect `git diff`, `git status`, fixture repository branches/PRs, local listeners, webhook restoration evidence, and secret-scan output.
- [ ] Report Phase 1 and Phase 2 verdicts separately and explicitly list direct-only, no-transitive, no-exposure, and no-graph limitations that remain.
