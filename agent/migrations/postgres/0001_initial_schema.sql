-- Relium continuous-pipeline PostgreSQL schema, migration 0001.
-- Applied transactionally by agent/postgres_migrate.py. Do not edit an
-- already-released migration; add a new numbered file instead.

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS repositories (
    organization_id TEXT NOT NULL REFERENCES organizations (organization_id),
    repository_id TEXT NOT NULL,
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id)
);

CREATE TABLE IF NOT EXISTS environments (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    connected BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, environment),
    FOREIGN KEY (organization_id, repository_id) REFERENCES repositories (organization_id, repository_id)
);
CREATE INDEX IF NOT EXISTS idx_environments_org_repo ON environments (organization_id, repository_id);

CREATE TABLE IF NOT EXISTS configuration_versions (
    configuration_version_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    policy_version TEXT,
    detector_version TEXT,
    threshold_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_configuration_versions_tenant_latest
    ON configuration_versions (organization_id, repository_id, environment, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, repository_id, environment, content_hash),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_evidence_tenant ON evidence (organization_id, repository_id, environment);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    reviewed_sha TEXT,
    merge_sha TEXT,
    manifest_hash TEXT,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, repository_id, environment, deployment_id),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_deployments_tenant ON deployments (organization_id, repository_id, environment);

CREATE TABLE IF NOT EXISTS deployment_transitions (
    transition_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deployment_id TEXT NOT NULL REFERENCES deployments (deployment_id),
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (deployment_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_deployment_transitions_deployment ON deployment_transitions (deployment_id);

CREATE TABLE IF NOT EXISTS metadata_baselines (
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    model TEXT NOT NULL,
    baseline JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, repository_id, environment, model),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);

CREATE TABLE IF NOT EXISTS monitoring_observations (
    observation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT REFERENCES deployments (deployment_id),
    model TEXT,
    metric TEXT NOT NULL,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_monitoring_observations_deployment ON monitoring_observations (deployment_id);
CREATE INDEX IF NOT EXISTS idx_monitoring_observations_tenant ON monitoring_observations (organization_id, repository_id, environment);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT REFERENCES deployments (deployment_id),
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, repository_id, environment, deployment_id, kind),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_anomalies_deployment ON anomalies (deployment_id);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT REFERENCES deployments (deployment_id),
    anomaly_id TEXT REFERENCES anomalies (anomaly_id),
    status TEXT NOT NULL CHECK (status IN ('open', 'investigating', 'resolved', 'unattributed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_incidents_deployment ON incidents (deployment_id);
CREATE INDEX IF NOT EXISTS idx_incidents_tenant ON incidents (organization_id, repository_id, environment);

CREATE TABLE IF NOT EXISTS rca_reports (
    rca_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'unattributed')),
    primary_cause JSONB,
    alternative_causes JSONB NOT NULL DEFAULT '[]'::jsonb,
    contributing_factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    downstream_symptoms JSONB NOT NULL DEFAULT '[]'::jsonb,
    unrelated_concurrent_changes JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence TEXT,
    unevaluated_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
-- Exactly one completed RCA per incident; unattributed re-attempts are allowed to accumulate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rca_reports_one_completed_per_incident
    ON rca_reports (incident_id) WHERE status = 'completed';

CREATE TABLE IF NOT EXISTS rca_evidence_links (
    rca_id TEXT NOT NULL REFERENCES rca_reports (rca_id),
    evidence_id TEXT NOT NULL REFERENCES evidence (evidence_id),
    role TEXT NOT NULL,
    PRIMARY KEY (rca_id, evidence_id, role)
);

CREATE TABLE IF NOT EXISTS lineage_records (
    lineage_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    model TEXT NOT NULL,
    payload JSONB NOT NULL,
    completeness TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, repository_id, environment, lineage_id),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    lineage_id TEXT NOT NULL REFERENCES lineage_records (lineage_id),
    upstream_model TEXT NOT NULL,
    downstream_model TEXT NOT NULL,
    PRIMARY KEY (lineage_id, upstream_model, downstream_model)
);

CREATE TABLE IF NOT EXISTS kpi_impact (
    kpi_impact_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT REFERENCES deployments (deployment_id),
    kpi_name TEXT NOT NULL,
    impact JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, repository_id, environment)
        REFERENCES environments (organization_id, repository_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_kpi_impact_deployment ON kpi_impact (deployment_id);

CREATE TABLE IF NOT EXISTS event_receipts (
    event_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING', 'CLAIMED', 'COMPLETED', 'DEAD_LETTER')),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (organization_id, repository_id, environment, deployment_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_outbox_events_claimable
    ON outbox_events (organization_id, repository_id, environment, state, next_attempt_at);

CREATE TABLE IF NOT EXISTS outbox_dead_letters (
    dead_letter_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbox_dead_letters_event ON outbox_dead_letters (event_id);

CREATE TABLE IF NOT EXISTS delivery_journal (
    journal_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('github', 'dashboard', 'slack')),
    event_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PUBLISHED', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    remote_id TEXT,
    reconciled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, repository_id, environment, channel, event_key)
);
CREATE INDEX IF NOT EXISTS idx_delivery_journal_tenant ON delivery_journal (organization_id, repository_id, environment);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id TEXT NOT NULL,
    repository_id TEXT,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    reference_type TEXT,
    reference_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_events_reference ON audit_events (reference_type, reference_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant ON audit_events (organization_id, repository_id);

CREATE TABLE IF NOT EXISTS retention_tombstones (
    organization_id TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL
);
