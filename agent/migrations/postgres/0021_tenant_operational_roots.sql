-- Authoritative bridge from Clerk workspaces to the legacy operational root.
--
-- A legacy organization is deliberately allowed to remain unmapped. Existing
-- pilot data has no trustworthy tenant identifier, and a mutable GitHub login,
-- repository name, email, billing row, or onboarding answer is not ownership
-- evidence. An absent row therefore means "not reconciled", never "free to
-- guess".

-- Fail quickly rather than extending a production write outage if another
-- transaction is holding either installation projection. The data backfill
-- lives in 0022 so these DDL locks are released before any legacy scan starts.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE tenant_operational_roots (
    organization_id TEXT PRIMARY KEY
                    REFERENCES organizations (organization_id) ON DELETE RESTRICT,
    tenant_id       TEXT NOT NULL
                    REFERENCES tenants (tenant_id) ON DELETE RESTRICT,

    mapping_basis   TEXT NOT NULL,
    source_github_repository_id BIGINT,
    source_github_installation_id BIGINT,
    source_ci_token_id TEXT,

    established_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at     TIMESTAMPTZ NOT NULL,

    CONSTRAINT tenant_operational_roots_basis_check
        CHECK (mapping_basis IN (
            'ci_token_binding', 'verified_github_repository'
        )),
    CONSTRAINT tenant_operational_roots_provenance_check
        CHECK (
            (mapping_basis = 'ci_token_binding'
             AND source_ci_token_id IS NOT NULL
             AND source_github_repository_id IS NOT NULL)
            OR
            (mapping_basis = 'verified_github_repository'
             AND source_github_repository_id IS NOT NULL
             AND source_github_installation_id IS NOT NULL)
        )
);

CREATE INDEX idx_tenant_operational_roots_tenant
    ON tenant_operational_roots (tenant_id);

-- A repository and its installation must name the same tenant. NOT VALID
-- preserves service for historical rows until the audit command identifies
-- and an operator reconciles them, while PostgreSQL enforces the relationship
-- for every new or updated row immediately.
ALTER TABLE tenant_github_installations
    ADD CONSTRAINT tenant_github_installations_installation_tenant_unique
    UNIQUE (github_installation_id, tenant_id);

ALTER TABLE tenant_repositories
    ADD CONSTRAINT tenant_repositories_installation_tenant_fk
    FOREIGN KEY (github_installation_id, tenant_id)
    REFERENCES tenant_github_installations (github_installation_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;

ALTER TABLE tenant_repository_dbt_detection
    ADD CONSTRAINT tenant_repository_detection_installation_tenant_fk
    FOREIGN KEY (github_installation_id, tenant_id)
    REFERENCES tenant_github_installations (github_installation_id, tenant_id)
    ON DELETE CASCADE
    NOT VALID;
