-- Pull-request state, persisted beside the review rather than instead of it.
--
-- Until now a merged or closed pull request was invisible to Relium: the
-- `closed` delivery was ignored outright, so the dashboard could only ever
-- describe a review as if its PR were still open. The fix is NOT to remove or
-- rewrite the review when the PR ends -- the analysis, its attempts, its
-- findings and its evidence are the audit record, and they stay exactly as
-- they were. What changes is that the review now also carries what happened to
-- the pull request it described.
--
-- Three design points that matter for review:
--
-- 1. `pr_state` is SEPARATE from `lifecycle_state` and from `decision`.
--    `lifecycle_state` is how far Relium's own analysis got; `decision` is the
--    verdict it reached; `pr_state` is what GitHub did with the pull request
--    afterwards. Folding any of the three into another would make "we are
--    still collecting evidence" indistinguishable from "the PR was closed
--    before we finished", which is precisely the distinction the History page
--    exists to show.
--
-- 2. UNKNOWN is a real value, not a placeholder for NULL. Every review written
--    before this migration was persisted by a path that never observed a
--    `closed` delivery, so its pull request's fate is genuinely not known
--    here. The DEFAULT fills those rows with UNKNOWN in place, without
--    rewriting a single other column.
--
-- 3. Additive and non-destructive. No row is deleted, no column is dropped, no
--    existing value is rewritten. A rollback of the application code leaves
--    the column populated and unread.

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS pr_state TEXT NOT NULL DEFAULT 'UNKNOWN';

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_pr_state_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_pr_state_check
    CHECK (pr_state IN (
        'OPEN',
        'MERGED',
        'CLOSED',
        'UNKNOWN'
    ));

-- When the pull request reached that state, as distinct from when the review
-- itself was last touched. NULL means it has never been observed, which is the
-- honest reading for every legacy row.
ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS pr_state_updated_at TIMESTAMPTZ;

-- The History page filters and groups by what happened to the pull request, so
-- that read must not scan every review a workspace has ever had.
CREATE INDEX IF NOT EXISTS idx_reviews_pr_state
    ON reviews (organization_id, repository_id, pr_state, created_at DESC);

-- Recording a PR's end has to find every review for that pull request -- one
-- per analysed head SHA -- and not just the newest.
CREATE INDEX IF NOT EXISTS idx_reviews_pull_number
    ON reviews (organization_id, repository_id, pull_number)
    WHERE pull_number IS NOT NULL;
