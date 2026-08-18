-- Clerk-identified tenants, and durable first-run onboarding state.
--
-- WHY A NEW TENANT TABLE
-- ----------------------
-- Until now a "tenant" was the pair (organization_id, repository_id), bound in
-- deployment configuration because the pilot served one design partner. That
-- cannot represent a customer who has signed in but not yet connected a
-- repository — which is precisely the state every new customer starts in.
--
-- `tenants` is the workspace-level owner: one row per customer organization,
-- created the moment they finish the first onboarding step, before any
-- repository exists. The existing `repositories` rows become children of it in
-- a later phase; this migration deliberately does not re-parent them, because
-- doing so would rewrite the tenancy of live pilot data in the same change
-- that introduces the concept.
--
-- IDENTITY IS AN IMMUTABLE IDENTIFIER, NEVER A NAME
-- -------------------------------------------------
-- `clerk_organization_id` is the identity. It is opaque, issued by Clerk, and
-- does not change when a customer renames their organization.
--
-- `organization_name` is a label. It is editable, it is not unique, and
-- nothing joins on it. A GitHub organization name is not stored here at all:
-- a Clerk Organization and a GitHub Organization are unrelated objects, and
-- matching them by name would let anyone who can create a GitHub organization
-- with a chosen name attach themselves to someone else's tenant.
--
-- The UNIQUE constraint on `clerk_organization_id` is what makes workspace
-- creation idempotent, including under concurrency. It is enforced by the
-- database rather than by a read-then-write in application code, because two
-- simultaneous first-time requests would both read "no tenant" and both
-- insert.

CREATE TABLE IF NOT EXISTS tenants (
    -- Relium's own identifier, and the only one the API exposes. Deliberately
    -- not the Clerk id: our resource identifiers should not change if we ever
    -- change identity provider, and they should not leak which Clerk
    -- organization a tenant corresponds to.
    tenant_id             TEXT PRIMARY KEY,

    -- The identity. One Clerk organization maps to exactly one tenant.
    clerk_organization_id TEXT NOT NULL UNIQUE,

    -- Display only. Never an identifier, never joined on.
    organization_name     TEXT NOT NULL,

    -- Self-declared context from the setup form. Optional, low-cardinality,
    -- and stored because onboarding asks for it — nothing more is collected.
    role                  TEXT,
    team_size             TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenants_tenant_id_format
        CHECK (tenant_id ~ '^ten_[0-9a-f]{32}$'),
    CONSTRAINT tenants_clerk_organization_id_bounded
        CHECK (length(clerk_organization_id) BETWEEN 1 AND 255),
    -- Bounded, and non-blank rather than merely non-null: a name of
    -- whitespace is not a name.
    --
    -- The character set is given explicitly. btrim's default strips SPACES
    -- ONLY, so `btrim(organization_name)` would accept a name of tabs — which
    -- is exactly what a test caught here.
    CONSTRAINT tenants_organization_name_bounded
        CHECK (length(btrim(organization_name, E' \t\n\r\f\v')) BETWEEN 1 AND 200),
    CONSTRAINT tenants_role_bounded
        CHECK (role IS NULL OR length(role) BETWEEN 1 AND 100),
    CONSTRAINT tenants_team_size_bounded
        CHECK (team_size IS NULL OR length(team_size) BETWEEN 1 AND 40)
);


-- Durable onboarding progress, on the server, keyed by tenant.
--
-- This is the source of truth for "has this customer finished setup". Not
-- Clerk publicMetadata, which the browser can read and which would put a
-- routing decision inside the identity provider; not localStorage, which the
-- customer can edit and which does not survive a new device.
--
-- One row per tenant, created with the tenant.
CREATE TABLE IF NOT EXISTS tenant_onboarding_state (
    tenant_id     TEXT PRIMARY KEY
                  REFERENCES tenants (tenant_id) ON DELETE CASCADE,

    -- The furthest step reached. A CHECK constraint rather than free text, so
    -- an unknown step is rejected by the database instead of being stored and
    -- rendered as a broken screen. The vocabulary matches the onboarding UI
    -- exactly: workspace -> github -> repository -> dbt -> ready.
    --
    -- 'workspace' is absent on purpose: a row cannot exist before the tenant
    -- does, and the tenant is created by finishing the workspace step. The
    -- workspace state is represented by having no tenant at all.
    current_step  TEXT NOT NULL DEFAULT 'github',

    -- Null until onboarding is completed. Set once, by the completion
    -- endpoint, in a later phase.
    completed_at  TIMESTAMPTZ,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenant_onboarding_state_step_check
        CHECK (current_step IN ('github', 'repository', 'dbt', 'ready')),

    -- Completion and step cannot disagree. Without this, a bug could mark a
    -- tenant complete while it still points at an unfinished step, and the
    -- router would then have two contradictory answers to choose between.
    CONSTRAINT tenant_onboarding_state_completion_consistent
        CHECK (completed_at IS NULL OR current_step = 'ready')
);

-- Answering "which tenants have finished setup" without scanning.
CREATE INDEX IF NOT EXISTS idx_tenant_onboarding_state_incomplete
    ON tenant_onboarding_state (tenant_id)
    WHERE completed_at IS NULL;
