-- Durable, non-selection dbt detection cache for onboarding listings.
CREATE TABLE IF NOT EXISTS tenant_repository_dbt_detection (
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    github_repository_id BIGINT NOT NULL,
    github_installation_id BIGINT NOT NULL,
    owner_login TEXT NOT NULL,
    name TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    private BOOLEAN NOT NULL DEFAULT FALSE,
    dbt_detected BOOLEAN,
    dbt_project_dir TEXT,
    dbt_checked_at TIMESTAMPTZ,
    dbt_checked_commit_sha TEXT,
    PRIMARY KEY (tenant_id, github_repository_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_repository_dbt_detection_repo
    ON tenant_repository_dbt_detection (github_repository_id);
