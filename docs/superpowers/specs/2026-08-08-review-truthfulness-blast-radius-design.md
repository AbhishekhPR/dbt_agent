# Review Truthfulness and Direct Blast Radius Design

## Scope

This work has two ordered phases. Phase 1 makes the promoted `relium-app` review report agree with the durable attempt it displays. Phase 2 proves the existing direct-downstream blast-radius capability with a dedicated dbt fixture. It does not change Relium decision semantics, exception semantics, collector architecture, GitHub App infrastructure, incident RCA, or the promoted dashboard directories.

## Phase 1: truthful review report

### Attempt binding

The visible review decision, findings, and evidence checks must all use `review.attempt`. The adapter selects the findings entry whose attempt equals that value and filters coverage rows to exactly the same attempt. It must not independently infer “latest” values from array position or maximum attempt.

Finding counts and evidence-evaluation counts remain separate. Findings are counted by mapped finding severity. Evidence rows are counted from the selected coverage attempt. Because `EVALUATED` means the evidence was available and evaluated—not that an individual validation passed—every report location uses “checks evaluated” and consumes the evaluation count.

### Primary reason

The backend severities `block`, `warn`, and `info` are the ordering authority. For a BLOCK decision, a blocking finding is primary. For WARN, a blocking finding in shadow mode outranks a warning, otherwise the warning-driving finding is primary. ALLOW uses a safe outcome statement and never promotes an informational finding into a problem headline.

Ordering is deterministic: severity rank first, then code, relation, column, and message. The finding list and primary-reason selection share the same ordering helper. No finding code or message is special-cased.

### Request Changes

The durable invariant is: `PUBLISHED` requires a valid persisted `remote_review_id`. A missing or malformed publisher identity is an unverifiable publication outcome and must not transition to PUBLISHED.

The existing domain has no separate UNVERIFIED state. Therefore legacy `PUBLISHED` rows without a remote identity are normalized to `FAILED` with the explicit reason `publication identity missing; publication success cannot be verified`. This does not assert that GitHub rejected the operation; it records only that Relium cannot verify success. No remote ID is invented.

The local recording transport returns a stable synthetic review ID for its pull-request-review endpoint. The report renders:

- PENDING: awaiting GitHub submission.
- PUBLISHED: published, with the persisted remote review ID.
- FAILED: the persisted failure/unverified reason.

### Slack preview

Only validated, non-empty string identifiers may enter user-facing semantic slots. When a metric is known, the preview names it. Otherwise, when a changed model is known, it names that model without calling it a KPI. When neither is known, the preview uses generic analytical-change wording. It must never render `undefined`, `null`, or `[object Object]` as semantic content.

### Exception history

The exceptions API remains authoritative. The report visibly renders its returned rows, including approval/revocation action, actor, reason, attempt, timestamp, scope, state, and revocation metadata when present. Actors are shown verbatim; service-token identity is not replaced with a human name.

### What Changed

The review API exposes a small allowlisted `change_plan` projection from the persisted review plan:

- `changed_models`
- `added_dependencies`
- `removed_dependencies`
- `downstream_models`
- safe target identity fields: relation name, model unique ID, dependency kind, requested columns, and reason

It does not expose raw manifests or arbitrary stored payloads. The adapter uses this projection. The report shows changed models, relevant dependency/target identities, base/head SHAs, and manifest hashes. It explicitly states that SQL diff, changed-file detail, and semantic before/after diff were not persisted when unavailable.

## Phase 2: direct downstream blast radius

The dedicated fixture uses real dbt `source()` and `ref()` relationships:

```text
source orders
  -> stg_orders
  -> fct_orders
     -> rpt_revenue
     -> rpt_customer_revenue
```

An exposure depending on `rpt_revenue` is included as independent manifest evidence. The current engine reads direct downstream models only, so the acceptance result is the two report models. The exposure is not counted.

The API returns the persisted direct downstream model identities from the real collection plan. The adapter maps them into a real list without consulting frontend fixture assets. Each row shows only supported facts: model identity, type `model`, direct relationship, and depth 1.

Graph view is disabled and labelled unavailable because the backend does not expose sufficient path/edge geometry. No transitive path or exposure propagation is implied.

## Testing and proof

All behavior changes follow red-green testing. Phase 1 receives focused backend API/store/migration tests and frontend adapter/component tests, followed by a production build and real attempt-10 browser proof. Phase 2 receives collection-plan tests plus one dedicated blast-radius E2E using only the dedicated E2E App/repository, isolated warehouse, and fixed webhook preservation path.

Final verification runs the complete backend unit/integration suite, the complete frontend suite, and the frontend production build. The final report separates Phase 1 and Phase 2 verdicts and lists the remaining direct-only, no-exposure, and no-graph limitations.

## Safety

No Relium Pilot credential or resource is used. The fixture PR is never merged. Cleanup closes the fixture PR, removes only the fixture branch, restores and verifies the exact webhook configuration through GitHub, stops tunnel/listener processes, and scans evidence for secrets.
