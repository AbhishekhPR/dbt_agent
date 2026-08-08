-- Human governance actions on a review.
--
-- Both tables record what a PERSON decided about a review. Neither may alter
-- what RELIUM decided: the review, its attempts and its findings stay exactly
-- as the engine wrote them. An override is governance metadata, not evidence
-- that the analysis was wrong, and the two must remain separable forever.

-- ---------------------------------------------------------------------
-- Request changes. A durable record of a GitHub pull-request review that
-- Relium submitted on a reviewer's behalf.
--
-- It is a queued intent, not a claim of success: `state` only becomes
-- PUBLISHED once GitHub has accepted the review and returned its id. A
-- failure is recorded with its reason and stays visible.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_change_requests (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    change_request_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    environment TEXT NOT NULL,
    pull_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    remote_review_id TEXT,
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, repository_id, change_request_id),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    CHECK (state IN ('PENDING', 'PUBLISHED', 'FAILED')),
    CHECK (length(message) BETWEEN 1 AND 4000)
);

-- One outstanding request-changes per (review, attempt). A double click, or a
-- retry, resolves to the SAME row rather than submitting a second GitHub
-- review on the same pull request.
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_change_requests_attempt
    ON review_change_requests (organization_id, repository_id, review_id, attempt)
    WHERE state IN ('PENDING', 'PUBLISHED');

CREATE INDEX IF NOT EXISTS idx_review_change_requests_review
    ON review_change_requests (organization_id, repository_id, review_id);

-- ---------------------------------------------------------------------
-- Review exceptions.
--
-- An approved exception records that a human accepted a risk Relium
-- identified. The review's decision column is deliberately NOT touched: a
-- BLOCK stays BLOCK, and the effective governance position is expressed as
-- "BLOCK, exception approved".
--
-- Scope defaults to the exact attempt. A later attempt is a new analysis of
-- new evidence, and inheriting an older attempt's override would let a stale
-- human judgement silently clear a finding nobody has seen.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_exceptions (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    exception_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    -- The exact attempt whose decision is being overridden.
    attempt INTEGER NOT NULL,
    environment TEXT NOT NULL,
    -- The decision as it stood when the exception was approved, copied so the
    -- record remains meaningful even if read far from its attempt.
    overridden_decision TEXT,
    base_sha TEXT,
    head_sha TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'attempt',
    state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    revocation_reason TEXT,
    PRIMARY KEY (organization_id, repository_id, exception_id),
    FOREIGN KEY (organization_id, repository_id, review_id)
        REFERENCES reviews (organization_id, repository_id, review_id) ON DELETE CASCADE,
    CHECK (state IN ('active', 'revoked')),
    CHECK (scope IN ('attempt', 'review')),
    -- A reason is not optional. An override without a stated reason is an
    -- unexplained decision, and the audit record would be worthless.
    CHECK (length(trim(reason)) >= 3),
    CHECK (state <> 'revoked' OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL))
);

-- One ACTIVE exception per (review, attempt). Re-submitting resolves to the
-- existing row instead of stacking duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_exceptions_active_attempt
    ON review_exceptions (organization_id, repository_id, review_id, attempt)
    WHERE state = 'active';

CREATE INDEX IF NOT EXISTS idx_review_exceptions_review
    ON review_exceptions (organization_id, repository_id, review_id);
