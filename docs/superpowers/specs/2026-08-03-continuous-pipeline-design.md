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

A repository/organization-scoped SQLite lifecycle store is the local reference
implementation. It has append-only transition history, immutable evidence
references, idempotency keys, restart-safe event processing, and explicit
out-of-order handling. Warehouse access uses a provider interface; SQLite is
the deterministic E2E adapter. A production warehouse adapter is not claimed
without an intended warehouse, credentials, and explicit authorization.

The public lifecycle surface accepts CI/CD callbacks, dashboard actions, and
test-harness events. Internal methods are not sufficient evidence for live E2E.

## Detector and RCA design

SQL detectors are typed registry entries with dialect support, positive and
negative fixtures, raw/compiled attribution, safe-equivalent rewrites,
limitations, remediation, owner, severity, and evidence links. Resulting-data
comparison is authoritative for risks static analysis cannot prove.

RCA is deterministic and ranks invariant/contract/KPI/grain/dependency changes
above related columns/models and generic implementation details. It emits
primary cause, alternatives, contributors, symptoms, unrelated concurrent
changes, confidence, unevaluated evidence, remediation, rollback advice, and
verification steps. An LLM can explain persisted evidence but cannot create
findings or select a root cause.

## Validation contract

Every local lifecycle scenario runs twice with isolated storage and normalized
evidence comparison. Live scenarios must originate from GitHub PR webhooks and
use public deployment/monitoring interfaces. Reports distinguish static SQL,
semantic, metadata, real-warehouse, lifecycle, RCA, GitHub, Slack, and hosted
production coverage. Production Release Ready is prohibited until permanent
hosting, durable production storage/queue, multi-worker safety, warehouse
integration, monitoring/rollback, and security/recovery gates are proven.

