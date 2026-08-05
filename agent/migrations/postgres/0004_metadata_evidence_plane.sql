-- 0004_metadata_evidence_plane.sql
--
-- Makes production metadata a first-class input to pre-deployment PR review.
--
-- Design notes that matter for review:
--
-- 1. The existing `reviews` table is EXTENDED rather than replaced. There is
--    exactly one authoritative review system after this migration. The
--    filesystem store in agent/github_app/storage.py keeps only its delivery
--    de-duplication role; it is no longer the authoritative review state.
--
-- 2. Lifecycle state and final decision are separate columns. `decision` is
--    relaxed to allow NULL, which means "not yet decided" - a review that is
--    waiting for production metadata has a lifecycle state but no decision.
--    Overloading one column would make "waiting" indistinguishable from a
--    verdict, which is exactly the failure mode this release exists to remove.
--
-- 3. Review recomputation reuses the proven outbox queue instead of adding a
--    second durable queue. outbox_events gains a subject (deployment | review);
--    the claim/lease/dead-letter/expired-lease-recovery machinery that was
--    validated under load stays untouched and unduplicated.
--    `review_recomputation_jobs` is exposed as a view over that queue.
--
-- 4. Snapshots are immutable, enforced by a trigger rather than convention.
--    A correction requires a new snapshot.
--
-- 5. Every key and foreign key is tenant scoped. No cross-tenant snapshot or
--    review binding is expressible.

-- =====================================================================
-- 1. REVIEW LIFECYCLE
-- =====================================================================

-- Lifecycle state, tracked independently from the final decision.
ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'RECEIVED';

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_lifecycle_state_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_lifecycle_state_check
    CHECK (lifecycle_state IN (
        'RECEIVED',
        'CODE_ANALYSIS_COMPLETE',
        'METADATA_NOT_REQUIRED',
        'METADATA_REQUESTED',
        'WAITING_FOR_METADATA',
        'METADATA_PARTIAL',
        'METADATA_COMPLETE',
        'METADATA_STALE',
        'DECISION_READY',
        'PUBLISHED',
        'FAILED'
    ));

-- The final decision must be independently representable, including the
-- "not yet decided" case. NULL is that case.
ALTER TABLE reviews ALTER COLUMN decision DROP NOT NULL;
ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_decision_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_decision_check
    CHECK (decision IS NULL OR decision IN ('ALLOW', 'WARN', 'BLOCK', 'SKIP', 'NEUTRAL'));

-- Immutable git and artifact binding.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS base_sha TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS base_manifest_hash TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS head_manifest_hash TEXT;

-- Policy identity and health, kept separate from coverage.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS policy_hash TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS health INTEGER;

-- Review attempt identity. Attempt 1 is the first pass; a recomputation
-- driven by an arriving snapshot increments it.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;

-- Sticky GitHub publication identity, so recomputation updates the SAME
-- comment and the SAME check run rather than publishing duplicates.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS github_comment_id TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS github_check_run_id TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS github_delivery_id TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS metadata_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- A pull request has at most one live review per head SHA.
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_pr_head
    ON reviews (organization_id, repository_id, pull_number, head_sha)
    WHERE pull_number IS NOT NULL AND head_sha IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reviews_lifecycle
    ON reviews (organization_id, repository_id, lifecycle_state);

-- ---------------------------------------------------------------------
-- Every attempt is preserved. Recomputation never overwrites history.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_attempts (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    lifecycle_state TEXT NOT NULL,
    decision TEXT,
    evidence_coverage TEXT,
    health INTEGER,
    enforcement_mode TEXT,
    policy_version TEXT,
    policy_hash TEXT,
    trigger TEXT NOT NULL,
    snapshot_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, review_id, attempt),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    CHECK (decision IS NULL OR decision IN ('ALLOW', 'WARN', 'BLOCK', 'SKIP', 'NEUTRAL')),
    CHECK (trigger IN ('initial', 'metadata_snapshot', 'timeout', 'manual', 'redelivery'))
);

CREATE TABLE IF NOT EXISTS review_lifecycle_transitions (
    transition_id BIGINT GENERATED ALWAYS AS IDENTITY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, transition_id),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_review_transitions_review
    ON review_lifecycle_transitions (organization_id, repository_id, review_id);

-- =====================================================================
-- 2. COLLECTOR IDENTITY
-- =====================================================================

CREATE TABLE IF NOT EXISTS collector_identities (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    collector_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    token_id TEXT,
    collector_version TEXT,
    adapter_type TEXT,
    description TEXT,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at TIMESTAMPTZ,
    revoked_reason TEXT,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, collector_id),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment),
    CHECK (revoked = FALSE OR revoked_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_collector_identities_env
    ON collector_identities (organization_id, repository_id, environment)
    WHERE revoked = FALSE;

-- =====================================================================
-- 3. TARGETED COLLECTION REQUESTS
-- =====================================================================

CREATE TABLE IF NOT EXISTS collection_requests (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    review_id TEXT,
    deployment_id TEXT,
    base_sha TEXT,
    head_sha TEXT,
    base_manifest_hash TEXT,
    head_manifest_hash TEXT,
    reason TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'standard',
    required_evidence_level TEXT NOT NULL DEFAULT 'schema',
    state TEXT NOT NULL DEFAULT 'PENDING',
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failure_reason TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, request_id),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    CHECK (priority IN ('critical', 'standard', 'low')),
    CHECK (required_evidence_level IN ('schema', 'profile', 'full')),
    CHECK (state IN ('PENDING', 'ACKNOWLEDGED', 'COMPLETED', 'PARTIAL', 'FAILED', 'EXPIRED'))
);

CREATE INDEX IF NOT EXISTS idx_collection_requests_pending
    ON collection_requests (organization_id, repository_id, environment, state, priority)
    WHERE state IN ('PENDING', 'ACKNOWLEDGED');

CREATE INDEX IF NOT EXISTS idx_collection_requests_review
    ON collection_requests (organization_id, repository_id, review_id);

-- The bounded target list. A request names exactly the relations it needs,
-- so a pull request never triggers a full warehouse scan.
CREATE TABLE IF NOT EXISTS collection_request_targets (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    target_index INTEGER NOT NULL,
    model_unique_id TEXT,
    relation_database TEXT,
    relation_schema TEXT,
    relation_name TEXT NOT NULL,
    columns JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_signals JSONB NOT NULL DEFAULT '[]'::jsonb,
    dependency_kind TEXT NOT NULL DEFAULT 'external',
    criticality TEXT NOT NULL DEFAULT 'standard',
    PRIMARY KEY (organization_id, repository_id, request_id, target_index),
    FOREIGN KEY (organization_id, repository_id, request_id)
        REFERENCES collection_requests (organization_id, repository_id, request_id)
        ON DELETE CASCADE,
    -- 'external' means the relation must already exist in production.
    -- 'head_derived' means the head graph produces it inside this PR, so its
    -- absence from current production is expected and must not be a failure.
    CHECK (dependency_kind IN ('external', 'head_derived', 'internal')),
    CHECK (criticality IN ('critical', 'standard', 'low'))
);

-- =====================================================================
-- 4. IMMUTABLE METADATA SNAPSHOTS
-- =====================================================================

CREATE TABLE IF NOT EXISTS metadata_snapshots (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    collector_id TEXT,
    collector_version TEXT,
    adapter_type TEXT,
    request_id TEXT,
    review_id TEXT,
    deployment_id TEXT,
    base_sha TEXT,
    head_sha TEXT,
    production_deployment_sha TEXT,
    base_manifest_hash TEXT,
    head_manifest_hash TEXT,
    configuration_version TEXT,
    completeness TEXT NOT NULL DEFAULT 'COMPLETE',
    freshness_state TEXT NOT NULL DEFAULT 'CURRENT',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    ttl_seconds INTEGER,
    observed_at TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, repository_id, snapshot_id),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment),
    FOREIGN KEY (organization_id, repository_id, request_id)
        REFERENCES collection_requests (organization_id, repository_id, request_id),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id),
    FOREIGN KEY (organization_id, repository_id, collector_id)
        REFERENCES collector_identities (organization_id, repository_id, collector_id),
    CHECK (completeness IN ('COMPLETE', 'PARTIAL', 'FAILED')),
    CHECK (freshness_state IN ('CURRENT', 'STALE', 'PARTIALLY_STALE', 'UNKNOWN'))
);

-- Per-tenant idempotency: the same key with a different payload is a conflict.
CREATE UNIQUE INDEX IF NOT EXISTS uq_metadata_snapshots_idempotency
    ON metadata_snapshots (organization_id, repository_id, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_metadata_snapshots_review
    ON metadata_snapshots (organization_id, repository_id, review_id);

CREATE INDEX IF NOT EXISTS idx_metadata_snapshots_freshness
    ON metadata_snapshots (organization_id, repository_id, environment, observed_at DESC);

-- ---------------------------------------------------------------------
-- Immutability is enforced, not merely documented. A correction must be a
-- new snapshot; silent mutation of accepted evidence is not possible.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION relium_reject_snapshot_mutation()
RETURNS TRIGGER AS $relium_immutable$
BEGIN
    RAISE EXCEPTION
        'metadata snapshots are immutable; submit a new snapshot instead (table %)',
        TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$relium_immutable$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_metadata_snapshots_immutable ON metadata_snapshots;
CREATE TRIGGER trg_metadata_snapshots_immutable
    BEFORE UPDATE OR DELETE ON metadata_snapshots
    FOR EACH ROW EXECUTE FUNCTION relium_reject_snapshot_mutation();

-- ---------------------------------------------------------------------
-- Relation, column and metric observations
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshot_relations (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    relation_index INTEGER NOT NULL,
    model_unique_id TEXT,
    relation_database TEXT,
    relation_schema TEXT,
    relation_name TEXT NOT NULL,
    relation_type TEXT,
    exists_in_production BOOLEAN NOT NULL DEFAULT TRUE,
    schema_fingerprint TEXT,
    row_count BIGINT,
    freshness_timestamp TIMESTAMPTZ,
    freshness_lag_seconds BIGINT,
    lineage_level TEXT,
    lineage_completeness TEXT,
    dbt_run_status TEXT,
    dbt_test_status TEXT,
    dbt_execution_ms BIGINT,
    collection_status TEXT NOT NULL DEFAULT 'COLLECTED',
    unevaluated_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    collection_error TEXT,
    observed_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, repository_id, snapshot_id, relation_index),
    FOREIGN KEY (organization_id, repository_id, snapshot_id)
        REFERENCES metadata_snapshots (organization_id, repository_id, snapshot_id)
        ON DELETE CASCADE,
    CHECK (collection_status IN ('COLLECTED', 'PARTIAL', 'UNSUPPORTED', 'FAILED', 'SKIPPED')),
    CHECK (lineage_completeness IS NULL
           OR lineage_completeness IN ('complete', 'incomplete', 'unknown'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_relations_name
    ON snapshot_relations (organization_id, repository_id, relation_name);

CREATE TABLE IF NOT EXISTS snapshot_columns (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    relation_index INTEGER NOT NULL,
    column_index INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    data_type TEXT,
    is_nullable BOOLEAN,
    ordinal_position INTEGER,
    null_count BIGINT,
    null_rate DOUBLE PRECISION,
    duplicate_count BIGINT,
    duplicate_rate DOUBLE PRECISION,
    distinct_count BIGINT,
    cardinality BIGINT,
    min_value TEXT,
    max_value TEXT,
    collection_status TEXT NOT NULL DEFAULT 'COLLECTED',
    PRIMARY KEY (organization_id, repository_id, snapshot_id, relation_index, column_index),
    FOREIGN KEY (organization_id, repository_id, snapshot_id, relation_index)
        REFERENCES snapshot_relations
            (organization_id, repository_id, snapshot_id, relation_index)
        ON DELETE CASCADE,
    CHECK (null_rate IS NULL OR (null_rate >= 0 AND null_rate <= 1)),
    CHECK (duplicate_rate IS NULL OR (duplicate_rate >= 0 AND duplicate_rate <= 1)),
    CHECK (collection_status IN ('COLLECTED', 'PARTIAL', 'UNSUPPORTED', 'FAILED', 'SKIPPED'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_columns_lookup
    ON snapshot_columns (organization_id, repository_id, snapshot_id, column_name);

-- Bounded scalar metrics only. Raw rows are never stored; the metric
-- expression is recorded as a fingerprint rather than as warehouse SQL.
CREATE TABLE IF NOT EXISTS snapshot_metrics (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    metric_index INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    model_unique_id TEXT,
    relation_name TEXT,
    metric_value DOUBLE PRECISION,
    metric_text TEXT,
    expression_fingerprint TEXT,
    collection_status TEXT NOT NULL DEFAULT 'COLLECTED',
    observed_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, repository_id, snapshot_id, metric_index),
    FOREIGN KEY (organization_id, repository_id, snapshot_id)
        REFERENCES metadata_snapshots (organization_id, repository_id, snapshot_id)
        ON DELETE CASCADE,
    CHECK (collection_status IN ('COLLECTED', 'PARTIAL', 'UNSUPPORTED', 'FAILED', 'SKIPPED')),
    CHECK (metric_text IS NULL OR length(metric_text) <= 256)
);

-- =====================================================================
-- 5. SNAPSHOT-TO-REVIEW BINDING
-- =====================================================================
-- The binding is a separate row so that acceptance, rejection and the reason
-- for rejection are all durable and auditable.

CREATE TABLE IF NOT EXISTS snapshot_review_bindings (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    request_id TEXT,
    binding_state TEXT NOT NULL,
    rejection_reason TEXT,
    base_sha_match BOOLEAN,
    head_sha_match BOOLEAN,
    manifest_hash_match BOOLEAN,
    freshness_state TEXT,
    completeness TEXT,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, review_id, snapshot_id),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, repository_id, snapshot_id)
        REFERENCES metadata_snapshots (organization_id, repository_id, snapshot_id)
        ON DELETE CASCADE,
    CHECK (binding_state IN ('ACCEPTED', 'REJECTED', 'SUPERSEDED'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_bindings_review
    ON snapshot_review_bindings (organization_id, repository_id, review_id, binding_state);

-- =====================================================================
-- 6. PER-REVIEW EVIDENCE COVERAGE
-- =====================================================================
-- One row per evidence source per review attempt, so "which of the three
-- evidence states were available" is a stored fact rather than a rendering.

CREATE TABLE IF NOT EXISTS review_evidence_coverage (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    evidence_source TEXT NOT NULL,
    requirement TEXT NOT NULL,
    state TEXT NOT NULL,
    evidence_state_group TEXT,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, review_id, attempt, evidence_source),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    CHECK (requirement IN ('required', 'optional', 'disabled')),
    CHECK (state IN ('EVALUATED', 'MISSING', 'FAILED', 'NOT EVALUATED',
                     'UNSUPPORTED', 'STALE', 'BLOCKED BY CREDENTIALS')),
    -- which of the three product states this source belongs to
    CHECK (evidence_state_group IS NULL
           OR evidence_state_group IN ('base_code', 'head_code', 'production'))
);

-- =====================================================================
-- 7. DURABLE REVIEW RECOMPUTATION (reuses the proven outbox)
-- =====================================================================

ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL DEFAULT 'deployment';
ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS subject_id TEXT;

-- Existing rows are deployment-subject rows; backfill before tightening.
UPDATE outbox_events SET subject_id = deployment_id WHERE subject_id IS NULL;

-- Backward compatibility: for a deployment-subject row the subject IS the
-- deployment, so deriving subject_id from deployment_id is a definition
-- rather than a convenience. This keeps every pre-existing writer working
-- against the generalised table without a coordinated rewrite, which is what
-- makes this a compatible extension rather than a breaking change.
CREATE OR REPLACE FUNCTION relium_default_outbox_subject()
RETURNS TRIGGER AS $relium_subject$
BEGIN
    IF NEW.subject_id IS NULL THEN
        NEW.subject_id := NEW.deployment_id;
    END IF;
    RETURN NEW;
END;
$relium_subject$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_outbox_default_subject ON outbox_events;
CREATE TRIGGER trg_outbox_default_subject
    BEFORE INSERT ON outbox_events
    FOR EACH ROW EXECUTE FUNCTION relium_default_outbox_subject();

ALTER TABLE outbox_events ALTER COLUMN subject_id SET NOT NULL;
ALTER TABLE outbox_events ALTER COLUMN deployment_id DROP NOT NULL;

ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_subject_type_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_subject_type_check
    CHECK (subject_type IN ('deployment', 'review'));

-- A deployment-subject event must still carry its deployment id.
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_deployment_subject_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_deployment_subject_check
    CHECK (subject_type <> 'deployment' OR deployment_id IS NOT NULL);

-- Replace the deployment-scoped uniqueness with subject-scoped uniqueness.
-- This keeps exactly-once semantics for both subject kinds.
ALTER TABLE outbox_events
    DROP CONSTRAINT IF EXISTS outbox_events_organization_id_repository_id_environment_depl_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_events_subject
    ON outbox_events (organization_id, repository_id, environment,
                      subject_type, subject_id, event_type);

CREATE INDEX IF NOT EXISTS idx_outbox_events_claimable
    ON outbox_events (organization_id, repository_id, environment, state, next_attempt_at);

-- Named view required by the release contract. It is a view rather than a
-- second table so there is exactly one durable queue and one set of
-- claim/lease/dead-letter semantics.
CREATE OR REPLACE VIEW review_recomputation_jobs AS
    SELECT
        event_id,
        organization_id,
        repository_id,
        environment,
        subject_id AS review_id,
        event_type,
        payload,
        state,
        lease_owner,
        lease_expires_at,
        attempts,
        next_attempt_at,
        last_error,
        created_at,
        completed_at
    FROM outbox_events
    WHERE subject_type = 'review';

-- =====================================================================
-- 8. GUARD RAILS
-- =====================================================================
-- Refuse to complete if any cross-tenant binding already exists. This
-- mirrors the guard used by 0003 and makes a silent cross-tenant link
-- impossible to introduce by migration.

DO $relium_guard$
DECLARE
    offending INTEGER;
BEGIN
    SELECT count(*) INTO offending
    FROM snapshot_review_bindings b
    JOIN metadata_snapshots s
      ON s.snapshot_id = b.snapshot_id
    WHERE s.organization_id <> b.organization_id
       OR s.repository_id <> b.repository_id;

    IF offending > 0 THEN
        RAISE EXCEPTION
            'refusing to migrate: % cross-tenant snapshot bindings found', offending;
    END IF;
END
$relium_guard$;
