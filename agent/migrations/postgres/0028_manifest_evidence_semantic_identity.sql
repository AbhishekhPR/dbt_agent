-- Semantic identity for CI manifest evidence.
--
-- `manifest_hash` and `payload_hash` are hashes of the bytes a client sent.
-- dbt stamps `generated_at`, `invocation_id` and a per-entry `created_at` on
-- every compile, so two compiles of one commit differ in both hashes while
-- meaning the same thing. Removing those stamps was done in the CI workflow
-- only, which made the identity of a row depend on which client version wrote
-- it first -- and the idempotency key is a pure function of repository and
-- SHA, so a mismatch was permanent for that commit.
--
-- `semantic_manifest_hash` is that identity, derived on the server from the
-- canonicalised manifest (agent/metadata_evidence/manifest_identity.py).
--
-- Deliberately NOT backfilled. Canonicalisation is Python, not SQL, and this
-- table is immutable by trigger -- an UPDATE would have to punch a hole in
-- that. Rows keep `canonicalization_version = 1`, meaning "no recipe was
-- recorded", and the store re-derives their identity from the `manifest`
-- column they already hold. That is cheap, needs no write to an append-only
-- table, and is the same path a future recipe change will take.
--
-- Nullable and defaulted, so the previous application version keeps writing
-- successfully during a rolling deploy; its rows land on version 1 and are
-- re-derived exactly like the older ones.

ALTER TABLE manifest_evidence
    ADD COLUMN IF NOT EXISTS semantic_manifest_hash TEXT,
    ADD COLUMN IF NOT EXISTS canonicalization_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE manifest_evidence
    DROP CONSTRAINT IF EXISTS manifest_evidence_semantic_hash_check;
ALTER TABLE manifest_evidence
    ADD CONSTRAINT manifest_evidence_semantic_hash_check
    CHECK (semantic_manifest_hash IS NULL
           OR length(semantic_manifest_hash) = 64);

-- Answers "is this commit's evidence already what we are about to submit"
-- without reading the manifest document itself.
CREATE INDEX IF NOT EXISTS idx_manifest_evidence_semantic
    ON manifest_evidence (organization_id, repository_id, semantic_manifest_hash);
