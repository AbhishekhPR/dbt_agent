-- Durable workspace/account lifecycle orchestration. External calls are never
-- made inside these transactions. Rows remain tenant/user linked only while
-- work is retryable; completion leaves a dissociated receipt.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE tenant_lifecycle_controls
    ADD COLUMN workspace_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE tenant_lifecycle_controls
    ADD COLUMN work_admission_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE tenant_lifecycle_controls
    ADD CONSTRAINT tenant_lifecycle_workspace_state_check
    CHECK (workspace_state IN ('active', 'frozen', 'deleting'));
ALTER TABLE tenant_lifecycle_controls
    ADD CONSTRAINT tenant_lifecycle_work_admission_check
    CHECK (work_admission_state IN ('active', 'blocked'));

CREATE TABLE IF NOT EXISTS workspace_lifecycle_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    initiated_by_clerk_user_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('delete_workspace')),
    phase TEXT NOT NULL CHECK (phase IN (
        'requested', 'frozen', 'billing_reconciliation', 'billing_revoked',
        'github_access_revocation', 'credentials_revoked', 'artifact_purge',
        'database_purge', 'clerk_organization_deletion', 'local_finalize',
        'completed')),
    disposition TEXT NOT NULL CHECK (disposition IN
        ('running', 'retryable', 'blocked', 'completed')),
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
    lease_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    failure_category TEXT,
    confirmation_verified_at TIMESTAMPTZ NOT NULL,
    billing_terminal_verified_at TIMESTAMPTZ,
    github_terminal_verified_at TIMESTAMPTZ,
    credentials_revoked_at TIMESTAMPTZ,
    artifact_files_deleted BIGINT NOT NULL DEFAULT 0
        CHECK (artifact_files_deleted >= 0),
    operational_records_deleted BIGINT NOT NULL DEFAULT 0
        CHECK (operational_records_deleted >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (length(operation_id) BETWEEN 1 AND 255),
    CHECK (length(initiated_by_clerk_user_id) BETWEEN 1 AND 255),
    CHECK (lease_id IS NULL OR length(lease_id) BETWEEN 1 AND 255),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 100),
    CHECK ((disposition = 'completed') = (completed_at IS NOT NULL)),
    CONSTRAINT workspace_lifecycle_operation_tenant_unique
        UNIQUE (operation_id, tenant_id)
);

CREATE UNIQUE INDEX uq_workspace_lifecycle_active_tenant
    ON workspace_lifecycle_operations (tenant_id)
    WHERE completed_at IS NULL;

CREATE TABLE IF NOT EXISTS workspace_lifecycle_provider_results (
    operation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('polar', 'github', 'clerk')),
    target_kind TEXT NOT NULL,
    target_reference TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN
        ('accepted', 'verified_terminal', 'verified_absent', 'retryable', 'blocked')),
    failure_category TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, provider, target_kind, target_reference),
    FOREIGN KEY (operation_id, tenant_id)
        REFERENCES workspace_lifecycle_operations (operation_id, tenant_id)
        ON DELETE CASCADE,
    CHECK (length(target_kind) BETWEEN 1 AND 64),
    CHECK (length(target_reference) BETWEEN 1 AND 255),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS account_lifecycle_operations (
    operation_id TEXT PRIMARY KEY,
    clerk_user_id TEXT NOT NULL,
    dissociated_actor_ref TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('delete_account')),
    phase TEXT NOT NULL CHECK (phase IN (
        'requested', 'membership_inventory', 'leaving_workspaces',
        'credentials_revoked', 'clerk_user_deletion', 'local_finalize',
        'completed')),
    disposition TEXT NOT NULL CHECK (disposition IN
        ('running', 'retryable', 'blocked', 'completed')),
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
    lease_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    failure_category TEXT,
    confirmation_verified_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (length(operation_id) BETWEEN 1 AND 255),
    CHECK (length(clerk_user_id) BETWEEN 1 AND 255),
    CHECK (dissociated_actor_ref ~ '^deleted_actor_[0-9a-f]{32}$'),
    CHECK (lease_id IS NULL OR length(lease_id) BETWEEN 1 AND 255),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 100),
    CHECK ((disposition = 'completed') = (completed_at IS NOT NULL))
);

CREATE UNIQUE INDEX uq_account_lifecycle_active_user
    ON account_lifecycle_operations (clerk_user_id)
    WHERE completed_at IS NULL;

CREATE TABLE clerk_membership_departure_guards (
    clerk_organization_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('leave_workspace','delete_account')),
    operation_id TEXT NOT NULL,
    clerk_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (length(clerk_organization_id) BETWEEN 1 AND 255),
    CHECK (length(operation_id) BETWEEN 1 AND 255),
    CHECK (length(clerk_user_id) BETWEEN 1 AND 255)
);

-- Local connection state is distinct from GitHub's installation state. Rows
-- remain as historical ownership evidence and can be reconnected only through
-- the verified onboarding flow.
ALTER TABLE tenant_repositories ADD COLUMN disconnected_at TIMESTAMPTZ;
ALTER TABLE tenant_repositories ADD COLUMN disconnect_reason TEXT;
ALTER TABLE tenant_repositories ADD CONSTRAINT tenant_repository_disconnect_check
    CHECK ((disconnected_at IS NULL) = (disconnect_reason IS NULL));
ALTER TABLE tenant_github_installations ADD COLUMN disconnected_at TIMESTAMPTZ;
ALTER TABLE tenant_github_installations ADD COLUMN github_absence_verified_at TIMESTAMPTZ;

CREATE TABLE github_access_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    initiated_by_clerk_user_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN
        ('repository_disconnect','installation_disconnect','installation_uninstall')),
    github_repository_id BIGINT,
    github_installation_id BIGINT,
    phase TEXT NOT NULL CHECK (phase IN ('local_revoked','provider_revocation','completed')),
    disposition TEXT NOT NULL CHECK (disposition IN
        ('running','retryable','blocked','completed')),
    failure_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (length(operation_id) BETWEEN 1 AND 255),
    CHECK (length(initiated_by_clerk_user_id) BETWEEN 1 AND 255),
    CHECK ((operation_kind='repository_disconnect') = (github_repository_id IS NOT NULL)),
    CHECK ((operation_kind IN ('installation_disconnect','installation_uninstall')) =
           (github_installation_id IS NOT NULL)),
    CHECK ((disposition='completed') = (completed_at IS NOT NULL)),
    UNIQUE (operation_id, tenant_id)
);
CREATE UNIQUE INDEX uq_github_access_active_repository
    ON github_access_operations (tenant_id, github_repository_id)
    WHERE completed_at IS NULL AND github_repository_id IS NOT NULL;
CREATE UNIQUE INDEX uq_github_access_active_installation
    ON github_access_operations (tenant_id, github_installation_id)
    WHERE completed_at IS NULL AND github_installation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS account_lifecycle_memberships (
    operation_id TEXT NOT NULL REFERENCES account_lifecycle_operations (operation_id)
        ON DELETE CASCADE,
    clerk_organization_id TEXT NOT NULL,
    clerk_membership_id TEXT NOT NULL,
    authoritative_role TEXT NOT NULL CHECK (authoritative_role IN
        ('owner', 'admin', 'member')),
    active_owner_count INTEGER NOT NULL CHECK (active_owner_count > 0),
    state TEXT NOT NULL CHECK (state IN
        ('verified_shared', 'leave_accepted', 'verified_absent')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, clerk_organization_id),
    CHECK (length(clerk_organization_id) BETWEEN 1 AND 255),
    CHECK (length(clerk_membership_id) BETWEEN 1 AND 255)
);

-- Serialize departures from the same Clerk organization. Without this, two
-- owners could both observe an owner count of two and concurrently leave.
CREATE UNIQUE INDEX uq_account_lifecycle_membership_departure
    ON account_lifecycle_memberships (clerk_organization_id)
    WHERE state <> 'verified_absent';

CREATE TABLE IF NOT EXISTS deletion_receipts (
    receipt_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN
        ('delete_workspace', 'delete_account')),
    requested_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    billing_terminal_verified BOOLEAN NOT NULL,
    provider_access_terminal_verified BOOLEAN NOT NULL,
    credentials_revoked BOOLEAN NOT NULL,
    operational_records_deleted BIGINT NOT NULL DEFAULT 0
        CHECK (operational_records_deleted >= 0),
    artifact_files_deleted BIGINT NOT NULL DEFAULT 0
        CHECK (artifact_files_deleted >= 0),
    warehouse_cleanup_required BOOLEAN NOT NULL DEFAULT FALSE,
    github_actions_cleanup_required BOOLEAN NOT NULL DEFAULT FALSE,
    historical_external_records_retained BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (length(receipt_id) BETWEEN 1 AND 255),
    CHECK (completed_at >= requested_at)
);

-- Preserve append-only manifests during normal operation. The lifecycle
-- engine may delete one mapped tenant's manifests only while holding its
-- deleting control row and setting a transaction-local tenant guard. The old
-- tombstone path remains solely for backwards compatibility and is never used
-- by the new lifecycle engine.
CREATE OR REPLACE FUNCTION relium_reject_manifest_evidence_mutation()
RETURNS TRIGGER AS $relium_manifest_immutable$
DECLARE
    purge_tenant TEXT;
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM retention_tombstones
        WHERE organization_id = OLD.organization_id
    ) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'DELETE' THEN
        purge_tenant := current_setting('relium.lifecycle_purge_tenant', true);
        IF purge_tenant IS NOT NULL AND purge_tenant <> '' AND EXISTS (
            SELECT 1
            FROM tenant_operational_roots ownership
            JOIN tenant_lifecycle_controls control
              ON control.tenant_id = ownership.tenant_id
            WHERE ownership.organization_id = OLD.organization_id
              AND ownership.tenant_id = purge_tenant
              AND control.workspace_state = 'deleting'
        ) THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION
        'manifest evidence is immutable; submit evidence for a different commit instead'
        USING ERRCODE = 'restrict_violation';
END;
$relium_manifest_immutable$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION relium_reject_snapshot_mutation()
RETURNS TRIGGER AS $relium_immutable$
DECLARE
    purge_tenant TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        purge_tenant := current_setting('relium.lifecycle_purge_tenant', true);
        IF purge_tenant IS NOT NULL AND purge_tenant <> '' AND EXISTS (
            SELECT 1
            FROM tenant_operational_roots ownership
            JOIN tenant_lifecycle_controls control
              ON control.tenant_id = ownership.tenant_id
            WHERE ownership.organization_id = OLD.organization_id
              AND ownership.tenant_id = purge_tenant
              AND control.workspace_state = 'deleting'
        ) THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION
        'metadata snapshots are immutable; submit a new snapshot instead (table %)',
        TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$relium_immutable$ LANGUAGE plpgsql;
