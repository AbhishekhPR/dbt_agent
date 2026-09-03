-- Evidence a workspace's plan does not include.
--
-- Warehouse and runtime evidence are paid inputs. A workspace that is not
-- entitled to them cannot supply them at all: the collector endpoint that
-- accepts a snapshot refuses with 402 before it reaches the store. Recording
-- that absence as PENDING said "requested, still outstanding", which is what
-- made such a review wait for a delivery that can never arrive.
--
-- NOT ENTITLED is the honest terminal answer for that case. It is distinct
-- from MISSING (should have been there and was not) and from NOT EVALUATED
-- (in scope, simply not looked at), so a review can state truthfully that
-- code and manifest evidence were evaluated while production warehouse
-- evidence is not included on the current plan.
--
-- Mirrors agent/evidence_policy.py EvidenceState. Widening a CHECK accepts
-- every row the old constraint accepted, so no backfill is required and no
-- existing row changes meaning.

-- 0004 declared the CHECK inline, so its name is whatever PostgreSQL derived.
-- Dropping by discovered name rather than by a guessed one keeps this
-- migration correct on a database whose constraint was auto-named differently.
DO $relium_evidence_state$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'review_evidence_coverage'
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%BLOCKED BY CREDENTIALS%'
    LOOP
        EXECUTE format(
            'ALTER TABLE review_evidence_coverage DROP CONSTRAINT %I',
            constraint_name);
    END LOOP;
END
$relium_evidence_state$;

ALTER TABLE review_evidence_coverage
    ADD CONSTRAINT review_evidence_coverage_state_check
    CHECK (state IN ('EVALUATED', 'MISSING', 'FAILED', 'NOT EVALUATED',
                     'UNSUPPORTED', 'STALE', 'PENDING',
                     'BLOCKED BY CREDENTIALS', 'NOT ENTITLED'));
