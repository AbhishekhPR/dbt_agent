-- Tenant-scoped lifecycle identifiers, migration 0003 (security).
--
-- Before this migration every externally-supplied lifecycle identifier was a
-- GLOBAL primary key, so an identifier could only ever belong to one tenant.
-- That made a cross-tenant lookup succeed instead of failing, which leaked a
-- victim tenant's lifecycle state through create_deployment.
--
-- Chosen approach (applied consistently): tenant-scoped composite uniqueness
-- and composite foreign keys. Every tenant-scoped table is keyed by
-- (organization_id, repository_id, <external identifier>), and every foreign
-- key carries the tenant columns so the database itself cannot express a
-- cross-tenant relationship.

-- ---------------------------------------------------------------- guard rails
-- Refuse to migrate data that already contains a cross-tenant relationship
-- rather than silently rewriting it.
DO $$
DECLARE
    offending INTEGER;
BEGIN
    SELECT count(*) INTO offending
    FROM deployment_transitions t
    JOIN deployments d ON d.deployment_id = t.deployment_id
    WHERE d.organization_id <> t.organization_id OR d.repository_id <> t.repository_id;
    IF offending > 0 THEN
        RAISE EXCEPTION 'refusing to migrate: % deployment_transitions rows reference another tenant''s deployment', offending;
    END IF;

    SELECT count(*) INTO offending
    FROM incidents i
    JOIN anomalies a ON a.anomaly_id = i.anomaly_id
    WHERE a.organization_id <> i.organization_id OR a.repository_id <> i.repository_id;
    IF offending > 0 THEN
        RAISE EXCEPTION 'refusing to migrate: % incidents reference another tenant''s anomaly', offending;
    END IF;

    SELECT count(*) INTO offending
    FROM rca_reports r
    JOIN incidents i ON i.incident_id = r.incident_id
    WHERE i.organization_id <> r.organization_id OR i.repository_id <> r.repository_id;
    IF offending > 0 THEN
        RAISE EXCEPTION 'refusing to migrate: % rca_reports reference another tenant''s incident', offending;
    END IF;
END $$;

-- ------------------------------------------------- junction tenant columns
-- Junction tables carry the tenant so their foreign keys can be composite.
ALTER TABLE rca_evidence_links ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE rca_evidence_links ADD COLUMN IF NOT EXISTS repository_id TEXT;
UPDATE rca_evidence_links l
   SET organization_id = r.organization_id, repository_id = r.repository_id
  FROM rca_reports r
 WHERE r.rca_id = l.rca_id AND l.organization_id IS NULL;
DELETE FROM rca_evidence_links WHERE organization_id IS NULL;
ALTER TABLE rca_evidence_links ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE rca_evidence_links ALTER COLUMN repository_id SET NOT NULL;

ALTER TABLE lineage_edges ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE lineage_edges ADD COLUMN IF NOT EXISTS repository_id TEXT;
UPDATE lineage_edges e
   SET organization_id = l.organization_id, repository_id = l.repository_id
  FROM lineage_records l
 WHERE l.lineage_id = e.lineage_id AND e.organization_id IS NULL;
DELETE FROM lineage_edges WHERE organization_id IS NULL;
ALTER TABLE lineage_edges ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE lineage_edges ALTER COLUMN repository_id SET NOT NULL;

-- ------------------------------------------------------- drop foreign keys
-- Dropped by discovered name so the migration does not depend on PostgreSQL's
-- auto-generated constraint naming.
DO $$
DECLARE
    fk RECORD;
BEGIN
    FOR fk IN
        SELECT con.conname, rel.relname
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
          JOIN pg_namespace ns ON ns.oid = rel.relnamespace
         WHERE con.contype = 'f'
           AND ns.nspname = 'public'
           AND rel.relname IN ('deployment_transitions', 'monitoring_observations',
                               'anomalies', 'incidents', 'kpi_impact', 'rca_reports',
                               'rca_evidence_links', 'lineage_edges')
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', fk.relname, fk.conname);
    END LOOP;
END $$;

-- ------------------------------------------------------- composite primary keys
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_pkey CASCADE;
ALTER TABLE evidence ADD PRIMARY KEY (organization_id, repository_id, evidence_id);

ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployments_pkey CASCADE;
ALTER TABLE deployments ADD PRIMARY KEY (organization_id, repository_id, deployment_id);

ALTER TABLE monitoring_observations DROP CONSTRAINT IF EXISTS monitoring_observations_pkey CASCADE;
ALTER TABLE monitoring_observations ADD PRIMARY KEY (organization_id, repository_id, observation_id);

ALTER TABLE anomalies DROP CONSTRAINT IF EXISTS anomalies_pkey CASCADE;
ALTER TABLE anomalies ADD PRIMARY KEY (organization_id, repository_id, anomaly_id);

ALTER TABLE incidents DROP CONSTRAINT IF EXISTS incidents_pkey CASCADE;
ALTER TABLE incidents ADD PRIMARY KEY (organization_id, repository_id, incident_id);

ALTER TABLE rca_reports DROP CONSTRAINT IF EXISTS rca_reports_pkey CASCADE;
ALTER TABLE rca_reports ADD PRIMARY KEY (organization_id, repository_id, rca_id);

ALTER TABLE lineage_records DROP CONSTRAINT IF EXISTS lineage_records_pkey CASCADE;
ALTER TABLE lineage_records ADD PRIMARY KEY (organization_id, repository_id, lineage_id);

ALTER TABLE kpi_impact DROP CONSTRAINT IF EXISTS kpi_impact_pkey CASCADE;
ALTER TABLE kpi_impact ADD PRIMARY KEY (organization_id, repository_id, kpi_impact_id);

ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_pkey CASCADE;
ALTER TABLE outbox_events ADD PRIMARY KEY (organization_id, repository_id, event_id);

ALTER TABLE delivery_journal DROP CONSTRAINT IF EXISTS delivery_journal_pkey CASCADE;
ALTER TABLE delivery_journal ADD PRIMARY KEY (organization_id, repository_id, journal_id);

ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_pkey CASCADE;
ALTER TABLE reviews ADD PRIMARY KEY (organization_id, repository_id, review_id);

-- Idempotency keys are per tenant: the same key in two tenants must not collide.
ALTER TABLE event_receipts DROP CONSTRAINT IF EXISTS event_receipts_pkey CASCADE;
ALTER TABLE event_receipts ADD PRIMARY KEY (organization_id, repository_id, event_id);

-- Transition sequence is per deployment within a tenant.
ALTER TABLE deployment_transitions DROP CONSTRAINT IF EXISTS deployment_transitions_deployment_id_sequence_key;
ALTER TABLE deployment_transitions
    ADD CONSTRAINT deployment_transitions_tenant_sequence_key
    UNIQUE (organization_id, repository_id, deployment_id, sequence);

-- ------------------------------------------------------- composite foreign keys
ALTER TABLE deployment_transitions
    ADD CONSTRAINT deployment_transitions_deployment_fkey
    FOREIGN KEY (organization_id, repository_id, deployment_id)
    REFERENCES deployments (organization_id, repository_id, deployment_id);

ALTER TABLE monitoring_observations
    ADD CONSTRAINT monitoring_observations_deployment_fkey
    FOREIGN KEY (organization_id, repository_id, deployment_id)
    REFERENCES deployments (organization_id, repository_id, deployment_id);
ALTER TABLE monitoring_observations
    ADD CONSTRAINT monitoring_observations_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE anomalies
    ADD CONSTRAINT anomalies_deployment_fkey
    FOREIGN KEY (organization_id, repository_id, deployment_id)
    REFERENCES deployments (organization_id, repository_id, deployment_id);
ALTER TABLE anomalies
    ADD CONSTRAINT anomalies_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE incidents
    ADD CONSTRAINT incidents_deployment_fkey
    FOREIGN KEY (organization_id, repository_id, deployment_id)
    REFERENCES deployments (organization_id, repository_id, deployment_id);
ALTER TABLE incidents
    ADD CONSTRAINT incidents_anomaly_fkey
    FOREIGN KEY (organization_id, repository_id, anomaly_id)
    REFERENCES anomalies (organization_id, repository_id, anomaly_id);
ALTER TABLE incidents
    ADD CONSTRAINT incidents_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE kpi_impact
    ADD CONSTRAINT kpi_impact_deployment_fkey
    FOREIGN KEY (organization_id, repository_id, deployment_id)
    REFERENCES deployments (organization_id, repository_id, deployment_id);
ALTER TABLE kpi_impact
    ADD CONSTRAINT kpi_impact_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE rca_reports
    ADD CONSTRAINT rca_reports_incident_fkey
    FOREIGN KEY (organization_id, repository_id, incident_id)
    REFERENCES incidents (organization_id, repository_id, incident_id);
ALTER TABLE rca_reports
    ADD CONSTRAINT rca_reports_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE rca_evidence_links
    ADD CONSTRAINT rca_evidence_links_rca_fkey
    FOREIGN KEY (organization_id, repository_id, rca_id)
    REFERENCES rca_reports (organization_id, repository_id, rca_id);
ALTER TABLE rca_evidence_links
    ADD CONSTRAINT rca_evidence_links_evidence_fkey
    FOREIGN KEY (organization_id, repository_id, evidence_id)
    REFERENCES evidence (organization_id, repository_id, evidence_id);

ALTER TABLE lineage_edges
    ADD CONSTRAINT lineage_edges_lineage_fkey
    FOREIGN KEY (organization_id, repository_id, lineage_id)
    REFERENCES lineage_records (organization_id, repository_id, lineage_id);

ALTER TABLE evidence
    ADD CONSTRAINT evidence_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE deployments
    ADD CONSTRAINT deployments_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE lineage_records
    ADD CONSTRAINT lineage_records_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

ALTER TABLE reviews
    ADD CONSTRAINT reviews_environment_fkey
    FOREIGN KEY (organization_id, repository_id, environment)
    REFERENCES environments (organization_id, repository_id, environment);

-- ------------------------------------------------------- scoped uniqueness
-- Exactly one completed RCA per incident, scoped to the owning tenant.
DROP INDEX IF EXISTS uq_rca_reports_one_completed_per_incident;
CREATE UNIQUE INDEX IF NOT EXISTS uq_rca_reports_one_completed_per_incident
    ON rca_reports (organization_id, repository_id, incident_id)
    WHERE status = 'completed';

CREATE INDEX IF NOT EXISTS idx_rca_evidence_links_tenant
    ON rca_evidence_links (organization_id, repository_id, rca_id);
CREATE INDEX IF NOT EXISTS idx_lineage_edges_tenant
    ON lineage_edges (organization_id, repository_id, lineage_id);
