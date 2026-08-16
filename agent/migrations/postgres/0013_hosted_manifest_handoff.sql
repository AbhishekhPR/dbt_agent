-- Hosted CI manifest handoff.
--
-- GitHub webhooks can arrive before CI has compiled target/manifest.json, and
-- generated artifacts are commonly absent from the repository entirely. This
-- table is the immutable, tenant-scoped bridge between those two deliveries.

ALTER TABLE api_service_tokens
    DROP CONSTRAINT IF EXISTS api_service_tokens_scope_check;
ALTER TABLE api_service_tokens
    ADD CONSTRAINT api_service_tokens_scope_check
    CHECK (scope IN ('collector', 'operator_read', 'ci'));

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_lifecycle_state_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_lifecycle_state_check
    CHECK (lifecycle_state IN (
        'RECEIVED',
        'WAITING_FOR_MANIFEST',
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

CREATE TABLE manifest_evidence (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest JSONB NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, commit_sha),
    UNIQUE (organization_id, repository_id, evidence_id),
    UNIQUE (organization_id, repository_id, idempotency_key),
    FOREIGN KEY (organization_id, repository_id)
        REFERENCES repositories (organization_id, repository_id),
    CHECK (commit_sha ~ '^[0-9A-Fa-f]{40}([0-9A-Fa-f]{24})?$'),
    CHECK (length(manifest_hash) = 64),
    CHECK (jsonb_typeof(manifest) = 'object')
);

CREATE INDEX idx_manifest_evidence_received
    ON manifest_evidence (organization_id, repository_id, received_at DESC);

CREATE OR REPLACE FUNCTION relium_reject_manifest_evidence_mutation()
RETURNS TRIGGER AS $relium_manifest_immutable$
BEGIN
    -- The application writes the retention tombstone first and deletes the
    -- tenant in the same transaction. That is the sole legitimate delete;
    -- ordinary correction remains append-only by commit SHA.
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM retention_tombstones
        WHERE organization_id = OLD.organization_id
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'manifest evidence is immutable; submit evidence for a different commit instead'
        USING ERRCODE = 'restrict_violation';
END;
$relium_manifest_immutable$ LANGUAGE plpgsql;

CREATE TRIGGER trg_manifest_evidence_immutable
    BEFORE UPDATE OR DELETE ON manifest_evidence
    FOR EACH ROW EXECUTE FUNCTION relium_reject_manifest_evidence_mutation();
