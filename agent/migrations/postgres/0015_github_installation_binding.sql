-- Binding a Relium tenant to a GitHub App installation, verifiably.
--
-- ###################################################################
-- # THE THREAT THIS SCHEMA EXISTS TO DEFEAT                         #
-- ###################################################################
--
-- After someone installs the GitHub App, GitHub redirects their browser to the
-- App's Setup URL with `?installation_id=...`. GitHub's own documentation warns
-- that this query parameter can be spoofed. Anyone can type a number into a
-- URL.
--
-- So a row in `tenant_github_installations` is never created from that number.
-- It requires three independent facts, and the schema is shaped so that each
-- one has somewhere to live:
--
--   1. WHO STARTED THIS FLOW      github_installation_states
--      An opaque, single-use, expiring value minted before the redirect and
--      bound server-side to a tenant and a Clerk user. The browser carries
--      only the value; the tenant is never in it.
--
--   2. WHAT THE INSTALLATION IS   github_installations
--      Facts read back from GitHub with the App's own credential, not from the
--      browser. This table is tenant-agnostic on purpose: the webhook can
--      populate it before any tenant is known, which is exactly what happens
--      when the delivery beats the redirect.
--
--   3. THAT THE HUMAN IS REALLY   clerk_github_identities
--      ASSOCIATED WITH IT         + a live check against GitHub as that user.
--      A verified link between a Clerk user and a GitHub user, established by
--      GitHub OAuth. It is what makes a forged installation_id useless: an
--      attacker can name any installation, but cannot make GitHub list one
--      they have no access to under their own user token.
--
-- Splitting facts (2) from the binding (4th table) is what makes the two
-- arrival orders converge without either one guessing. The webhook records
-- what it knows — the installation — and stays silent about tenancy, which it
-- genuinely cannot determine: nothing in a webhook payload names a Relium
-- tenant, and matching on account login would let anyone who can create a
-- GitHub organization with a chosen name attach themselves to someone else's
-- workspace.


-- ---------------------------------------------------------------------------
-- 1. Installation-flow state
-- ---------------------------------------------------------------------------
--
-- Same shape and the same reasoning as `oauth_states` in migration 0009: the
-- value never touches the database, only its SHA-256 does, so a database
-- disclosure cannot be replayed as a live flow.
--
-- Deliberately NOT a JWT. A signed token would put the tenant id in something
-- the browser holds and would have to be revoked out-of-band to be single-use.
-- An opaque row is single-use by an UPDATE.
CREATE TABLE IF NOT EXISTS github_installation_states (
    installation_state_id TEXT PRIMARY KEY,

    -- SHA-256 of the value handed to the browser. UNIQUE so a hash collision
    -- or a duplicate insert cannot produce two rows one lookup could match.
    state_hash            TEXT NOT NULL UNIQUE,

    -- Who this flow belongs to. Both come from a verified Clerk session at
    -- mint time and are read back from HERE at consume time — never from the
    -- redirect, which is entirely attacker-controlled.
    tenant_id             TEXT NOT NULL
                          REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    clerk_user_id         TEXT NOT NULL,

    -- What the state may be spent on. One value today; present so a second
    -- flow cannot later reuse a state minted for this one.
    purpose               TEXT NOT NULL DEFAULT 'github_app_install',

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at            TIMESTAMPTZ NOT NULL,

    -- Set exactly once, by a conditional UPDATE. That is what makes
    -- consumption single-use and safe under concurrency: two simultaneous
    -- redirects race on one row and PostgreSQL lets exactly one win.
    consumed_at           TIMESTAMPTZ,

    -- Two flows use this table, and a state minted for one must not be
    -- spendable on the other: consuming always names the purpose, so an
    -- identity-link state cannot be replayed to complete an installation.
    CONSTRAINT github_installation_states_purpose_check
        CHECK (purpose IN ('github_app_install', 'github_identity_link')),
    CONSTRAINT github_installation_states_expiry_after_creation
        CHECK (expires_at > created_at),
    CONSTRAINT github_installation_states_id_format
        CHECK (installation_state_id ~ '^ist_[0-9a-f]{32}$'),
    CONSTRAINT github_installation_states_hash_format
        CHECK (state_hash ~ '^[0-9a-f]{64}$')
);

-- For expiring old rows, and for finding a tenant's outstanding attempts.
CREATE INDEX IF NOT EXISTS idx_github_installation_states_open
    ON github_installation_states (tenant_id, expires_at)
    WHERE consumed_at IS NULL;


-- ---------------------------------------------------------------------------
-- 2. Verified Clerk-user to GitHub-user links
-- ---------------------------------------------------------------------------
--
-- Established only by completing GitHub OAuth as that user. Nothing here is
-- inferred from an email address, a display name, an organization name or an
-- installation account — all of which are either mutable, unverified, or
-- attacker-chosen.
CREATE TABLE IF NOT EXISTS clerk_github_identities (
    clerk_user_id       TEXT PRIMARY KEY,

    -- The immutable identifier. A login can be renamed and reused by someone
    -- else; the numeric id cannot.
    github_user_id      BIGINT NOT NULL,
    -- Display only. Never joined on, never an identity.
    github_login        TEXT NOT NULL,

    -- The user credential, encrypted by the application before it reaches the
    -- database, exactly as dashboard_sessions does. It never reaches the
    -- browser. It exists so Relium can ask GitHub, AS THE USER, which
    -- installations they can actually see — the question that makes a forged
    -- installation_id worthless.
    access_token        BYTEA,
    access_expires_at   TIMESTAMPTZ,
    refresh_token       BYTEA,
    refresh_expires_at  TIMESTAMPTZ,

    linked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ,

    CONSTRAINT clerk_github_identities_user_id_positive
        CHECK (github_user_id > 0),
    CONSTRAINT clerk_github_identities_login_bounded
        CHECK (length(btrim(github_login)) BETWEEN 1 AND 100)
);

-- Deliberately NOT unique. One person may hold more than one Clerk account and
-- link the same GitHub identity to each; refusing that would break a legitimate
-- case while preventing no attack, since a link cannot be created without
-- completing OAuth as that GitHub user.
CREATE INDEX IF NOT EXISTS idx_clerk_github_identities_github_user
    ON clerk_github_identities (github_user_id);


-- ---------------------------------------------------------------------------
-- 3. Installation facts, tenant-agnostic
-- ---------------------------------------------------------------------------
--
-- What GitHub says about an installation, from the webhook or from the App
-- API. No tenant column: this table must be writable when the tenant is not
-- known, which is the case for every webhook delivery.
CREATE TABLE IF NOT EXISTS github_installations (
    github_installation_id BIGINT PRIMARY KEY,

    -- Which App the installation belongs to. Recorded so an installation of a
    -- DIFFERENT GitHub App can be recognised and refused rather than bound.
    github_app_id          BIGINT,

    github_account_id      BIGINT NOT NULL,
    github_account_login   TEXT NOT NULL,
    github_account_type    TEXT NOT NULL,

    repository_selection   TEXT,

    status                 TEXT NOT NULL DEFAULT 'active',
    suspended_at           TIMESTAMPTZ,
    -- Soft delete. Uninstalling stops reviews; it does not erase the history
    -- of what Relium did while it was installed.
    deleted_at             TIMESTAMPTZ,

    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT github_installations_id_positive
        CHECK (github_installation_id > 0),
    CONSTRAINT github_installations_account_id_positive
        CHECK (github_account_id > 0),
    CONSTRAINT github_installations_status_check
        CHECK (status IN ('active', 'suspended', 'deleted')),
    CONSTRAINT github_installations_account_type_check
        CHECK (github_account_type IN ('User', 'Organization')),
    CONSTRAINT github_installations_repository_selection_check
        CHECK (repository_selection IS NULL
               OR repository_selection IN ('all', 'selected'))
);

-- An account login is NOT unique: an account can uninstall and reinstall,
-- producing a second installation id for the same account, and both rows are
-- legitimate history.
CREATE INDEX IF NOT EXISTS idx_github_installations_account
    ON github_installations (github_account_id);


-- ---------------------------------------------------------------------------
-- 4. The binding
-- ---------------------------------------------------------------------------
--
-- The only table that says an installation belongs to a tenant, and the only
-- one written after all three verifications pass.
CREATE TABLE IF NOT EXISTS tenant_github_installations (
    -- PRIMARY KEY on the installation id is the enforcement of "each GitHub
    -- installation belongs to exactly one Relium tenant". A second tenant
    -- claiming it is refused by the database, not by application logic.
    github_installation_id  BIGINT PRIMARY KEY
                            REFERENCES github_installations (github_installation_id)
                            ON DELETE CASCADE,

    -- No UNIQUE here: a tenant may hold many installations, which is the
    -- normal case for a customer with repositories under both a personal
    -- account and an organization.
    tenant_id               TEXT NOT NULL
                            REFERENCES tenants (tenant_id) ON DELETE CASCADE,

    -- The audit trail for why this binding was allowed to exist.
    bound_by_clerk_user_id  TEXT NOT NULL,
    -- The GitHub user whose own credential proved association with the
    -- installation. Numeric, immutable, and verified — not a login.
    verified_github_user_id BIGINT NOT NULL,
    -- Which single-use state authorised it. Kept for audit; the state row may
    -- later be pruned, so this is not a foreign key.
    bound_via_state_id      TEXT,

    bound_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenant_github_installations_verified_user_positive
        CHECK (verified_github_user_id > 0)
);

CREATE INDEX IF NOT EXISTS idx_tenant_github_installations_tenant
    ON tenant_github_installations (tenant_id);
