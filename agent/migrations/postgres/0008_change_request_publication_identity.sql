-- A PUBLISHED change request is a verifiable claim about a remote GitHub
-- review. Legacy rows without that identity cannot support the claim, so keep
-- the historical outcome explicit as FAILED verification rather than imply
-- that the remote operation itself failed.
UPDATE review_change_requests
SET state = 'FAILED',
    failure_reason = 'publication identity missing; publication success cannot be verified',
    published_at = NULL
WHERE state = 'PUBLISHED'
  AND (
      remote_review_id IS NULL
      OR NOT (
          remote_review_id ~ '^[0-9]+$'
          AND remote_review_id ~ '[1-9]'
      )
  );

ALTER TABLE review_change_requests
    ADD CONSTRAINT review_change_requests_published_identity_check
    CHECK (
        state <> 'PUBLISHED'
        OR (
            remote_review_id IS NOT NULL
            AND remote_review_id ~ '^[0-9]+$'
            AND remote_review_id ~ '[1-9]'
        )
    );
