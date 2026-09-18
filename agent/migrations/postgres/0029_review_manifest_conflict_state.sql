-- An explicit review state for "this commit's manifest evidence disagrees".
--
-- A genuine semantic difference for one commit SHA is neither a transient wait
-- nor a decision. The evidence already stored is what earlier decisions were
-- computed from and is immutable; the manifest just submitted says something
-- else. Analysing either one would be analysing code the other side did not
-- send, so the review stops here, visibly, and is picked up again by a later
-- delivery once the disagreement is gone.
--
-- Additive: the constraint is replaced with the same list plus one value, so
-- every existing row stays valid and the previous application release keeps
-- writing every state it knows. Rolling back the code leaves at most a few
-- rows in a state the old code will not produce; nothing reads the state as
-- an enum outside the CHECK and the application tuple.

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_lifecycle_state_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_lifecycle_state_check
    CHECK (lifecycle_state IN (
        'RECEIVED',
        'WAITING_FOR_MANIFEST',
        'CODE_ANALYSIS_COMPLETE',
        'METADATA_NOT_REQUIRED',
        'METADATA_REQUESTED',
        'WAITING_FOR_METADATA',
        'METADATA_PARTIAL',
        'METADATA_COMPLETE',
        'METADATA_STALE',
        'DECISION_READY',
        'PUBLISHED',
        'FAILED',
        'MANIFEST_CONFLICT'
    ));

-- Finding the reviews that need a human without scanning every review.
CREATE INDEX IF NOT EXISTS idx_reviews_manifest_conflict
    ON reviews (organization_id, repository_id, updated_at DESC)
    WHERE lifecycle_state = 'MANIFEST_CONFLICT';
