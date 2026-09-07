-- Authoritative Clerk organization membership projected into Relium.
--
-- This migration intentionally inserts no rows. Existing workspaces are not
-- assigned owners from application data: a complete Clerk organization
-- snapshot must establish membership before sensitive authorization can pass.

CREATE TABLE IF NOT EXISTS tenant_membership_sync_state (
    tenant_id               TEXT PRIMARY KEY
                            REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    sync_generation         TEXT NOT NULL,
    sync_status             TEXT NOT NULL,
    ownership_status        TEXT NOT NULL,
    active_owner_count      INTEGER NOT NULL DEFAULT 0,
    source_version          TEXT,
    source_fingerprint      TEXT,
    last_attempted_at       TIMESTAMPTZ NOT NULL,
    last_synchronized_at    TIMESTAMPTZ,
    failure_category        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenant_membership_sync_generation_bounded
        CHECK (length(sync_generation) BETWEEN 1 AND 255),
    CONSTRAINT tenant_membership_sync_status_check
        CHECK (sync_status IN ('never_synced', 'refreshing', 'synchronized',
                               'failed', 'ambiguous')),
    CONSTRAINT tenant_membership_ownership_status_check
        CHECK (ownership_status IN ('authoritative', 'ambiguous')),
    CONSTRAINT tenant_membership_owner_count_nonnegative
        CHECK (active_owner_count >= 0),
    CONSTRAINT tenant_membership_source_version_bounded
        CHECK (source_version IS NULL OR length(source_version) BETWEEN 1 AND 255),
    CONSTRAINT tenant_membership_source_fingerprint_format
        CHECK (source_fingerprint IS NULL OR source_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT tenant_membership_failure_category_bounded
        CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 100),
    CONSTRAINT tenant_membership_sync_success_has_timestamp
        CHECK (sync_status NOT IN ('synchronized', 'ambiguous')
               OR last_synchronized_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS tenant_memberships (
    tenant_id               TEXT NOT NULL
                            REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    clerk_user_id           TEXT NOT NULL,
    clerk_membership_id     TEXT NOT NULL,
    role                    TEXT NOT NULL,
    clerk_role_key          TEXT NOT NULL,
    role_basis              TEXT NOT NULL,
    source_version          TEXT,
    source_updated_at       TIMESTAMPTZ,
    sync_generation         TEXT NOT NULL,
    status                  TEXT NOT NULL,
    synchronized_at         TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, clerk_user_id),
    CONSTRAINT tenant_memberships_membership_unique
        UNIQUE (tenant_id, clerk_membership_id),
    CONSTRAINT tenant_memberships_user_bounded
        CHECK (length(clerk_user_id) BETWEEN 1 AND 255),
    CONSTRAINT tenant_memberships_membership_bounded
        CHECK (length(clerk_membership_id) BETWEEN 1 AND 255),
    CONSTRAINT tenant_memberships_role_check
        CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT tenant_memberships_clerk_role_bounded
        CHECK (length(clerk_role_key) BETWEEN 1 AND 255),
    CONSTRAINT tenant_memberships_role_basis_check
        CHECK (role_basis IN ('explicit_clerk_role', 'organization_creator',
                              'clerk_membership')),
    CONSTRAINT tenant_memberships_source_version_bounded
        CHECK (source_version IS NULL OR length(source_version) BETWEEN 1 AND 255),
    CONSTRAINT tenant_memberships_generation_bounded
        CHECK (length(sync_generation) BETWEEN 1 AND 255),
    CONSTRAINT tenant_memberships_status_check
        CHECK (status IN ('active', 'removed'))
);

CREATE INDEX IF NOT EXISTS idx_tenant_memberships_active_role
    ON tenant_memberships (tenant_id, role, clerk_user_id)
    WHERE status = 'active';
