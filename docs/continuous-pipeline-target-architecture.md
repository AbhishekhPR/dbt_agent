# Continuous Pipeline: Target Architecture

## Guarantees and result contract

Every supported detector has a documented owner, severity, dialect scope,
positive fixtures, negative controls, limitations, remediation, source
attribution, and raw/compiled SQL attribution tests. The system never claims
universal SQL semantic correctness; resulting-data comparison covers risks that
static analysis cannot prove.

Every review, deployment, observation, anomaly, incident, RCA conclusion, and
delivery is an immutable evidence reference. A normalized decision has separate
fields:

```text
health             evaluated finding severity only; unavailable evidence adds 0
coverage           COMPLETE or INCOMPLETE required-source coverage
decision           ALLOW, WARN, or BLOCK
evidence_reasons   required missing/failed sources and their evidence IDs
unsupported        explicit UNSUPPORTED capability records, never PASS
integration_state  e.g. BLOCKED BY CREDENTIALS for optional Slack/warehouse
```

Material required evidence missing produces WARN in shadow mode and BLOCK in
enforce mode. Optional or non-material missing evidence is NOT EVALUATED, has no
health adjustment, and cannot escalate the decision. Unsupported capabilities
remain UNSUPPORTED and only affect the decision when repository policy marks them
required. Optional Slack failure never changes the deployment decision.

## Components and data flow

1. **Policy and contracts**: repository-scoped configuration declares each
   evidence source as required, optional, or disabled. Defaults cover immutable
   manifests, complete compare results, model resolution, declared contracts,
   post-deployment identity/monitoring, RCA signal/history/primary-cause proof,
   optional history, lineage, dashboard, Slack, and warehouse access.
2. **Detector registry**: typed detector specifications for CROSS JOIN,
   duplicate-generating joins, grain changes, missing deduplication, incremental
   watermarks, LEFT-to-INNER changes, and cardinality collapse. Each finding
   stores owner, severity, dialect, evidence references, limitation, and fix.
3. **Result comparison and metadata boundary**: a read-only provider interface
   exposes schema, counts, nulls, duplicates, distinct keys, freshness,
   distributions, and KPI observations. SQLite is the deterministic E2E adapter.
   A production adapter is added only for an explicitly authorized warehouse
   with credentials; otherwise the result is BLOCKED BY CREDENTIALS.
4. **Lifecycle store/API**: a tenant/repository-scoped SQLite store records
   immutable deployment records, append-only transition history, idempotency
   keys, observations, anomalies, incidents, RCA evidence, and delivery
   attempts. Public event endpoints accept CI/CD, dashboard, and harness events;
   out-of-order events are retained and reconciled deterministically.
5. **Lineage and impact**: model graph, supported column lineage, changed and
   downstream models, contracts, owners, and KPIs are snapshotted per review
   and deployment. Unsupported dialect/column paths are disclosed as such.
6. **RCA engine**: deterministic ranking favors removed/changed invariants,
   dependency and contract changes, KPI/grain changes, then related columns,
   models, and metadata anomalies. It distinguishes cause, contributor,
   downstream symptom, and unrelated concurrent change. An LLM may explain
   stored evidence only; it cannot create findings or choose the root cause.
7. **Delivery adapters**: dashboard API contracts, GitHub comment/check/incident
   links, and redacted deduplicated Slack alerts consume the same evidence IDs.
   Delivery success is independent per channel.

## Lifecycle state machine

```text
reviewed -> approved -> deployment_started -> deployment_succeeded
         -> post_deployment_monitoring -> healthy

deployment_started -> deployment_failed
post_deployment_monitoring -> post_deployment_anomaly -> incident_open
incident_open -> rolled_back -> post_deployment_monitoring
incident_open -> incident_resolved
```

Every transition carries deployment ID, repository, PR/base/head/merge SHAs,
manifest/schema/baseline hashes, changed/affected models, KPIs, decision,
timestamps, monitoring status, anomaly IDs, rollback status, and outcome.

## Required evidence policy defaults

Pre-merge requires immutable base/head manifests, complete changed-file
comparison, and changed-model resolution when dbt models changed. Declared
contracts/KPIs are required only when declared for that model; history is
optional; warehouse metadata is optional unless configured as a required gate.

Post-deployment requires deployment identity/commit binding and configured
monitoring checks. Slack and dashboard publication are optional. RCA requires
the incident signal, deployment history for attribution, and evidence supporting
the primary cause. Complete lineage is optional unless exhaustive impact is
claimed; missing optional evidence lowers and discloses confidence.

## Release boundary

The first release targets deterministic local lifecycle E2E and live GitHub
interfaces. It does not claim permanent hosting, production storage/queues,
multi-worker safety, a production warehouse, or real Slack until those gates
are separately authorized and proven.

