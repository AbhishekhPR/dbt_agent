# Continuous Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PostgreSQL-authoritative, evidence-traceable Relium lifecycle from pre-merge review through deployment monitoring, anomaly attribution, RCA, and independent delivery channels while preserving deterministic SQLite E2E validation.

**Architecture:** Add explicit evidence-policy, detector, warehouse, lifecycle, lineage, RCA, and delivery contracts around the existing review engine. PostgreSQL stores tenant-scoped immutable evidence, lifecycle transitions, and transactional outbox records; SQLite implements the same interfaces for isolated local E2E. Health is calculated only from evaluated findings, while policy-driven evidence coverage independently controls WARN/BLOCK behavior.

**Tech Stack:** Python 3.10+, dataclasses, SQLGlot, Starlette, psycopg, PostgreSQL, SQLite, dbt manifest JSON, unittest, deterministic JSON evidence, GitHub App APIs, optional Slack webhook.

---

### Task 1: Establish versioned evidence-policy contracts

**Files:**
- Create: `agent/evidence_policy.py`
- Modify: `agent/github_app/config.py`
- Modify: `relium.yml` schema handling in `agent/github_app/config.py`
- Create: `test_evidence_policy.py`

- [ ] **Step 1: Write the failing policy tests**

```python
import unittest

class EvidencePolicyTests(unittest.TestCase):
    def test_required_missing_evidence_warns_in_shadow_without_health_change(self):
        result = evaluate_evidence_policy(
            mode="shadow", policy=default_policy(),
            evidence={"head_manifest": EvidenceState.MISSING}, health=100,
        )
        self.assertEqual(result.coverage, "INCOMPLETE")
        self.assertEqual(result.decision, "WARN")
        self.assertEqual(result.health, 100)
        self.assertIn("head_manifest", result.reasons[0])

    def test_required_missing_evidence_blocks_in_enforce(self):
        result = evaluate_evidence_policy(
            mode="enforce", policy=default_policy(),
            evidence={"head_manifest": EvidenceState.MISSING}, health=100,
        )
        self.assertEqual(result.decision, "BLOCK")
        self.assertEqual(result.health, 100)

    def test_optional_missing_is_not_evaluated_without_escalation(self):
        result = evaluate_evidence_policy(
            mode="enforce", policy=default_policy(),
            evidence={"history": EvidenceState.NOT_EVALUATED}, health=100,
        )
        self.assertEqual(result.coverage, "COMPLETE")
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.health, 100)
        self.assertEqual(result.unevaluated, ["history"])

    def test_unsupported_never_becomes_pass(self):
        result = evaluate_evidence_policy(
            mode="enforce", policy=default_policy(),
            evidence={"detector:B05": EvidenceState.UNSUPPORTED}, health=100,
        )
        self.assertEqual(result.unsupported, ["detector:B05"])
        self.assertEqual(result.evidence["detector:B05"], EvidenceState.UNSUPPORTED)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest test_evidence_policy.py -v`

Expected: import or assertion failures because the policy contract does not yet exist.

- [ ] **Step 3: Implement immutable policy/version types**

Define `EvidenceState` (`EVALUATED`, `MISSING`, `FAILED`, `NOT_EVALUATED`, `UNSUPPORTED`, `BLOCKED_BY_CREDENTIALS`), `EvidenceRequirement` (`required`, `optional`, `disabled`), `EvidencePolicyVersion`, and `EvidenceCoverageResult`. `evaluate_evidence_policy()` must leave health unchanged, treat required material missing as WARN/BLOCK by mode, leave optional gaps unevaluated, and attach policy version plus reasons.

- [ ] **Step 4: Add repository policy parsing and validation**

Parse `relium.yml` evidence declarations with defaults for pre-merge, post-deployment, and RCA. Reject unknown requirement values, duplicate source names, negative thresholds, and policy versions without a content hash. Keep policy selection repository/environment scoped.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python -m unittest test_evidence_policy.py -v`

Expected: all policy semantics pass. Commit with `git add agent/evidence_policy.py agent/github_app/config.py test_evidence_policy.py && git commit -m "feat: add versioned evidence policy"`.

### Task 2: Add detector registry and complete SQL risk coverage

**Files:**
- Create: `agent/sql_detectors.py`
- Modify: `agent/ast_analyzer.py`
- Modify: `agent/pr_analysis.py`
- Create: `test_sql_detectors.py`
- Create: `docs/SQL-detector-coverage.md`

- [ ] **Step 1: Write detector contract and fixture tests**

Cover explicit/implicit Cartesian joins, duplicate-generating joins with grain/key metadata, GROUP BY grain changes, removed deduplication, weakened incremental watermark, and LEFT-to-INNER changes. Every test must assert finding type, owner, severity, dialect, remediation, evidence source, and `raw_code` versus `compiled_code` attribution. Add negative controls for intentional one-row parameter tables, approved Cartesian contracts, safe equivalent rewrites, preserved deduplication, and unchanged LEFT joins.

- [ ] **Step 2: Run the detector tests to establish failures**

Run: `python -m unittest test_sql_detectors.py -v`

Expected: the new detector registry and previously unsupported rules are absent.

- [ ] **Step 3: Implement typed detector specifications**

Define `DetectorSpec`, `DetectorFinding`, `DetectorEvidence`, and `DetectorRegistry`. Each detector returns `EVALUATED`, `NOT_EVALUATED`, or `UNSUPPORTED` explicitly; it never returns a clean PASS for an unsupported dialect or missing required metadata. Use SQLGlot AST nodes where supported and retain raw/compiled source labels.

- [ ] **Step 4: Implement the six detector families**

Use AST join nodes and policy/contract metadata for CROSS JOIN and join predicate checks; declared grain, uniqueness, and relationship metadata for multiplication; GROUP BY/select/aggregate signatures for grain changes; window/QUALIFY/dedup patterns plus declared grain for missing deduplication; incremental config, predicate, update column, lookback, and unique key metadata for watermarks; and base/head join-tree plus downstream filter analysis for LEFT-to-INNER. Keep dialect limitations in each finding.

- [ ] **Step 5: Connect the registry to review signals**

Replace direct legacy AST-only aggregation in `_ast_signal()` with registry output while preserving existing rule IDs and safe findings. Include detector version and evidence IDs in signal metadata. Ensure unsupported findings are carried to coverage and cannot reduce health or become PASS.

- [ ] **Step 6: Write detector coverage documentation and run tests**

Document owner, severity, supported dialects, fixtures, negative controls, limitations, remediation, and source attribution in `docs/SQL-detector-coverage.md`. Run `python -m unittest test_sql_detectors.py test_ast_analyzer.py test_pr_analysis.py -v` and commit.

### Task 3: Implement cardinality-collapse metadata detection

**Files:**
- Create: `agent/cardinality.py`
- Modify: `agent/signals.py`
- Modify: `agent/pr_analysis.py`
- Create: `test_cardinality.py`

- [ ] **Step 1: Add failing metric tests**

Test severe distinct-key collapse, gradual collapse, stable row count with key collapse, row-count drop without collapse, intentional filtered models, declared grain changes, insufficient history, small samples, null-key spikes, rollback recovery, and repeated deployment correlation.

- [ ] **Step 2: Implement typed observation and result contracts**

Define `CardinalityObservation` and `CardinalityResult` with model identity, declared grain, key columns, current/previous row and distinct-key counts, baseline window, thresholds, minimum sample, deployment/PR/commit IDs, scope changes, absolute delta, ratio, rows-per-key ratio, uniqueness ratio, null-key ratio, baseline deviation, status, and reason.

- [ ] **Step 3: Implement safe evaluation rules**

Return `NOT_EVALUATED` with an explicit reason for missing inputs, insufficient history, or small samples. Apply per-model thresholds and intentional-change contracts. Distinguish row-count movement from key collapse and preserve rollback/recovery evidence.

- [ ] **Step 4: Connect metadata signals and policy coverage**

Emit a metadata signal only from evaluated observations; attach downstream models/KPIs from lineage and deployment ID. Required metadata failure must affect coverage/decision policy, never health arithmetic. Run the focused tests and commit.

### Task 4: Create provider-neutral warehouse monitoring

**Files:**
- Create: `agent/warehouse.py`
- Create: `agent/warehouse_sqlite.py`
- Create: `agent/warehouse_postgres.py`
- Modify: `requirements.txt`
- Modify: `requirements.lock`
- Create: `test_warehouse_contract.py`
- Create: `test_warehouse_sqlite.py`
- Create: `test_warehouse_postgres.py`
- Create: `docs/metadata-monitoring-evidence.md`

- [ ] **Step 1: Define the read-only provider protocol and safety types**

Expose schema inspection, row count, null/duplicate/distinct counts, freshness/watermarks, types, sample-safe distributions, and KPI observations. Require allowlisted model identities, timeout, statement timeout, result-size limit, redacted query fingerprint, and an audit record on every call.

- [ ] **Step 2: Implement the SQLite adapter**

Use read-only URI connections and quoted identifiers. Return deterministic observations, explicit missing-data states, sample scope, and provider version. Add tests that reject writes, symlink escapes, unallowlisted tables, malformed inputs, and unbounded queries.

- [ ] **Step 3: Implement the PostgreSQL adapter boundary**

Use `psycopg` with read-only transaction settings, `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, allowlisted schema/table names, bounded sampling, and tenant-scoped credentials. If credentials are absent, return `BLOCKED_BY_CREDENTIALS` and never fabricate observations.

- [ ] **Step 4: Add monitoring windows and persistence hooks**

Represent immediate, scheduled, late-event, backfill, and rollback observation windows. Persist event time, ingestion time, source watermark, completeness, and excluded late/backfill scope. Run contract tests with fakes and local SQLite; mark live PostgreSQL validation blocked when no authorized DSN exists.

- [ ] **Step 5: Document coverage and commit**

Write `docs/metadata-monitoring-evidence.md` distinguishing SQLite E2E, PostgreSQL contract tests, real warehouse validation, and credential-blocked status. Run the focused suite and commit.

### Task 5: Build PostgreSQL lifecycle/evidence store and migration

**Files:**
- Create: `agent/lifecycle_models.py`
- Create: `agent/lifecycle_store.py`
- Create: `agent/postgres_lifecycle_store.py`
- Create: `agent/sqlite_lifecycle_store.py`
- Create: `agent/lifecycle_schema.sql`
- Create: `agent/migrate_pilot_store.py`
- Create: `test_lifecycle_store.py`
- Create: `test_lifecycle_migration.py`
- Create: `docs/deployment-lifecycle-evidence.md`

- [ ] **Step 1: Write schema and tenant-boundary tests**

Test organization/repository/environment isolation, immutable evidence references, append-only transition history, policy/detector/threshold version references, idempotency, deletion tombstones, repository disconnect, and unauthorized cross-tenant reads/writes.

- [ ] **Step 2: Implement PostgreSQL schema**

Create tenant, repository, environment, policy_version, detector_version, threshold_version, evidence, deployment, deployment_transition, monitoring_observation, anomaly, incident, rca_report, delivery_journal, outbox_event, retention_tombstone, and deletion_request tables. Enforce composite tenant keys, unique event/idempotency keys, foreign keys, transition sequence, and immutable evidence hashes.

- [ ] **Step 3: Implement store interfaces and SQLite compatibility**

Define methods for append evidence, create/get deployment, append transition, persist observation/anomaly/incident/RCA, claim outbox, journal delivery, disconnect repository, retain/tombstone, and delete tenant data. Make SQLite use the same contract and deterministic IDs for E2E.

- [ ] **Step 4: Implement transactional outbox**

Commit lifecycle mutation plus outbox event atomically. Add lease ownership, bounded retries, dead-letter state, event version checks, duplicate suppression, and explicit out-of-order rejection/retention. Test crash before publish, after publish, during lease, and restart recovery.

- [ ] **Step 5: Implement one-way pilot migration**

Freeze filesystem writes, export/checksum jobs/publications/evidence, import with tenant keys and schema version, reconcile counts/hashes, dual-read for a bounded verification period, then switch the writer. Preserve pilot files as historical evidence and emit migration evidence.

- [ ] **Step 6: Add backup/restore and load probes**

Provide local PostgreSQL backup/restore scripts and a documented pilot load target. Verify restored hashes, tenant isolation, outbox continuity, and transition ordering. Run the focused tests and commit.

### Task 6: Implement public deployment lifecycle events

**Files:**
- Create: `agent/deployment_events.py`
- Create: `agent/deployment_api.py`
- Modify: `agent/github_app/http_app.py`
- Modify: `agent/github_app/server.py`
- Create: `test_deployment_events.py`
- Create: `test_deployment_api.py`

- [ ] **Step 1: Write state-machine tests**

Assert exact deployment transitions: `reviewed`, `approved`, `deployment_started`, `deployment_succeeded`, `post_deployment_monitoring`, `healthy`, plus `deployment_failed`, `post_deployment_anomaly`, `rolled_back`, `incident_open`, `incident_resolved`. Assert exact incident transitions: `incident_open`, `incident_acknowledged`, `incident_investigating`, `incident_mitigated`, `incident_resolved`, reopen, and rollback mitigation.

- [ ] **Step 2: Implement validated event commands**

Require deployment ID, organization/repository/environment, PR/base/head/merge SHAs, manifest/schema/baseline hashes, changed/affected models, KPIs, pre-merge decision, timestamps, and source event ID. Reject invalid transitions; retain duplicate valid events idempotently; retain out-of-order events without silent state loss.

- [ ] **Step 3: Add public HTTP interfaces**

Add authenticated CI/CD callback, dashboard action, monitoring observation, anomaly, rollback, and incident routes. Route all writes through the lifecycle store/outbox, not direct internal calls. Return stable resource IDs and evidence references.

- [ ] **Step 4: Integrate GitHub review binding**

Persist the exact reviewed base/head/merge binding, manifest hash, schema snapshot hash, policy/detector/threshold versions, changed models, affected lineage, and pre-merge decision when a PR review completes.

- [ ] **Step 5: Run API/state tests and commit**

Run: `python -m unittest test_deployment_events.py test_deployment_api.py test_github_app_http.py test_github_app_jobs.py -v`.

### Task 7: Connect lineage, blast radius, and KPI impact

**Files:**
- Create: `agent/lineage_impact.py`
- Modify: `agent/dbt_context.py`
- Modify: `agent/semantic_context.py`
- Modify: `agent/semantic_kpi_inference.py`
- Modify: `agent/column_lineage.py`
- Create: `test_lineage_impact.py`
- Create: `docs/lineage-impact-evidence.md`

- [ ] **Step 1: Write completeness and impact tests**

Cover one upstream model affecting several marts, one KPI depending on multiple intermediates, rename, dependency removal, stale manifest rejection, base/head hash mismatch, downstream runtime anomaly, complete model lineage, partial column lineage, and unavailable KPI lineage.

- [ ] **Step 2: Implement immutable lineage snapshots**

Persist model, column, KPI, source, exposure, contract, owner, and test edges with manifest hash, dialect, unresolved nodes, and completeness (`COMPLETE`, `PARTIAL`, `UNAVAILABLE`) at each level.

- [ ] **Step 3: Enforce manifest/commit binding**

Reject stale or mismatched manifests before review/deployment evidence is accepted. Preserve the mismatch as required missing evidence and prevent a PASS result.

- [ ] **Step 4: Persist ranked blast radius and KPI impact**

Store direct/downstream models, paths, affected KPIs, owners, and confidence. Mark exhaustive impact claims invalid when completeness is not `COMPLETE`.

- [ ] **Step 5: Run lineage tests and commit**

Run the focused tests plus existing semantic/column-lineage suites and write the evidence documentation.

### Task 8: Build causality-safe RCA and incident evidence

**Files:**
- Create: `agent/rca_engine.py`
- Modify: `agent/root_cause_engine.py`
- Modify: `agent/reasoning_engine.py`
- Modify: `agent/incident.py`
- Create: `test_rca_engine.py`
- Create: `docs/RCA-evidence.md`

- [ ] **Step 1: Write RCA scenario tests**

Cover refund subtraction removal/revenue spike, LEFT-to-INNER/customer collapse, currency conversion removal, dedup removal/duplicates, weakened watermark/stale data, schema type failure, no relevant deployment, two candidates, unrelated concurrent change, rollback recovery, incomplete metadata, incomplete lineage, and false correlation.

- [ ] **Step 2: Implement evidence graph and ranking**

Accept anomaly, monitoring signal, model/KPI context, deployment history, base/head semantic change, lineage, SQL findings, schema/metadata differences, and historical outcomes. Rank invariant/contract/KPI/grain/dependency evidence highest, then related model/column/metadata evidence.

- [ ] **Step 3: Implement independent RCA confidence**

Emit `HIGH`, `MEDIUM`, `LOW`, or `UNATTRIBUTED` independently of review health. Require deployment-to-reviewed-commit binding, temporal ordering, affected path, and matching observed change/invariant breach for causal primary attribution. Missing links force unattributed RCA or alternative ranking.

- [ ] **Step 4: Add redaction and LLM explanation boundary**

Allow explanations to reference evidence IDs and redacted summaries only. Reject raw SQL, secrets, tokens, and unsupported claims. Add tests proving the explanation layer cannot add findings or alter the deterministic primary cause.

- [ ] **Step 5: Run RCA tests and commit**

Run the focused RCA suite and write `docs/RCA-evidence.md` with confidence and causality coverage.

### Task 9: Add independent GitHub, dashboard, and Slack delivery journals

**Files:**
- Create: `agent/delivery_journal.py`
- Create: `agent/dashboard_api.py`
- Modify: `agent/github_app/runner.py`
- Modify: `agent/github_app/slack.py`
- Modify: `agent/github_app/http_app.py`
- Create: `test_delivery_journal.py`
- Create: `test_dashboard_api.py`
- Create: `docs/dashboard-contracts.md`
- Create: `docs/slack-evidence.md`

- [ ] **Step 1: Write channel isolation tests**

Test duplicate GitHub comment/check suppression, dashboard retry, Slack bounded retry, channel-specific failure, redaction, payload hash, dead-letter handling, and optional Slack failure leaving decision/health unchanged.

- [ ] **Step 2: Implement the common delivery journal contract**

Record channel, tenant keys, resource ID, idempotency key, payload hash, evidence IDs, attempts, redaction state, status, retry/dead-letter state, and credential-blocked reason independently per channel.

- [ ] **Step 3: Implement dashboard resource contracts**

Expose review list/detail, deployment list/detail, monitoring status, anomaly list, incident/RCA detail, lineage, KPI impact, and repository settings resources with tenant authorization and evidence references.

- [ ] **Step 4: Integrate GitHub incident links and Slack alerts**

Keep existing sticky comment/check reconciliation. Add post-deployment incident/RCA links. Keep Slack disabled by default, redact all payloads, classify missing credentials as `BLOCKED BY CREDENTIALS`, and never gate core decisions unless policy marks Slack mandatory.

- [ ] **Step 5: Run delivery tests and commit**

Run the focused suites and document real Slack as credential-blocked unless a dedicated test channel and secure credential are present.

### Task 10: Extend local lifecycle E2E matrix

**Files:**
- Modify: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/e2e/scripts/run_matrix.py`
- Modify: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/e2e/scripts/matrix_adapters.py`
- Create: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/e2e/scripts/lifecycle_scenarios.py`
- Create: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/e2e/scripts/test_lifecycle_contract.py`
- Create: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/scenario-results/lifecycle/<scenario-id>.json`
- Modify: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/e2e-matrix.md`
- Modify: `C:/Users/Abhishekh/Relium_Continuous_Pipeline_Source/e2e/.worktrees/continuous-pipeline/known-limitations.md`

- [ ] **Step 1: Add deterministic lifecycle fixture contracts**

Every scenario must use immutable base/head commits, compiled manifests, isolated runtime storage, deployment records, observations, expected lineage, expected RCA, and expected deliveries. Reject hardcoded business column names in the harness.

- [ ] **Step 2: Add the ten policy tests and lifecycle scenarios**

Cover required missing evidence shadow/enforce, optional NOT EVALUATED, unsupported non-PASS, health/coverage separation, first deployment with no history, required metadata failure, optional Slack failure, and optional RCA evidence confidence decrease.

- [ ] **Step 3: Add the lifecycle anomaly scenarios**

Cover safe healthy deployment, risky block, shadow anomaly/RCA, enforce failure, cardinality collapse, duplicate explosion, freshness regression, KPI anomaly, rollback recovery, crash/restart, duplicate anomaly, missing warehouse evidence, incomplete lineage, and multiple candidate deployments.

- [ ] **Step 4: Run every scenario twice and normalize evidence**

Compare IDs, hashes, states, coverage, decisions, lineage completeness, RCA confidence, and delivery statuses after removing only run timestamps and physical paths. Any difference is a failure, not a PASS.

- [ ] **Step 5: Update supported/unsupported catalog and commit E2E changes**

Previously unsupported B05, B08, B09, B10, B11, C06, and D10 become PASS only with real detector/metadata evidence; otherwise remain UNSUPPORTED with technical reason.

### Task 11: Run live production-like lifecycle validation

**Files:**
- Create: `C:/Users/Abhishekh/.relium-validation/continuous-pipeline-live/scripts/run_live_lifecycle.py`
- Create: `C:/Users/Abhishekh/.relium-validation/continuous-pipeline-live/live-evidence/<scenario-id>.json`
- Create: `C:/Users/Abhishekh/.relium-validation/continuous-pipeline-live/scenario-results/live/<scenario-id>.json`

- [ ] **Step 1: Preflight exact App/repository scope**

Verify the dedicated `relium-e2e` App and `AbhishekhPR/relium-e2e-dbt` only. Do not touch Relium Pilot. Confirm webhook origin, selected-repository scope, permissions, and cleanup plan.

- [ ] **Step 2: Drive GitHub PR webhooks and public lifecycle events**

Run safe, risky shadow, enforce, cardinality, duplicate, freshness, KPI, rollback, crash/restart, duplicate delivery, missing evidence, incomplete lineage, and multiple-candidate scenarios through public HTTP interfaces. Never call internal lifecycle methods as live evidence.

- [ ] **Step 3: Capture immutable live evidence**

Record PR URL, delivery/job/deployment/incident IDs, SHAs, manifest/snapshot hashes, changed/affected models, KPIs, metadata values, anomaly, RCA, GitHub IDs, dashboard resource IDs, Slack state, and recovery transitions with redaction scans.

- [ ] **Step 4: Restore safe external state**

Leave scenario PRs unmerged, restore inert webhook, stop listeners/tunnels, remove temporary credentials, keep Slack disabled when unavailable, and preserve historical failures separately.

### Task 12: Generate reports, security gates, and release manifest

**Files:**
- Create: `scripts/generate_continuous_report.py`
- Create: `Relium_Continuous_Pipeline_E2E_Report.md`
- Create: `Relium_Continuous_Pipeline_E2E_Report.pdf`
- Create: `continuous-release-manifest.json`
- Create: `docs/security-summary.md`
- Create: `docs/known-limitations.md`
- Create: `checksums.sha256`

- [ ] **Step 1: Run correctness and security checks**

Run backend suites on supported Python versions, E2E helper tests, dbt validation, compileall, pip check, dependency audit, Bandit, secret detection, raw-SQL exposure scan, authorization/tenant tests, retry/dead-letter tests, PostgreSQL backup/restore, and documented pilot load test.

- [ ] **Step 2: Generate evidence-separated report sections**

Separate static SQL, semantic, metadata, real warehouse, local lifecycle, live GitHub, RCA, real Slack, and permanent hosted production coverage. Preserve prior release verdicts and historical failure reports.

- [ ] **Step 3: Compute checksums and validate artifacts**

Hash every generated artifact except the checksum file itself, reject absolute credential paths/secrets/raw SQL in customer-facing output, and verify normalized scenario repeatability.

- [ ] **Step 4: Select only a justified verdict**

Use `CONTINUOUS PIPELINE FUNCTIONAL E2E PASSED`, `PRODUCTION-LIKE CONTINUOUS PIPELINE E2E PASSED`, `PRODUCTION RELEASE READY`, or `PRODUCTION NO-GO` according to evidence. Do not use production-ready until permanent hosting, durable production storage/queue, multi-worker safety, warehouse integration, monitoring/rollback, and security/recovery gates all pass.

### Task 13: Final verification and handoff

**Files:**
- No planned source files; this task may modify only the exact files named by a
  failing verification step in Tasks 1–12.

- [ ] **Step 1: Run the complete backend suite**

Run: `python -m unittest discover -s . -p "test_*.py" -v` with AI and Slack disabled unless a dedicated credentialed test is explicitly authorized.

- [ ] **Step 2: Run the complete E2E suite and matrix twice**

Run helper tests, dbt seed/build/compile, full matrix twice, and normalized evidence comparison. Confirm no unsupported scenario is reported as PASS.

- [ ] **Step 3: Run final repository and tenant checks**

Verify both feature worktrees, exact release ancestry, clean historical evidence directories, PostgreSQL/SQLite tenant isolation, immutable evidence references, outbox recovery, deletion behavior, and no unsafe external scope.

- [ ] **Step 4: Review the final report against the guarantee**

Confirm every supported detector is documented/tested, contracts/KPIs are authoritative, resulting-data limitations are stated, unknown evidence is never PASS, ambiguity follows policy, and every decision/RCA claim links to evidence.

- [ ] **Step 5: Commit the verified implementation**

Run `git diff --check`, `git status --short --branch`, and commit only the feature worktree changes with a release summary. Preserve all earlier release commits and reports.
