-- Record WHEN a deletion proved operational ownership, so a later phase does
-- not re-derive it from proof material the earlier phases deliberately
-- destroyed.
--
-- The deletion sequence revokes the CI token and removes the tenant repository
-- and installation projections. Those are exactly the rows Foundation 2 derives
-- `ci_token_binding` ownership from, so once GitHub access and credential
-- revocation have run, the root that was provably owned at the start of the
-- operation is no longer provable at all:
--
--     ownership proven            -> operation created
--     github/credential revocation-> CI token revoked, projections removed
--     operational purge           -> re-derives ownership -> INCOMPLETE
--
-- The workspace then cannot be deleted, having been blocked by the consequences
-- of its own deletion.
--
-- This follows the checkpoint pattern already on the row --
-- billing_terminal_verified_at, github_terminal_verified_at,
-- credentials_revoked_at -- rather than introducing a new mechanism. It records
-- a fact about ONE operation and grants nothing: a contradicted ownership state
-- discovered later still fails closed, because the purge continues to refuse an
-- `inconsistent` inventory whatever this column says.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE workspace_lifecycle_operations
    ADD COLUMN IF NOT EXISTS operational_ownership_verified_at TIMESTAMPTZ;

-- In-flight operations that already passed the gate keep their proof.
--
-- `credentials_revoked_at` is only ever set after `revoke_workspace_credentials`
-- returned, and that function refuses unless ownership_status is 'complete'.
-- The timestamp is therefore durable evidence that ownership WAS verified for
-- this operation, recorded before the revocation that destroyed the evidence.
-- Backfilling from it restores a fact the schema simply had nowhere to store,
-- and it cannot invent one: an operation that never reached credential
-- revocation has NULL here and must still prove ownership the ordinary way.
UPDATE workspace_lifecycle_operations
SET operational_ownership_verified_at = credentials_revoked_at
WHERE operational_ownership_verified_at IS NULL
  AND credentials_revoked_at IS NOT NULL;
