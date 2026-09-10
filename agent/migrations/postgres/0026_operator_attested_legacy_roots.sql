-- Explicit operator attestation of pre-tenant legacy operational ownership.
--
-- Some legacy operational roots predate the tenant plane entirely. Their
-- `organizations` / `repositories` rows carry TEXT names and nothing else --
-- no GitHub repository id, no installation id, no owner id -- so there is no
-- provider-issued identifier anywhere in production from which ownership could
-- be derived. `ci_token_binding` correctly refuses them, and Foundation 2
-- correctly reports `operational_ownership_incomplete`.
--
-- ###################################################################
-- # THIS IS AN ATTESTATION, NOT A PROOF. IT IS NOT VERIFIED.        #
-- ###################################################################
--
-- `operator_attested_legacy` records that a named human took responsibility
-- for a mapping the system cannot verify. It is deliberately a DIFFERENT basis
-- from `ci_token_binding` so that every reader -- the inventory, the audit, the
-- lifecycle gate, and a person reading the table a year from now -- can tell
-- the two apart. Nothing here weakens or reinterprets the CI-token proof.
--
-- The provenance CHECK below is what keeps that honest: an attested row must
-- carry NO provider source identifiers at all. The schema itself refuses to let
-- an attestation dress up as provider-verified evidence.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE tenant_operational_roots
    DROP CONSTRAINT IF EXISTS tenant_operational_roots_basis_check;
ALTER TABLE tenant_operational_roots
    ADD CONSTRAINT tenant_operational_roots_basis_check
    CHECK (mapping_basis IN (
        'ci_token_binding', 'verified_github_repository',
        'operator_attested_legacy'
    ));

ALTER TABLE tenant_operational_roots
    DROP CONSTRAINT IF EXISTS tenant_operational_roots_provenance_check;
ALTER TABLE tenant_operational_roots
    ADD CONSTRAINT tenant_operational_roots_provenance_check
    CHECK (
        (mapping_basis = 'ci_token_binding'
         AND source_ci_token_id IS NOT NULL
         AND source_github_repository_id IS NOT NULL)
        OR
        (mapping_basis = 'verified_github_repository'
         AND source_github_repository_id IS NOT NULL
         AND source_github_installation_id IS NOT NULL)
        OR
        -- An attestation names no provider evidence, because it has none.
        (mapping_basis = 'operator_attested_legacy'
         AND source_ci_token_id IS NULL
         AND source_github_repository_id IS NULL
         AND source_github_installation_id IS NULL)
    );

-- The durable record of WHY. Separate from the mapping so the reason survives
-- independently, and append-only in practice: one row per attestation attempt
-- that mutated, keyed by its own id rather than by the organization.
CREATE TABLE IF NOT EXISTS tenant_operational_root_attestations (
    attestation_id  TEXT PRIMARY KEY,

    -- Not a foreign key to tenant_operational_roots: the audit record must
    -- outlive the mapping it describes. ON DELETE RESTRICT on the parents keeps
    -- the referenced rows alive while the mapping exists; this record persists
    -- regardless.
    organization_id TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,

    mapping_basis   TEXT NOT NULL
                    CHECK (mapping_basis = 'operator_attested_legacy'),

    -- Operator-supplied prose. Bounded, and never a credential: the CLI refuses
    -- an empty reason and the column refuses an unbounded one.
    reason          TEXT NOT NULL
                    CHECK (length(btrim(reason)) BETWEEN 1 AND 2000),

    attested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_root_attestations_organization
    ON tenant_operational_root_attestations (organization_id);
CREATE INDEX IF NOT EXISTS idx_root_attestations_tenant
    ON tenant_operational_root_attestations (tenant_id);
