-- Two integrity defects in the observation plane, both of which let the
-- production metadata comparison state something untrue.
--
-- WHY THIS IS A NEW FILE RATHER THAN AN EDIT TO 0011
--
-- 0011 is not merged or pushed, so amending it looks tempting. It is not safe.
-- agent/postgres_migrate.py selects pending work by VERSION alone
-- (`pending_migrations(applied)`); the checksum is recorded but never compared.
-- Any database that has already applied version 11 - a developer's local
-- database, a CI volume, a branch deployment - would therefore SKIP an edited
-- 0011 silently and end up missing these fixes while reporting itself fully
-- migrated. A new version is the only change every database will actually see.
--
-- Nothing already public is rewritten. 0004 and 0005 are untouched.

-- =====================================================================
-- 1. CARDINALITY IS A RATIO, AND MUST BE STORED AS ONE
-- =====================================================================
--
-- agent/collector/warehouse.py computes:
--
--     column["cardinality"] = float(distinct) / row_count
--
-- which is a fraction in [0, 1]. It is the rate-shaped twin of
-- `distinct_count`, exactly as `duplicate_rate` is the twin of
-- `duplicate_count`. Both of the other rates in this table are already
-- DOUBLE PRECISION with a [0, 1] CHECK; `cardinality` being BIGINT is the odd
-- one out, and it is wrong.
--
-- The consequence was not a rounding nuisance. PostgreSQL rounds on the way
-- into a BIGINT, so a genuine cardinality of 0.37 was PERSISTED AS 0 - a
-- column that is 37% distinct was recorded as one with no distinct values at
-- all. Every value below 0.5 became 0 and every value at or above it became 1.
--
-- Existing values are therefore set to NULL rather than converted. This is not
-- discarding evidence; the evidence was already destroyed at write time, and
-- it is unrecoverable: 0 could have come from 0.0, 0.1 or 0.49, and 1 from
-- 1.0, 0.6 or 0.5. NULL is the only honest record of that, and it is the value
-- the comparison engine already reads as "not observed" - so a snapshot
-- written before this migration will simply not contribute a cardinality
-- comparison, instead of contributing a fabricated one.
--
-- This runs BEFORE the immutability triggers below are installed. It is a
-- deliberate, reviewed, one-time repair of a storage defect, which is a
-- different act from the silent mutation of accepted evidence that those
-- triggers exist to prevent - and after this file runs, no such UPDATE is
-- possible again.
UPDATE snapshot_columns SET cardinality = NULL WHERE cardinality IS NOT NULL;

ALTER TABLE snapshot_columns
    ALTER COLUMN cardinality TYPE DOUBLE PRECISION;

ALTER TABLE snapshot_columns DROP CONSTRAINT IF EXISTS snapshot_columns_cardinality_check;
ALTER TABLE snapshot_columns ADD CONSTRAINT snapshot_columns_cardinality_check
    CHECK (cardinality IS NULL OR (cardinality >= 0 AND cardinality <= 1));

-- =====================================================================
-- 2. IMMUTABILITY THAT COVERS THE WHOLE OBSERVATION
-- =====================================================================
--
-- 0004 made `metadata_snapshots` immutable and said snapshots are immutable.
-- Only the header was. `snapshot_relations`, `snapshot_columns` and
-- `snapshot_metrics` hold every measured value - the row counts, the rates,
-- the data types, the existence flags - and all of them could be updated or
-- deleted in place. The header hash would still match while the numbers under
-- it had changed.
--
-- That matters more now than it did, because the production metadata
-- comparison binds an attempt to two snapshot IDs and promises that the
-- evidence does not shift underneath it. A promise about a row that anything
-- with a database connection can rewrite is not a promise.
--
-- These reuse relium_reject_snapshot_mutation() from 0004 rather than adding a
-- second function: one definition, one error message, one thing to reason
-- about. The trigger fires on UPDATE and DELETE only, so ingest INSERTs and
-- the foreign keys between these tables are entirely unaffected.
--
-- Deleting a parent snapshot is already refused by the 0004 trigger, so the
-- ON DELETE CASCADE declared on these tables can never fire; these triggers
-- close the direct path rather than fighting the cascade.

DROP TRIGGER IF EXISTS trg_snapshot_relations_immutable ON snapshot_relations;
CREATE TRIGGER trg_snapshot_relations_immutable
    BEFORE UPDATE OR DELETE ON snapshot_relations
    FOR EACH ROW EXECUTE FUNCTION relium_reject_snapshot_mutation();

DROP TRIGGER IF EXISTS trg_snapshot_columns_immutable ON snapshot_columns;
CREATE TRIGGER trg_snapshot_columns_immutable
    BEFORE UPDATE OR DELETE ON snapshot_columns
    FOR EACH ROW EXECUTE FUNCTION relium_reject_snapshot_mutation();

DROP TRIGGER IF EXISTS trg_snapshot_metrics_immutable ON snapshot_metrics;
CREATE TRIGGER trg_snapshot_metrics_immutable
    BEFORE UPDATE OR DELETE ON snapshot_metrics
    FOR EACH ROW EXECUTE FUNCTION relium_reject_snapshot_mutation();
