# Relium Launch Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make exact-manifest reviews preserve SQL evidence and code reasoning through metadata recomputation, present internally consistent decisions, and give customers a secure, observable PostgreSQL collector setup flow.

**Architecture:** Persist a self-contained review explanation document on every immutable attempt: normalized code findings, code-health provenance, and a deterministic primary reason. Metadata recomputation carries only same-review code evidence forward and appends newly evaluated metadata findings. Collector readiness is a separate, verification-backed resource derived from scoped tokens and collector heartbeats, never from an environment row.

**Tech Stack:** Python 3.10+, Starlette, psycopg/PostgreSQL migrations, sqlglot, Click, React/Vite, Vitest, Testing Library.

---

### Task 1: Exact-manifest semantic evidence and code findings

**Files:**
- Modify: `agent/deployment_review_service.py`
- Modify: `agent/github_app/runner.py`
- Modify: `agent/metadata_evidence/manifest_handoff.py`
- Modify: `agent/metadata_evidence/review_lifecycle.py`
- Test: `test_manifest_handoff_postgres.py`
- Test: `test_sql_semantic_diff.py`

- [ ] **Step 1: Write failing exact-manifest regression tests**

Add a base/head manifest case whose head adds `where p.payment_status = 'succeeded'` below a `LEFT JOIN payments p`, plus an unrelated safe projection change. Assert the stored attempt has evaluated semantic evidence, a filter before/after pair, and no fabricated evidence when either SQL body is absent.

- [ ] **Step 2: Run the focused tests and confirm the semantic payload is missing**

```powershell
python -m pytest test_manifest_handoff_postgres.py test_sql_semantic_diff.py -q
```

Expected: the exact-manifest resume assertion fails because `semantic_evidence` is `NULL`.

- [ ] **Step 3: Pass the already-computed evidence and normalized findings into the lifecycle**

Use one shared conversion from `material_findings` to lifecycle finding dictionaries, including AST severity, model, rule, message, and remediation detail. Both direct webhook and manifest-resume callers pass:

```python
semantic_evidence=_semantic_evidence(incident)
code_findings=review_code_findings(result)
```

The comparison remains `None` when no supported SQL was available.

- [ ] **Step 4: Re-run focused tests**

```powershell
python -m pytest test_manifest_handoff_postgres.py test_sql_semantic_diff.py test_semantic_evidence_persistence.py -q
```

Expected: PASS.

### Task 2: Immutable decision explanation and recomputation lifecycle

**Files:**
- Create: `agent/metadata_evidence/decision_explanation.py`
- Modify: `agent/metadata_evidence/review_lifecycle.py`
- Modify: `agent/metadata_evidence/recompute.py`
- Modify: `agent/metadata_evidence/publication_reconcile.py`
- Modify: `agent/api/routes.py`
- Test: `test_metadata_review_integration.py`
- Test: `test_publication_reconcile.py`
- Test: `test_public_api.py`

- [ ] **Step 1: Write failing lifecycle and publication tests**

Cover initial metadata wait followed by a complete snapshot and recomputation. Assert attempt 1 is unchanged; attempt 2 retains the AST finding, drops `metadata.pending`, records health 65 with a `-35` high-risk deduction, exposes a concise primary reason, and never publishes BLOCK with an empty reason list.

- [ ] **Step 2: Confirm failures**

```powershell
python -m pytest test_metadata_review_integration.py test_publication_reconcile.py test_public_api.py -q
```

Expected: current recomputation loses code findings and current publication has no material reason.

- [ ] **Step 3: Build and persist a deterministic explanation**

The attempt payload gains:

```python
{
    "findings": [...],
    "primary_reason": "...",
    "health_explanation": {
        "score": 65,
        "label": "Code review health",
        "basis": "static_code_analysis",
        "deductions": [{"component": "ast", "points": 35, "reason": "..."}],
    },
}
```

Derive BLOCK/WARN reasons from block/warn findings first, then from an explicit health or evidence-policy reason. ALLOW may truthfully report no material risks. Legacy attempts without fields are projected with a safe derived fallback and no invented semantic claim.

- [ ] **Step 4: Carry code facts into the new attempt only**

`recompute_review` reads code-category findings and health provenance from the immediately previous attempt, evaluates metadata with them, and writes a new attempt. It never updates old attempts.

- [ ] **Step 5: Re-run focused tests**

```powershell
python -m pytest test_metadata_review_integration.py test_publication_reconcile.py test_public_api.py -q
```

Expected: PASS for ALLOW/WARN/BLOCK, zero/one/multiple findings, and historical rows.

### Task 3: Production-observation baseline eligibility

**Files:**
- Modify: `agent/postgres_lifecycle_store.py`
- Modify: `agent/metadata_evidence/production_comparison.py`
- Test: `test_production_metadata_comparison.py`
- Test: `test_production_metadata_comparison_postgres.py`

- [ ] **Step 1: Add baseline tests**

Cover first snapshot/no baseline, second snapshot with changes, second snapshot without changes, and a newer stale/failed/ineligible snapshot that must be skipped in favor of the previous current eligible observation or no baseline.

- [ ] **Step 2: Confirm the stale-baseline test fails**

```powershell
python -m pytest test_production_metadata_comparison.py test_production_metadata_comparison_postgres.py -q
```

- [ ] **Step 3: Tighten the database selection**

Select baselines only when completeness is `COMPLETE` or `PARTIAL` and freshness is `CURRENT`; preserve the strict `(observed_at, received_at, snapshot_id)` ordering and repository/environment scope.

- [ ] **Step 4: Re-run comparison tests**

Expected: PASS with no PR-causality language in the persisted comparison.

### Task 4: Expired request reconciliation and deadline contract

**Files:**
- Modify: `agent/postgres_lifecycle_store.py`
- Modify: `agent/worker/lifecycle_worker.py`
- Modify: `agent/metadata_evidence/review_lifecycle.py`
- Modify: `docs/collector-install.md`
- Test: `test_expired_rerun_lifecycle.py`
- Test: `test_lifecycle_worker.py`

- [ ] **Step 1: Replace the bug-pinning expiry test**

Assert a never-decided review with an expired newest request returns to `WAITING_FOR_METADATA` with `decision IS NULL`, while a decided refresh restores `DECISION_READY` and its prior decision. Both paths append transitions and audit events without new attempts.

- [ ] **Step 2: Confirm the never-decided case fails**

```powershell
python -m pytest test_expired_rerun_lifecycle.py test_lifecycle_worker.py -q
```

- [ ] **Step 3: Reconcile both states in the store's single transaction**

Remove the `decision IS NOT NULL` exclusion, branch target state by decision presence, and have the worker call the store method instead of maintaining duplicate lifecycle SQL.

- [ ] **Step 4: Align request deadline copy**

Document the enforced 30-minute collection-request deadline separately from the 60/15-minute production-observation freshness policy.

- [ ] **Step 5: Re-run focused tests**

Expected: PASS.

### Task 5: Collector verification, status, and secure token setup API

**Files:**
- Create: `agent/migrations/postgres/0019_collector_health.sql`
- Modify: `agent/postgres_lifecycle_store.py`
- Modify: `agent/api/collector_routes.py`
- Modify: `agent/api/routes.py`
- Modify: `agent/api/contract.py`
- Modify: `agent/collector/client.py`
- Modify: `agent/collector/warehouse.py`
- Modify: `agent/collector/runner.py`
- Modify: `agent/cli.py`
- Modify: `docs/public-api.md`
- Test: `test_collector.py`
- Test: `test_collector_integration.py`
- Test: `test_public_api.py`
- Test: `test_api_contract.py`

- [ ] **Step 1: Write failing API and CLI tests**

Assert token issuance requires a human governance principal and warehouse entitlement, returns the secret once, and list/status never returns a token/hash/DSN. Assert collector status distinguishes `not_configured`, `configured_never_seen`, `connected`, and `stale`; `ensure_tenant()` alone never means connected. Assert `--request-id` reaches `run_collection`, and `--test` verifies API auth plus `SELECT 1` warehouse connectivity.

- [ ] **Step 2: Confirm failures**

```powershell
python -m pytest test_collector.py test_collector_integration.py test_public_api.py test_api_contract.py -q
```

- [ ] **Step 3: Add health persistence and projections**

Migration 0019 adds nullable `last_verified_at`, `last_failed_at`, and bounded `verification_status` fields to collector identities. Add scoped list/update methods and derive status from active collector tokens plus recent successful verification; environment `connected` is not consulted.

- [ ] **Step 4: Add the setup and verification endpoints**

Add human-only paid routes for setup status, one-time token issuance, and revocation, plus a collector-only verification heartbeat. The heartbeat accepts only a status/error category and never warehouse credentials or SQL.

- [ ] **Step 5: Repair the CLI behavior**

Forward `request_id` to `run_collection`. Add a `--test` path that authenticates/registers, opens a read-only PostgreSQL transaction, executes `SELECT 1`, reports safe status, and exits without needing a pending request.

- [ ] **Step 6: Re-run focused tests**

Expected: PASS.

### Task 6: Collector artifact integrity

**Files:**
- Create: `.github/workflows/collector-package.yml`
- Modify: `docs/collector-install.md`
- Test: `test_collector_install.py`

- [ ] **Step 1: Add a failing packaging contract test**

Assert the guide no longer contains an unverifiable hard-coded wheel digest and instructs verification against the `SHA256SUMS` file produced beside the wheel.

- [ ] **Step 2: Add a package workflow**

Build the wheel once, smoke-test `relium collect --help`, generate `SHA256SUMS` from that exact artifact, and upload both together. Do not publish or deploy automatically.

- [ ] **Step 3: Build locally and run packaging tests**

```powershell
python -m build
python -m pytest test_collector_install.py -q
```

Expected: wheel builds, checksum file matches the wheel bytes, tests pass.

### Task 7: Review decision UX

**Files (frontend repository):**
- Modify: `src/lib/adapter.js`
- Modify: `src/components/report/DecisionSummary.jsx`
- Modify: `src/components/report/ProductionMetadataChanges.jsx`
- Modify: `src/components/surfaces/Surfaces.jsx`
- Modify: `src/pages/ChangeReport.jsx`
- Modify: `src/styles/app.css`
- Test: `src/lib/adapter.test.js`
- Test: `src/components/report/DecisionSummary.test.jsx`
- Test: `src/components/report/ProductionMetadataChanges.test.jsx`
- Test: `src/components/surfaces/Surfaces.test.jsx`
- Test: `src/pages/ChangeReport.test.jsx`

- [ ] **Step 1: Write failing component and adapter tests**

Cover health present/absent/historical, BLOCK and WARN primary reasons, ALLOW without a blocking reason, legacy missing reason, BLOCK preview with zero findings plus explicit policy reason, and the intentional first-baseline state.

- [ ] **Step 2: Confirm failures with Vitest**

```powershell
npm test -- --run src/lib/adapter.test.js src/components/report/DecisionSummary.test.jsx src/components/surfaces/Surfaces.test.jsx
```

- [ ] **Step 3: Render backend explanation without re-deciding**

Map `primary_reason` and `health_explanation` from the current attempt. Show a prominent “Code review health — 65/100” block with its static-code meaning and deductions. Omit the primary-reason row only for a legacy record with no reason. Surface previews use the decision reason whenever risk-finding counts are zero.

- [ ] **Step 4: Make baseline and waiting states intentional**

Use “Baseline established” for `no_baseline`; explain that future eligible snapshots will compare with it. When metadata is waiting and collector status is not active, link the user directly to Warehouse evidence setup and name the 30-minute deadline.

- [ ] **Step 5: Re-run focused frontend tests**

Expected: PASS.

### Task 8: Guided collector onboarding UX

**Files (frontend repository):**
- Modify: `src/lib/api.js`
- Modify: `src/pages/Integrations.jsx`
- Modify: `src/pages/Settings.jsx`
- Modify: `src/styles/app.css`
- Test: `src/lib/api.test.js`
- Test: `src/pages/Integrations.test.jsx`
- Test: `src/pages/Settings.test.jsx`

- [ ] **Step 1: Write failing setup-flow tests**

Cover PostgreSQL-only support, customer-side credential boundary, one-time token reveal, secret-free environment-variable template, copyable install/test/run commands, status identities/timestamps, and all four health states.

- [ ] **Step 2: Add API calls and guided setup panel**

Expose setup status, issue/revoke actions, and the copyable `relium collect --test` verification command. Never cache an issued token beyond component memory and never refetch it.

- [ ] **Step 3: Remove environment-row connectivity claims**

Integrations and Settings derive warehouse state only from collector setup status or received evidence. `ensure_tenant()` cannot render “Connected.”

- [ ] **Step 4: Re-run focused frontend tests**

Expected: PASS.

### Task 9: Full-system verification and pull requests

**Files:**
- Test: `test_metadata_review_integration.py`
- Test: `src/pages/ChangeReport.test.jsx`

- [ ] **Step 1: Add the production-like scenario**

Exercise exact base/head manifests with the LEFT JOIN/WHERE change, Starter entitlement, initial request, PostgreSQL snapshot acceptance, recomputation, semantic diff, 65 code-health explanation, blast radius, no-baseline metadata state, coherent code finding, primary reason, BLOCK, audit history, and publication result. Add Free/no-warehouse and later-baseline variants.

- [ ] **Step 2: Run complete relevant backend validation**

```powershell
python -m pytest -q
python -m build
```

- [ ] **Step 3: Run complete frontend validation**

```powershell
npm test -- --run
npm run build
```

- [ ] **Step 4: Inspect the rendered flows**

Capture and inspect the review decision, first baseline, waiting-for-collector, token-issued, connected, and stale states. Confirm responsive layout, keyboard-reachable controls, labels, focus, and that no secret remains after leaving the one-time token state.

- [ ] **Step 5: Commit logical units and open pull requests**

Create logically separated backend and frontend commits, push feature branches, and open PRs without merging or deploying. Record exact commands/results, screenshots, migration, endpoints, remaining blockers, and deployment order in the final report.
