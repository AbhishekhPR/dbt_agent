-- Production metadata comparison evidence, bound to the exact attempt.
--
-- This follows 0010 deliberately rather than inventing a second mechanism.
-- The evidence lives on review_attempts because that table's primary key is
-- (organization_id, repository_id, review_id, attempt), so the binding to one
-- attempt is structural instead of something every read path has to remember
-- to filter by. An attempt that recorded baseline X against current Y keeps
-- saying X against Y after a newer snapshot arrives, because the answer was
-- written down rather than re-derived at render time.
--
-- A dedicated column rather than the generic `payload`, because four states
-- have to stay distinguishable and only a real NULL can carry the first:
--
--   NULL                                        -> comparison was never computed
--                                                  (for example an attempt still
--                                                  waiting for production metadata)
--   {"status":"no_baseline", ...}               -> ran; no prior eligible
--                                                  production observation existed
--   {"status":"evaluated","changes":[]}         -> ran; nothing changed
--   {"status":"partial", ...}                   -> ran; comparable coverage was
--                                                  incomplete
--   {"status":"unavailable", ...}               -> ran; could not be computed
--
-- Both snapshot identities are inside the document, so the evidence is
-- auditable against the immutable snapshots it was derived from.
--
-- No policy column, no severity, no threshold: this release stores what
-- changed between two production observations and nothing about what that
-- ought to mean.
ALTER TABLE review_attempts
    ADD COLUMN IF NOT EXISTS metadata_comparison JSONB;

-- The baseline lookup is "most recent eligible observation for this tenant,
-- repository and environment, strictly before this one". The existing
-- idx_metadata_snapshots_freshness index covers the leading columns; this one
-- extends the ordering to the full deterministic key so a tie on observed_at
-- is resolved by the index rather than by a sort.
CREATE INDEX IF NOT EXISTS idx_metadata_snapshots_baseline
    ON metadata_snapshots (organization_id, repository_id, environment,
                           observed_at DESC, received_at DESC, snapshot_id DESC);
