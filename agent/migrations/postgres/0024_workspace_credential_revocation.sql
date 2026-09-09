-- Tenant-scoped credential/access revocation. This migration adds no deletion
-- or purge behavior and retains all review/evidence/history records.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS workspace_credential_revocations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    initiated_by_clerk_user_id TEXT NOT NULL,
    generation BIGINT NOT NULL CHECK (generation > 0),
    state TEXT NOT NULL CHECK (state IN ('claimed', 'revoked', 'failed')),
    failure_category TEXT,
    service_tokens_revoked INTEGER NOT NULL DEFAULT 0 CHECK (service_tokens_revoked >= 0),
    collector_identities_revoked INTEGER NOT NULL DEFAULT 0
        CHECK (collector_identities_revoked >= 0),
    collection_requests_canceled INTEGER NOT NULL DEFAULT 0
        CHECK (collection_requests_canceled >= 0),
    outbox_events_canceled INTEGER NOT NULL DEFAULT 0
        CHECK (outbox_events_canceled >= 0),
    dashboard_sessions_revoked INTEGER NOT NULL DEFAULT 0
        CHECK (dashboard_sessions_revoked >= 0),
    installation_states_consumed INTEGER NOT NULL DEFAULT 0
        CHECK (installation_states_consumed >= 0),
    claimed_work_remaining INTEGER NOT NULL DEFAULT 0
        CHECK (claimed_work_remaining >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, generation),
    CHECK (length(operation_id) BETWEEN 1 AND 255),
    CHECK (length(initiated_by_clerk_user_id) BETWEEN 1 AND 255),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 64)
);

ALTER TABLE api_service_tokens ALTER COLUMN secret_hash DROP NOT NULL;
ALTER TABLE api_service_tokens
    ADD CONSTRAINT api_service_tokens_active_digest_check
    CHECK (revoked_at IS NOT NULL OR secret_hash IS NOT NULL) NOT VALID;
UPDATE api_service_tokens SET secret_hash = NULL
WHERE revoked_at IS NOT NULL AND secret_hash IS NOT NULL;
ALTER TABLE api_service_tokens
    VALIDATE CONSTRAINT api_service_tokens_active_digest_check;

ALTER TABLE collection_requests ADD COLUMN IF NOT EXISTS canceled_at TIMESTAMPTZ;
ALTER TABLE collection_requests ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;
ALTER TABLE collection_requests DROP CONSTRAINT IF EXISTS collection_requests_state_check;
ALTER TABLE collection_requests ADD CONSTRAINT collection_requests_state_check
    CHECK (state IN ('PENDING', 'ACKNOWLEDGED', 'COMPLETED', 'PARTIAL',
                     'FAILED', 'EXPIRED', 'CANCELED'));

ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS canceled_at TIMESTAMPTZ;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_state_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_state_check
    CHECK (state IN ('PENDING', 'CLAIMED', 'COMPLETED', 'DEAD_LETTER', 'CANCELED'));

ALTER TABLE dashboard_sessions
    ADD COLUMN IF NOT EXISTS source_clerk_user_id TEXT;
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_clerk_user
    ON dashboard_sessions (source_clerk_user_id)
    WHERE source_clerk_user_id IS NOT NULL AND revoked_at IS NULL;

ALTER TABLE tenant_repositories
    DROP CONSTRAINT IF EXISTS tenant_repositories_ci_token_fk;
ALTER TABLE tenant_repositories
    ADD CONSTRAINT tenant_repositories_ci_token_fk
    FOREIGN KEY (ci_token_id) REFERENCES api_service_tokens (token_id)
    ON DELETE SET NULL NOT VALID;

ALTER TABLE collector_identities
    DROP CONSTRAINT IF EXISTS collector_identities_token_fk;
ALTER TABLE collector_identities
    ADD CONSTRAINT collector_identities_token_fk
    FOREIGN KEY (token_id) REFERENCES api_service_tokens (token_id)
    ON DELETE SET NULL NOT VALID;
