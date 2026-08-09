-- Human dashboard sessions, and the capability boundary between a person and
-- a machine.
--
-- Before this, the dashboard authenticated with a tenant service token
-- compiled into its JavaScript bundle. That credential could approve and
-- revoke governance exceptions, and anyone who could load the page could read
-- it out of the bundle. The `actor` recorded against those actions came from
-- the request body, so it was whatever the caller typed.
--
-- A session here represents one authenticated GitHub user, holding one
-- verified repository permission, for one tenant.

-- The session id itself is never stored. The browser holds it in a cookie and
-- the server keeps only its SHA-256, so a database disclosure cannot be
-- replayed as a live session.
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    session_id_hash          TEXT PRIMARY KEY,
    organization_id          TEXT NOT NULL,
    repository_id            TEXT NOT NULL,
    environment              TEXT,

    github_login             TEXT NOT NULL,
    github_user_id           BIGINT,

    -- The highest permission GitHub reported for this user on this repository,
    -- and the decision derived from it. Both are stored: the decision is what
    -- the API enforces, the raw value is what an audit needs to see.
    github_permission        TEXT NOT NULL,
    may_govern               BOOLEAN NOT NULL,
    permission_checked_at    TIMESTAMPTZ NOT NULL,

    -- GitHub user credentials, encrypted by the application before they reach
    -- the database. They never leave the server and are never sent to the
    -- browser. Null when the App issues non-expiring tokens and no refresh
    -- token exists.
    github_access_token      BYTEA,
    github_access_expires_at TIMESTAMPTZ,
    github_refresh_token     BYTEA,
    github_refresh_expires_at TIMESTAMPTZ,

    -- Paired with the session cookie for double-submit CSRF. Useless on its
    -- own: a request must carry both this and the session cookie.
    csrf_token               TEXT NOT NULL,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at               TIMESTAMPTZ NOT NULL,
    revoked_at               TIMESTAMPTZ,
    revocation_reason        TEXT,

    FOREIGN KEY (organization_id, repository_id)
        REFERENCES repositories (organization_id, repository_id)
);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_tenant
    ON dashboard_sessions (organization_id, repository_id);
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry
    ON dashboard_sessions (expires_at)
    WHERE revoked_at IS NULL;

-- One authorization attempt. Single-use, short-lived, and bound to the browser
-- that started it: the state alone is not enough, the caller must also present
-- the nonce cookie set when the flow began. Both are stored hashed.
CREATE TABLE IF NOT EXISTS oauth_authorization_states (
    state_hash    TEXT PRIMARY KEY,
    nonce_hash    TEXT NOT NULL,
    redirect_to   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry
    ON oauth_authorization_states (expires_at)
    WHERE consumed_at IS NULL;

-- What a service token is allowed to be used for.
--
-- Every token issued so far was a collector credential in practice, so that is
-- the default and the backfill. `scope` is checked per endpoint: a collector
-- token is refused on governance routes even though it authenticates
-- correctly, which is the distinction the previous model could not express.
ALTER TABLE api_service_tokens
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'collector';

UPDATE api_service_tokens SET scope = 'collector' WHERE scope IS NULL;

ALTER TABLE api_service_tokens
    DROP CONSTRAINT IF EXISTS api_service_tokens_scope_check;
ALTER TABLE api_service_tokens
    ADD CONSTRAINT api_service_tokens_scope_check
    CHECK (scope IN ('collector', 'operator_read'));
