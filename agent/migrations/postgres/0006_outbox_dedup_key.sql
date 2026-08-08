-- Exactly-once per unit of WORK, not per subject.
--
-- uq_outbox_events_subject keyed on (organization, repository, environment,
-- subject_type, subject_id, event_type). complete_outbox leaves the row in
-- place as COMPLETED, so once a review had been recomputed once, every later
-- enqueue for that review was silently discarded by ON CONFLICT DO NOTHING.
--
-- The consequence was that a second, genuinely different metadata snapshot -
-- new evidence, describing a changed production state - never produced a
-- recomputation. The review's decision froze at whatever the first snapshot
-- said, which is precisely wrong for a warehouse whose state moves.
--
-- The duplicate-suppression this index was providing is already enforced,
-- correctly and one layer lower, by recompute_review(): it returns
-- already_recomputed when an attempt for that snapshot_id exists, so a
-- redelivered duplicate cannot produce a second decision even if it enqueues.
--
-- dedup_key names the distinct work item. Deployment events leave it '' and
-- keep byte-identical behaviour; review recomputation sets it to the
-- snapshot_id, which is the thing that actually makes the job distinct.
ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS dedup_key TEXT NOT NULL DEFAULT '';

DROP INDEX IF EXISTS uq_outbox_events_subject;
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_events_subject
    ON outbox_events (organization_id, repository_id, environment,
                      subject_type, subject_id, event_type, dedup_key);
