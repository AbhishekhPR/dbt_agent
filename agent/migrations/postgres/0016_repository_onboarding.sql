-- Repository selection, dbt configuration, and CI credential state.
--
-- ###################################################################
-- # A REPOSITORY IS ADDRESSED BY ITS NUMERIC GITHUB ID.             #
-- ###################################################################
--
-- `github_repository_id` is the identity. `owner_login` and `name` are display
-- metadata, kept so the UI and the CI variables can be rendered without a
-- round trip to GitHub, and joined on by nothing.
--
-- A repository can be renamed, and a freed name can be claimed by somebody
-- else the same day. Authorizing on a name would mean an attacker who takes a
-- released name inherits whatever Relium had configured for it. The numeric id
-- is issued by GitHub, never reused, and never chosen by the caller.
--
-- The row is reachable only through the tenant's own installation:
--
--   tenant -> tenant_github_installations -> tenant_repositories
--
-- so "which repositories may this tenant configure" is answered by a join, not
-- by trusting a value from a request.

CREATE TABLE IF NOT EXISTS tenant_repositories (
    -- GitHub's own repository id. PRIMARY KEY, so one repository belongs to
    -- exactly one Relium tenant — the same rule, and the same enforcement, as
    -- tenant_github_installations in migration 0015.
    github_repository_id  BIGINT PRIMARY KEY,

    tenant_id             TEXT NOT NULL
                          REFERENCES tenants (tenant_id) ON DELETE CASCADE,

    -- Which installation granted access. NOT NULL and a foreign key, because
    -- a repository configured through an installation that has since been
    -- removed must not stay reachable: dropping the installation drops this.
    github_installation_id BIGINT NOT NULL
                          REFERENCES github_installations (github_installation_id)
                          ON DELETE CASCADE,

    -- Display metadata. Refreshed from GitHub whenever the repository is
    -- listed, so a rename shows up. Never an identity.
    owner_login           TEXT NOT NULL,
    name                  TEXT NOT NULL,
    default_branch        TEXT,
    private               BOOLEAN,

    -- ---- dbt configuration -------------------------------------------
    --
    -- `project_dir` is NOT a relium.yml key. It is the CI variable
    -- RELIUM_DBT_PROJECT_DIR, and it is stored separately for exactly that
    -- reason: writing it into the YAML would produce a file the backend's own
    -- loader rejects, because _ALLOWED_KEYS does not contain it.
    project_dir           TEXT,
    -- Repository-relative POSIX, validated by the same
    -- validate_repository_relative_path the GitHub App uses to read the file.
    manifest_path         TEXT,
    enforcement_mode      TEXT,

    -- Whether a dbt_project.yml was actually seen, and where. Recorded rather
    -- than assumed, so the UI can distinguish "no dbt project" from "not
    -- looked yet" instead of rendering a guess.
    dbt_detected          BOOLEAN,
    dbt_project_dir       TEXT,
    dbt_checked_at        TIMESTAMPTZ,

    -- ---- CI credential state ------------------------------------------
    --
    -- STATE ONLY. The token itself is never stored here, or anywhere: the
    -- existing issue_ci_token keeps sha256(secret) on api_service_tokens and
    -- nothing else. These columns record that a token exists, which one it is
    -- (by its non-secret id), and how it reached the customer.
    ci_token_id           TEXT,
    ci_token_issued_at    TIMESTAMPTZ,
    ci_token_delivery     TEXT,

    selected_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    configured_at         TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenant_repositories_id_positive
        CHECK (github_repository_id > 0),
    CONSTRAINT tenant_repositories_owner_bounded
        CHECK (length(btrim(owner_login)) BETWEEN 1 AND 100),
    CONSTRAINT tenant_repositories_name_bounded
        CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    -- The two values the real relium.yml loader accepts, and nothing else.
    CONSTRAINT tenant_repositories_enforcement_mode_check
        CHECK (enforcement_mode IS NULL
               OR enforcement_mode IN ('shadow', 'enforce')),
    -- How the CI token reached the customer. `actions_secret` means Relium
    -- wrote it straight into the repository and it never entered a browser;
    -- `display_once` means it was shown to the person exactly once.
    CONSTRAINT tenant_repositories_ci_delivery_check
        CHECK (ci_token_delivery IS NULL
               OR ci_token_delivery IN ('actions_secret', 'display_once')),
    -- A path that fails this could never be read back by the GitHub App, so
    -- storing one would produce a configuration that silently never works.
    CONSTRAINT tenant_repositories_manifest_path_relative
        CHECK (manifest_path IS NULL
               OR (manifest_path !~ '^/' AND manifest_path !~ '^[A-Za-z]:'
                   AND position('\' in manifest_path) = 0
                   AND manifest_path !~ '(^|/)\.\.(/|$)'
                   AND manifest_path !~ '(^|/)\.(/|$)'
                   AND manifest_path !~ '//' AND manifest_path !~ '/$')),
    CONSTRAINT tenant_repositories_project_dir_relative
        CHECK (project_dir IS NULL OR project_dir = '.'
               OR (project_dir !~ '^/' AND project_dir !~ '^[A-Za-z]:'
                   AND position('\' in project_dir) = 0
                   AND project_dir !~ '(^|/)\.\.(/|$)'
                   AND project_dir !~ '//' AND project_dir !~ '/$'))
);

CREATE INDEX IF NOT EXISTS idx_tenant_repositories_tenant
    ON tenant_repositories (tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_repositories_installation
    ON tenant_repositories (github_installation_id);


-- Onboarding completion.
--
-- `tenant_onboarding_state.completed_at` already exists from migration 0014
-- and is the source of truth; no new column is needed for it. What is added
-- here is the record of WHICH repository the tenant finished on, so a
-- completion can be audited without inferring it from timestamps.
ALTER TABLE tenant_onboarding_state
    ADD COLUMN IF NOT EXISTS completed_repository_id BIGINT;

ALTER TABLE tenant_onboarding_state
    ADD COLUMN IF NOT EXISTS completed_by_clerk_user_id TEXT;
