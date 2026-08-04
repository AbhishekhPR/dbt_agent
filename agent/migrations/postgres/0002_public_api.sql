-- Public lifecycle and dashboard API layer, migration 0002.
-- Adds service-token authentication, the reviews resource surfaced by the
-- dashboard contract, and the idempotency columns the write APIs need.

-- Service tokens are never stored in plaintext. A token is presented as
-- "rlm_<token_id>.<secret>"; only sha256(secret) is persisted, and the
-- comparison is done in constant time by the application.
CREATE TABLE IF NOT EXISTS api_service_tokens (
    token_id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY (organization_id, repository_id) REFERENCES repositories (organization_id, repository_id)
);
CREATE INDEX IF NOT EXISTS idx_api_service_tokens_tenant
    ON api_service_tokens (organization_id, repository_id);

-- Pull-request review decisions, surfaced by the dashboard review resources.
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    pull_number INTEGER,
    commit_sha TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('ALLOW', 'WARN', 'BLOCK', 'NEUTRAL')),
    enforcement_mode TEXT CHECK (enforcement_mode IN ('shadow', 'enforce')),
    risk_score INTEGER,
    evidence_coverage TEXT CHECK (evidence_coverage IN ('COMPLETE', 'INCOMPLETE', 'UNKNOWN')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_reviews_tenant
    ON reviews (organization_id, repository_id, environment, created_at DESC);

-- Idempotency support for externally retried writes. The payload hash lets the
-- API distinguish a faithful replay from a conflicting reuse of the same key.
ALTER TABLE event_receipts ADD COLUMN IF NOT EXISTS payload_hash TEXT;
ALTER TABLE event_receipts ADD COLUMN IF NOT EXISTS resource_kind TEXT;
ALTER TABLE event_receipts ADD COLUMN IF NOT EXISTS resource_id TEXT;

CREATE INDEX IF NOT EXISTS idx_event_receipts_tenant
    ON event_receipts (organization_id, repository_id, environment);

-- Observations carry both observed-at (reported by the caller) and received-at
-- (assigned by the server) so late events are identifiable rather than silently
-- reordered.
ALTER TABLE monitoring_observations ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE monitoring_observations ADD COLUMN IF NOT EXISTS evidence_coverage TEXT;
ALTER TABLE monitoring_observations ADD COLUMN IF NOT EXISTS source TEXT;

-- Baselines record their own evidence completeness and source identity.
ALTER TABLE metadata_baselines ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE metadata_baselines ADD COLUMN IF NOT EXISTS evidence_coverage TEXT;
ALTER TABLE metadata_baselines ADD COLUMN IF NOT EXISTS source TEXT;

-- Anomalies carry severity, detection time and affected entities.
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS affected_models JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS affected_kpis JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS observation_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

-- RCA reports carry remediation guidance and lineage/evidence qualifiers that
-- the incident detail response must disclose.
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS attributed_deployment_id TEXT;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS deployment_candidates JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS affected_model TEXT;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS downstream_models JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS affected_kpis JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS remediation JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS rollback_recommendation TEXT;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS verification_steps JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS lineage_level TEXT;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS lineage_completeness TEXT;
ALTER TABLE rca_reports ADD COLUMN IF NOT EXISTS evidence_coverage TEXT;
