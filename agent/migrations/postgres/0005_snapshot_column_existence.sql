-- Column-level existence in the evidence plane.
--
-- The collector already reports that a requested column is absent from
-- production. There was nowhere to put that fact: snapshot_relations carries
-- exists_in_production but snapshot_columns did not, so the API dropped the
-- flag and every requested column was stored as if it existed with unknown
-- metrics. The decision engine tests `column is None`, which the real
-- collector never produces, so column.missing_in_production could not fire and
-- a dropped production column was decided ALLOW with no findings.
--
-- TRUE is the correct default for existing rows: every column already stored
-- was one the collector found in the catalog, or one whose absence was
-- unrepresentable. Backfilling FALSE would invent findings for history.
ALTER TABLE snapshot_columns
    ADD COLUMN IF NOT EXISTS exists_in_production BOOLEAN NOT NULL DEFAULT TRUE;
