-- Collector reachability is not warehouse connectivity.
-- Keep the two clocks separate so the dashboard cannot call an environment
-- connected merely because ensure_tenant(), token issuance, or registration
-- touched it.
ALTER TABLE collector_identities
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;
ALTER TABLE collector_identities
    ADD COLUMN IF NOT EXISTS last_failed_at TIMESTAMPTZ;
ALTER TABLE collector_identities
    ADD COLUMN IF NOT EXISTS verification_status TEXT;
ALTER TABLE collector_identities
    ADD COLUMN IF NOT EXISTS verification_error_category TEXT;

ALTER TABLE collector_identities
    DROP CONSTRAINT IF EXISTS collector_identities_verification_status_check;
ALTER TABLE collector_identities
    ADD CONSTRAINT collector_identities_verification_status_check
    CHECK (verification_status IS NULL OR verification_status IN ('verified', 'failed'));

CREATE INDEX IF NOT EXISTS idx_collector_identities_verified
    ON collector_identities
       (organization_id, repository_id, environment, last_verified_at DESC)
    WHERE revoked = FALSE;
