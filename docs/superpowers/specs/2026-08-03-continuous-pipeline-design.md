# Relium Continuous Pipeline Design

## Goal

Extend Relium from a validated pre-merge GitHub reviewer into a traceable
continuous pipeline that follows reviewed analytics changes through deployment,
read-only metadata monitoring, anomaly detection, deployment attribution,
downstream impact, deterministic RCA, and independent GitHub/dashboard/Slack
delivery.

## Scope decomposition

The work is intentionally split into independently verifiable releases:

1. detector coverage and evidence-policy contract;
2. cardinality-collapse and provider-neutral metadata monitoring;
3. durable deployment lifecycle and public event API;
4. lineage/impact persistence and RCA ranking;
5. dashboard/delivery contracts and local lifecycle E2E;
6. live GitHub lifecycle validation and security/operations reporting.

Each release must preserve the prior evidence pack and may report
UNSUPPORTED or BLOCKED BY CREDENTIALS explicitly. No release claims arbitrary
SQL semantic correctness.

## Decision and evidence model

Health is the aggregate of evaluated finding severity only. Evidence coverage is
separate and is computed from repository policy declarations. Missing required
material evidence yields WARN in shadow mode and BLOCK in enforce mode, without
changing health. Optional evidence is NOT EVALUATED with no decision escalation
or health adjustment. Unsupported detectors remain UNSUPPORTED and only block
when configured as required. Credential-blocked optional integrations have no
effect on core review decisions. Every decision and RCA claim references an
immutable evidence record.

## Persistence and interfaces

PostgreSQL is the authoritative lifecycle/evidence store. Every row is scoped
to organization, repository, and environment, and authorization checks those
three keys on every read/write. Evidence references are immutable, transition
history is append-only, and idempotency keys make event handling restart-safe.
The local SQLite adapter is a deterministic E2E compatibility store only; it is
not the production source of truth.

Lifecycle writes use a transactional outbox: the state/evidence mutation and an
outbox row are committed in one PostgreSQL transaction. A publisher leases
outbox rows, emits the public event, records the delivery attempt, and retries
with bounded backoff. Consumers use event IDs and version checks so duplicate
and out-of-order events are retained but cannot silently overwrite newer state.

Repository policy, detector definitions, and threshold sets are immutable
versioned records. Every decision, observation, anomaly, incident, RCA, and
delivery journal references the exact policy/detector/threshold version used.

Warehouse access uses a provider interface; SQLite is the deterministic E2E
adapter. A production warehouse adapter is not claimed without an intended
warehouse, credentials, explicit authorization, allowlisted relations, bounded
timeouts, statement and result-size limits, read-only credentials, and audited
query fingerprints. Customer-facing evidence contains redacted query metadata,
never unrestricted SQL.

The public lifecycle surface accepts CI/CD callbacks, dashboard actions, and
test-harness events. Internal methods are not sufficient evidence for live E2E.

## Detector and RCA design

SQL detectors are typed registry entries with dialect support, positive and
negative fixtures, raw/compiled attribution, safe-equivalent rewrites,
limitations, remediation, owner, severity, and evidence links. Resulting-data
comparison is authoritative for risks static analysis cannot prove, but only
within the observed model, time window, sample/partition scope, and declared
keys. It cannot establish behavior outside that scope, prove arbitrary SQL
equivalence, or replace a missing contract. A resulting-data finding must name
the compared base/head observations, query scope, completeness, and any late or
backfilled data excluded from the comparison.

RCA is deterministic and ranks invariant/contract/KPI/grain/dependency changes
above related columns/models and generic implementation details. It emits
primary cause, alternatives, contributors, symptoms, unrelated concurrent
changes, confidence, unevaluated evidence, remediation, rollback advice, and
verification steps. RCA confidence is independent of review health and uses
`HIGH` (causal evidence directly binds the anomaly to one deployment), `MEDIUM`
(strong temporal/lineage correlation with a remaining causal gap), `LOW`
(plausible correlation only), or `UNATTRIBUTED` (no responsible deployment can
be supported). The engine must state the causality evidence required for a
primary cause: deployment identity bound to reviewed commit, temporal ordering,
affected-model/KPI path, and a matching observed change or invariant breach.
When any required link is absent it may rank alternatives but must emit an
unattributed RCA instead of inventing causation. An LLM can explain persisted
evidence but cannot create findings or select a root cause.

Lineage snapshots carry independent completeness metadata for model, column,
and KPI lineage: `COMPLETE`, `PARTIAL`, or `UNAVAILABLE`, with dialect,
manifest hash, unresolved nodes, and the reason for every gap. Exhaustive
impact claims are valid only with complete lineage at the claimed level.

Monitoring records the deployment-relative observation window, schedule, source
watermark, event time, ingestion time, late-event allowance, backfill marker,
and rollback association. Late observations and backfills are append-only and
may revise anomaly status through a new transition; they never rewrite the
original observation. Rollback observations must prove the rollback deployment
identity and record signal recovery or continued failure.

The exact deployment transitions are:

`reviewed -> approved -> deployment_started -> deployment_succeeded ->
post_deployment_monitoring -> healthy`.

Failure transitions are `deployment_started -> deployment_failed`,
`post_deployment_monitoring -> post_deployment_anomaly -> incident_open`,
`incident_open -> rolled_back -> post_deployment_monitoring`, and
`incident_open -> incident_resolved`. Invalid transitions are rejected and
recorded as evidence; duplicate valid events are idempotent.

The exact incident transitions are `incident_open -> incident_acknowledged ->
incident_investigating -> incident_mitigated -> incident_resolved`, with
`incident_investigating -> incident_open` for a reopened incident and
`incident_mitigated -> rolled_back` when rollback is the mitigation. Anomaly
records remain immutable even when incident status changes.

GitHub, dashboard, and Slack each have an independent delivery journal with
idempotency key, payload hash, attempt count, redaction result, status, and
bounded retry/dead-letter state. One channel's failure never marks another
channel successful or changes the core decision.

Evidence retention is organization/repository/environment scoped and policy
controlled. Retention expiry creates an auditable tombstone rather than
silently deleting references. Repository disconnect stops new processing,
revokes delivery credentials, and retains evidence until its policy expires.
Customer deletion removes tenant data, outbox payloads, and delivery journals
after an auditable deletion request; only minimal non-customer operational
records needed to prove deletion remain.

Migration from the filesystem pilot is one-way and bounded: freeze new writes,
export and checksum every job/publication/evidence record, import into staging
PostgreSQL with tenant keys and schema version, reconcile counts/hashes, dual-
read for a verification window, then switch the API writer. The original pilot
files remain preserved as historical evidence and are never treated as the
authoritative store after cutover.

## Validation contract

Every local lifecycle scenario runs twice with isolated storage and normalized
evidence comparison. Live scenarios must originate from GitHub PR webhooks and
use public deployment/monitoring interfaces. Reports distinguish static SQL,
semantic, metadata, real-warehouse, lifecycle, RCA, GitHub, Slack, and hosted
production coverage. Production Release Ready is prohibited until permanent
hosting, durable production storage/queue, multi-worker safety, warehouse
integration, monitoring/rollback, and security/recovery gates are proven.
