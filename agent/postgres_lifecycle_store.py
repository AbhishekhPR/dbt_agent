"""PostgreSQL authoritative lifecycle-store adapter.

Implements the same externally-visible contract as
``agent.sqlite_lifecycle_store.SQLiteLifecycleStore`` (used for deterministic
local unit tests) plus the extended continuous-pipeline entities: monitoring,
anomalies, incidents, RCA, lineage edges, KPI impact, outbox recovery and
dead-lettering, delivery journals and audit events.

This adapter refuses to operate without an explicitly supplied DSN and never
falls back to SQLite, an in-memory store, or filesystem/JSON storage.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent.lifecycle_models import ALLOWED_TRANSITIONS
from agent.postgres_migrate import apply_migrations

OUTBOX_LEASE_SECONDS = 300


class PostgresLifecycleStore:
    provider = "postgresql"

    def __init__(self, dsn: str | None):
        if not dsn:
            raise RuntimeError("POSTGRES lifecycle store is BLOCKED BY CREDENTIALS")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("PostgreSQL lifecycle store requires psycopg") from exc
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        # autocommit=True: every statement commits immediately and read-only
        # methods never leave the connection idle-in-transaction (which would
        # otherwise hold locks and starve concurrent DDL/other connections).
        # Multi-statement operations that must be atomic use an explicit
        # `with self.connection.transaction():` block instead.
        self.connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        apply_migrations(self.connection)

    # -- schema / tenant lifecycle -----------------------------------------

    def ensure_schema(self):
        apply_migrations(self.connection)

    def ensure_tenant(self, organization_id, repository_id, environment):
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO organizations (organization_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (organization_id,),
            )
            self.connection.execute(
                "INSERT INTO repositories (organization_id, repository_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (organization_id, repository_id),
            )
            self.connection.execute(
                "INSERT INTO environments (organization_id, repository_id, environment, connected) "
                "VALUES (%s, %s, %s, TRUE) "
                "ON CONFLICT (organization_id, repository_id, environment) DO UPDATE SET connected = TRUE",
                (organization_id, repository_id, environment),
            )

    def disconnect_repository(self, organization_id, repository_id):
        self.connection.execute(
            "UPDATE environments SET connected = FALSE WHERE organization_id=%s AND repository_id=%s",
            (organization_id, repository_id),
        )

    def delete_tenant(self, organization_id):
        now = datetime.now(timezone.utc)
        with self.connection.transaction():
            cur = self.connection.execute(
                "INSERT INTO retention_tombstones (organization_id, deleted_at) VALUES (%s, %s) "
                "ON CONFLICT (organization_id) DO UPDATE SET deleted_at = EXCLUDED.deleted_at "
                "RETURNING deleted_at",
                (organization_id, now),
            )
            row = cur.fetchone()
            # Junction tables now carry the tenant themselves, so they are deleted
            # by the same direct predicate as every other tenant-scoped table.
            for table in (
                "rca_evidence_links", "lineage_edges",
                "rca_reports", "incidents", "anomalies",
                "monitoring_observations", "metadata_baselines", "kpi_impact",
                "lineage_records", "outbox_dead_letters", "outbox_events",
                "deployment_transitions", "deployments", "evidence", "configuration_versions",
                "delivery_journal", "environments", "repositories",
            ):
                self.connection.execute(f"DELETE FROM {table} WHERE organization_id=%s", (organization_id,))
            # Audit events are retained across tenant deletion for compliance; they are
            # keyed by the tombstoned organization_id so they remain attributable.
        return {"organization_id": organization_id, "deleted_at": row["deleted_at"].isoformat()}

    def _tenant(self, organization_id, repository_id, environment, *, allow_disconnected=False):
        row = self.connection.execute(
            "SELECT connected FROM environments WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchone()
        if not row or (not allow_disconnected and not row["connected"]):
            raise ValueError("Unknown, disconnected, or unauthorized tenant")

    # -- configuration / policy / detector versions -------------------------

    def record_versions(self, organization_id, repository_id, environment, *, policy, detector, threshold):
        self._tenant(organization_id, repository_id, environment)
        self.connection.execute(
            "INSERT INTO configuration_versions "
            "(organization_id, repository_id, environment, policy_version, detector_version, threshold_version) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (organization_id, repository_id, environment, policy, detector, threshold),
        )
        return {"policy_version": policy, "detector_version": detector, "threshold_version": threshold}

    def latest_versions(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute(
            "SELECT policy_version, detector_version, threshold_version FROM configuration_versions "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "ORDER BY created_at DESC, configuration_version_id DESC LIMIT 1",
            (organization_id, repository_id, environment),
        ).fetchone()
        return dict(row) if row else {}

    # -- evidence -------------------------------------------------------------

    def append_evidence(self, organization_id, repository_id, environment, payload, *, evidence_id=None):
        self._tenant(organization_id, repository_id, environment)
        evidence_id = evidence_id or str(uuid.uuid4())
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.connection.transaction():
            existing = self.connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id=%s", (evidence_id,)
            ).fetchone()
            if existing:
                raise ValueError("Evidence references are immutable")
            self.connection.execute(
                "INSERT INTO evidence (evidence_id, organization_id, repository_id, environment, payload, content_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (evidence_id, organization_id, repository_id, environment, self._Jsonb(payload), digest),
            )
        return {"evidence_id": evidence_id, "hash": digest, "payload": payload}

    def list_evidence(self, organization_id, repository_id, environment):
        try:
            self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        except ValueError:
            tombstoned = self.connection.execute(
                "SELECT 1 FROM retention_tombstones WHERE organization_id=%s", (organization_id,)
            ).fetchone()
            if not tombstoned:
                raise
            return []
        rows = self.connection.execute(
            "SELECT evidence_id, content_hash AS hash, payload FROM evidence "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- deployment lifecycle --------------------------------------------------

    def create_deployment(self, organization_id, repository_id, environment, payload):
        self._tenant(organization_id, repository_id, environment)
        deployment_id = payload["deployment_id"]
        # Scoped: an identifier owned by another tenant must not resolve here.
        existing = self.connection.execute(
            "SELECT payload, status FROM deployments "
            "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
            (organization_id, repository_id, deployment_id),
        ).fetchone()
        if existing:
            return {"deployment_id": deployment_id, **existing["payload"], "status": existing["status"]}
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO deployments "
                "(deployment_id, organization_id, repository_id, environment, reviewed_sha, merge_sha, manifest_hash, payload, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    deployment_id, organization_id, repository_id, environment,
                    payload.get("reviewed_sha"), payload.get("merge_sha"), payload.get("manifest_hash"),
                    self._Jsonb(payload), "reviewed",
                ),
            )
            self.connection.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, deployment_id, event_type, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id,
                 "deployment.reviewed", self._Jsonb(payload)),
            )
        return {"deployment_id": deployment_id, **payload, "status": "reviewed"}

    def append_transition(self, organization_id, repository_id, environment, deployment_id, to_status):
        self._tenant(organization_id, repository_id, environment)
        row = self.connection.execute(
            "SELECT status FROM deployments WHERE deployment_id=%s AND organization_id=%s "
            "AND repository_id=%s AND environment=%s",
            (deployment_id, organization_id, repository_id, environment),
        ).fetchone()
        if not row:
            raise ValueError("Unknown deployment")
        if to_status == row["status"]:
            return
        if to_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
            raise ValueError(f"Invalid deployment transition {row['status']} -> {to_status}")
        with self.connection.transaction():
            next_seq = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM deployment_transitions "
                "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
                (organization_id, repository_id, deployment_id),
            ).fetchone()["next"]
            self.connection.execute(
                "UPDATE deployments SET status=%s, updated_at=now() "
                "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
                (to_status, organization_id, repository_id, deployment_id),
            )
            self.connection.execute(
                "INSERT INTO deployment_transitions "
                "(deployment_id, organization_id, repository_id, environment, from_status, to_status, sequence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (deployment_id, organization_id, repository_id, environment, row["status"], to_status, next_seq),
            )
            self.connection.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, deployment_id, event_type, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, deployment_id, event_type) DO NOTHING",
                (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id,
                 f"deployment.{to_status}", self._Jsonb({"deployment_id": deployment_id, "status": to_status})),
            )

    def transitions(self, organization_id, repository_id, environment, deployment_id):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rows = self.connection.execute(
            "SELECT * FROM deployment_transitions "
            "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s ORDER BY sequence",
            (organization_id, repository_id, deployment_id),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- transactional outbox --------------------------------------------------

    def claim_outbox(self, organization_id, repository_id, environment, worker):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        self._recover_expired_claims(organization_id, repository_id, environment)
        # The SELECT ... FOR UPDATE SKIP LOCKED and the UPDATE that claims the
        # winning row must share one transaction: the row lock only prevents a
        # concurrent claim for as long as the transaction holding it is open.
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT * FROM outbox_events WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND state='PENDING' AND next_attempt_at <= now() "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                (organization_id, repository_id, environment),
            ).fetchone()
            if not row:
                return None
            lease_expires = datetime.now(timezone.utc) + timedelta(seconds=OUTBOX_LEASE_SECONDS)
            self.connection.execute(
                "UPDATE outbox_events SET state='CLAIMED', lease_owner=%s, lease_expires_at=%s, attempts=attempts+1 "
                "WHERE organization_id=%s AND repository_id=%s AND event_id=%s AND state='PENDING'",
                (worker, lease_expires, organization_id, repository_id, row["event_id"]),
            )
        return dict(row)

    def _recover_expired_claims(self, organization_id, repository_id, environment):
        self.connection.execute(
            "UPDATE outbox_events SET state='PENDING', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "AND state='CLAIMED' AND lease_expires_at < now()",
            (organization_id, repository_id, environment),
        )

    def complete_outbox(self, organization_id, repository_id, event_id):
        self.connection.execute(
            "UPDATE outbox_events SET state='COMPLETED', completed_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s AND state='CLAIMED'",
            (organization_id, repository_id, event_id),
        )

    def fail_outbox(self, organization_id, repository_id, event_id, *, error,
                    max_attempts=5, retry_backoff_seconds=30):
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT * FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s AND event_id=%s FOR UPDATE",
                (organization_id, repository_id, event_id),
            ).fetchone()
            if not row or row["state"] in ("DEAD_LETTER", "COMPLETED"):
                return
            if row["attempts"] >= max_attempts:
                self._dead_letter(row, error)
            else:
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=retry_backoff_seconds)
                self.connection.execute(
                    "UPDATE outbox_events SET state='PENDING', lease_owner=NULL, lease_expires_at=NULL, "
                    "next_attempt_at=%s, last_error=%s "
                    "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
                    (next_attempt, str(error)[:2000], organization_id, repository_id, event_id),
                )

    def _dead_letter(self, row, error):
        self.connection.execute(
            "INSERT INTO outbox_dead_letters "
            "(event_id, organization_id, repository_id, environment, deployment_id, event_type, payload, attempts, last_error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row["event_id"], row["organization_id"], row["repository_id"], row["environment"],
             row["deployment_id"], row["event_type"], self._Jsonb(row["payload"]), row["attempts"], str(error)[:2000]),
        )
        self.connection.execute(
            "UPDATE outbox_events SET state='DEAD_LETTER', last_error=%s "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (str(error)[:2000], row["organization_id"], row["repository_id"], row["event_id"]),
        )

    def dead_letters(self, organization_id, repository_id, environment):
        rows = self.connection.execute(
            "SELECT * FROM outbox_dead_letters WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "ORDER BY created_at",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- monitoring / anomalies --------------------------------------------------

    def record_metadata_baseline(self, organization_id, repository_id, environment, model, baseline):
        self._tenant(organization_id, repository_id, environment)
        self.connection.execute(
            "INSERT INTO metadata_baselines (organization_id, repository_id, environment, model, baseline) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, environment, model) "
            "DO UPDATE SET baseline=EXCLUDED.baseline, created_at=now()",
            (organization_id, repository_id, environment, model, self._Jsonb(baseline)),
        )

    def append_observation(self, organization_id, repository_id, environment, *, deployment_id, model, metric, payload, observation_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        observation_id = observation_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO monitoring_observations "
            "(observation_id, organization_id, repository_id, environment, deployment_id, model, metric, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (observation_id, organization_id, repository_id, environment, deployment_id, model, metric, self._Jsonb(payload)),
        )
        return observation_id

    def observations(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_observations WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s AND deployment_id=%s ORDER BY observed_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_observations WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s ORDER BY observed_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_anomaly(self, organization_id, repository_id, environment, *, deployment_id, kind, payload, anomaly_id=None):
        """Idempotent: a second anomaly of the same kind for the same deployment returns the first."""
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        anomaly_id = anomaly_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO anomalies (anomaly_id, organization_id, repository_id, environment, deployment_id, kind, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, deployment_id, kind) DO NOTHING "
                "RETURNING *",
                (anomaly_id, organization_id, repository_id, environment, deployment_id, kind, self._Jsonb(payload)),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                    "AND deployment_id=%s AND kind=%s",
                    (organization_id, repository_id, environment, deployment_id, kind),
                ).fetchone()
        return dict(row)

    def anomalies(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND deployment_id=%s ORDER BY created_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s ORDER BY created_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- lineage / KPI impact --------------------------------------------------

    def record_lineage(self, organization_id, repository_id, environment, model, payload, *, edges=(), completeness=None, lineage_id=None):
        self._tenant(organization_id, repository_id, environment)
        lineage_id = lineage_id or str(uuid.uuid4())
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO lineage_records (lineage_id, organization_id, repository_id, environment, model, payload, completeness) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, lineage_id) DO NOTHING",
                (lineage_id, organization_id, repository_id, environment, model, self._Jsonb(payload), completeness),
            )
            for upstream, downstream in edges:
                self.connection.execute(
                    "INSERT INTO lineage_edges "
                    "(lineage_id, upstream_model, downstream_model, organization_id, repository_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (lineage_id, upstream, downstream, organization_id, repository_id),
                )
        return lineage_id

    def list_lineage(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rows = self.connection.execute(
            "SELECT lineage_id, payload FROM lineage_records "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row["payload"], lineage_id=row["lineage_id"]) for row in rows]

    def record_kpi_impact(self, organization_id, repository_id, environment, *, deployment_id, kpi_name, impact, kpi_impact_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        kpi_impact_id = kpi_impact_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO kpi_impact (kpi_impact_id, organization_id, repository_id, environment, deployment_id, kpi_name, impact) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (kpi_impact_id, organization_id, repository_id, environment, deployment_id, kpi_name, self._Jsonb(impact)),
        )
        return kpi_impact_id

    def kpi_impacts(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM kpi_impact WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND deployment_id=%s ORDER BY created_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM kpi_impact WHERE organization_id=%s AND repository_id=%s AND environment=%s ORDER BY created_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- incidents / RCA --------------------------------------------------

    def create_incident(self, organization_id, repository_id, environment, *, deployment_id, anomaly_id, incident_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        incident_id = incident_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO incidents (incident_id, organization_id, repository_id, environment, deployment_id, anomaly_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'open') "
                "ON CONFLICT (organization_id, repository_id, incident_id) DO NOTHING RETURNING *",
                (incident_id, organization_id, repository_id, environment, deployment_id, anomaly_id),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM incidents "
                    "WHERE organization_id=%s AND repository_id=%s AND incident_id=%s",
                    (organization_id, repository_id, incident_id),
                ).fetchone()
        return dict(row)

    def update_incident_status(self, organization_id, repository_id, incident_id, status):
        """Idempotent: setting the same status twice is a no-op, not an error."""
        self.connection.execute(
            "UPDATE incidents SET status=%s, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND incident_id=%s AND status != %s",
            (status, organization_id, repository_id, incident_id, status),
        )

    def get_incident(self, organization_id, repository_id, incident_id):
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE organization_id=%s AND repository_id=%s AND incident_id=%s",
            (organization_id, repository_id, incident_id),
        ).fetchone()
        return dict(row) if row else None

    def create_rca(self, incident_id, organization_id, repository_id, environment, *, status, primary_cause=None,
                    alternative_causes=(), contributing_factors=(), downstream_symptoms=(),
                    unrelated_concurrent_changes=(), confidence=None, unevaluated_evidence=(),
                    evidence_links=(), rca_id=None):
        """Idempotent: only one COMPLETED RCA is retained per incident (a unique partial index enforces it)."""
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rca_id = rca_id or str(uuid.uuid4())
        try:
            # The insert and its evidence links share a transaction so a link
            # failure can't leave an RCA report with partial evidence attached.
            with self.connection.transaction():
                row = self.connection.execute(
                    "INSERT INTO rca_reports "
                    "(rca_id, incident_id, organization_id, repository_id, environment, status, primary_cause, "
                    "alternative_causes, contributing_factors, downstream_symptoms, unrelated_concurrent_changes, "
                    "confidence, unevaluated_evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        rca_id, incident_id, organization_id, repository_id, environment, status,
                        self._Jsonb(primary_cause) if primary_cause is not None else None,
                        self._Jsonb(list(alternative_causes)), self._Jsonb(list(contributing_factors)),
                        self._Jsonb(list(downstream_symptoms)), self._Jsonb(list(unrelated_concurrent_changes)),
                        confidence, self._Jsonb(list(unevaluated_evidence)),
                    ),
                ).fetchone()
                for evidence_id, role in evidence_links:
                    # Composite foreign keys make a cross-tenant evidence link
                    # impossible to insert, not merely discouraged.
                    self.connection.execute(
                        "INSERT INTO rca_evidence_links "
                        "(rca_id, evidence_id, role, organization_id, repository_id) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (rca_id, evidence_id, role, organization_id, repository_id),
                    )
        except self._psycopg.errors.UniqueViolation:
            # The transaction block already rolled back on the way out; the
            # connection is clean, so this read runs in a fresh statement.
            existing = self.connection.execute(
                "SELECT * FROM rca_reports WHERE organization_id=%s AND repository_id=%s "
                "AND incident_id=%s AND status='completed'",
                (organization_id, repository_id, incident_id),
            ).fetchone()
            return dict(existing)
        return dict(row)

    def rca_for_incident(self, organization_id, repository_id, incident_id):
        rows = self.connection.execute(
            "SELECT * FROM rca_reports WHERE organization_id=%s AND repository_id=%s "
            "AND incident_id=%s ORDER BY created_at",
            (organization_id, repository_id, incident_id),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- delivery journal --------------------------------------------------

    def record_delivery(self, organization_id, repository_id, environment, *, channel, event_key, payload, journal_id=None):
        journal_id = journal_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO delivery_journal (journal_id, organization_id, repository_id, environment, channel, event_key, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, channel, event_key) DO NOTHING RETURNING *",
                (journal_id, organization_id, repository_id, environment, channel, event_key, self._Jsonb(payload)),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                    "AND channel=%s AND event_key=%s",
                    (organization_id, repository_id, environment, channel, event_key),
                ).fetchone()
        return dict(row)

    def mark_delivered(self, organization_id, repository_id, journal_id, *, remote_id):
        self.connection.execute(
            "UPDATE delivery_journal SET status='PUBLISHED', remote_id=%s, reconciled_at=now(), "
            "attempts=attempts+1, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND journal_id=%s",
            (remote_id, organization_id, repository_id, journal_id),
        )

    def mark_delivery_failed(self, organization_id, repository_id, journal_id):
        self.connection.execute(
            "UPDATE delivery_journal SET status='FAILED', attempts=attempts+1, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND journal_id=%s",
            (organization_id, repository_id, journal_id),
        )

    def deliveries(self, organization_id, repository_id, environment, *, channel=None):
        if channel:
            rows = self.connection.execute(
                "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s AND channel=%s",
                (organization_id, repository_id, environment, channel),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s AND environment=%s",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- audit --------------------------------------------------

    def append_audit(self, organization_id, repository_id, *, actor, event_type, reference_type=None, reference_id=None, payload=None):
        self.connection.execute(
            "INSERT INTO audit_events (organization_id, repository_id, actor, event_type, reference_type, reference_id, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (organization_id, repository_id, actor, event_type, reference_type, reference_id, self._Jsonb(payload or {})),
        )

    def audit_events(self, organization_id, repository_id=None):
        if repository_id:
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE organization_id=%s AND repository_id=%s ORDER BY created_at",
                (organization_id, repository_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE organization_id=%s ORDER BY created_at",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- service tokens (public API authentication) -------------------------

    def create_service_token(self, token_id, secret_hash, organization_id, repository_id,
                             *, environment=None, description=None, expires_at=None):
        """Persist a token's hash. The secret itself is never stored."""
        self.connection.execute(
            "INSERT INTO api_service_tokens "
            "(token_id, secret_hash, organization_id, repository_id, environment, description, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (token_id, secret_hash, organization_id, repository_id, environment, description, expires_at),
        )
        return token_id

    def get_service_token(self, token_id):
        row = self.connection.execute(
            "SELECT token_id, secret_hash, organization_id, repository_id, environment, "
            "expires_at, revoked_at FROM api_service_tokens WHERE token_id=%s",
            (token_id,),
        ).fetchone()
        return dict(row) if row else None

    def revoke_service_token(self, token_id):
        self.connection.execute(
            "UPDATE api_service_tokens SET revoked_at=now() WHERE token_id=%s AND revoked_at IS NULL",
            (token_id,),
        )

    # -- idempotent event receipts -------------------------------------------

    def get_event_receipt(self, organization_id, repository_id, event_id):
        """Idempotency keys are per tenant: the same key in two tenants is two keys."""
        row = self.connection.execute(
            "SELECT * FROM event_receipts "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (organization_id, repository_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def record_event_receipt(self, event_id, organization_id, repository_id, environment,
                             *, status, response, payload_hash, resource_kind=None, resource_id=None):
        """Claim an idempotency key. Returns None if the key is already taken."""
        row = self.connection.execute(
            "INSERT INTO event_receipts "
            "(event_id, organization_id, repository_id, environment, status, response, "
            "payload_hash, resource_kind, resource_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, event_id) DO NOTHING RETURNING *",
            (event_id, organization_id, repository_id, environment, status,
             self._Jsonb(response), payload_hash, resource_kind, resource_id),
        ).fetchone()
        return dict(row) if row else None

    # -- reviews ---------------------------------------------------------------

    def create_review(self, organization_id, repository_id, environment, *, review_id,
                      decision, pull_number=None, commit_sha=None, enforcement_mode=None,
                      risk_score=None, evidence_coverage=None, payload=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute(
            "INSERT INTO reviews (review_id, organization_id, repository_id, environment, "
            "pull_number, commit_sha, decision, enforcement_mode, risk_score, evidence_coverage, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, review_id) DO NOTHING RETURNING *",
            (review_id, organization_id, repository_id, environment, pull_number, commit_sha,
             decision, enforcement_mode, risk_score, evidence_coverage, self._Jsonb(payload or {})),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT * FROM reviews "
                "WHERE organization_id=%s AND repository_id=%s AND review_id=%s",
                (organization_id, repository_id, review_id),
            ).fetchone()
        return dict(row)

    def get_review(self, organization_id, repository_id, review_id):
        row = self.connection.execute(
            "SELECT * FROM reviews WHERE review_id=%s AND organization_id=%s AND repository_id=%s",
            (review_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_reviews(self, organization_id, repository_id, *, environment=None, limit=25, offset=0):
        return self._paged(
            "reviews", "review_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
        )

    # -- tenant-scoped paginated reads ----------------------------------------

    def _paged(self, table, id_column, organization_id, repository_id, *, environment=None,
               limit=25, offset=0, extra_sql="", extra_params=()):
        """Deterministically ordered, tenant-scoped page plus a total count.

        ``table`` and ``id_column`` are internal identifiers chosen by this
        module, never caller input; all caller-supplied values are bound.
        """
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        if extra_sql:
            where += extra_sql
            params.extend(extra_params)

        total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM {table} WHERE {where}", tuple(params)
        ).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM {table} WHERE {where} "
            f"ORDER BY created_at DESC, {id_column} ASC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def get_deployment(self, organization_id, repository_id, deployment_id):
        row = self.connection.execute(
            "SELECT * FROM deployments WHERE deployment_id=%s AND organization_id=%s AND repository_id=%s",
            (deployment_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_deployments(self, organization_id, repository_id, *, environment=None,
                         status=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if status is not None:
            extra_sql, extra_params = (" AND status=%s", (status,))
        return self._paged(
            "deployments", "deployment_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=extra_sql, extra_params=extra_params,
        )

    def list_anomalies(self, organization_id, repository_id, *, environment=None,
                       deployment_id=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if deployment_id is not None:
            extra_sql, extra_params = (" AND deployment_id=%s", (deployment_id,))
        return self._paged(
            "anomalies", "anomaly_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=extra_sql, extra_params=extra_params,
        )

    def get_rca(self, organization_id, repository_id, rca_id):
        row = self.connection.execute(
            "SELECT * FROM rca_reports "
            "WHERE organization_id=%s AND repository_id=%s AND rca_id=%s",
            (organization_id, repository_id, rca_id),
        ).fetchone()
        return dict(row) if row else None

    def get_anomaly(self, organization_id, repository_id, anomaly_id):
        row = self.connection.execute(
            "SELECT * FROM anomalies WHERE anomaly_id=%s AND organization_id=%s AND repository_id=%s",
            (anomaly_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def get_incident_scoped(self, organization_id, repository_id, incident_id):
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_id=%s AND organization_id=%s AND repository_id=%s",
            (incident_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_incidents(self, organization_id, repository_id, *, environment=None, limit=25, offset=0):
        return self._paged(
            "incidents", "incident_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
        )

    def list_observations(self, organization_id, repository_id, *, environment=None,
                          deployment_id=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if deployment_id is not None:
            extra_sql, extra_params = (" AND deployment_id=%s", (deployment_id,))
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        where += extra_sql
        params.extend(extra_params)
        total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM monitoring_observations WHERE {where} "
            "ORDER BY observed_at DESC, observation_id ASC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def lineage_for_model(self, organization_id, repository_id, model, *, environment=None):
        where = "organization_id=%s AND repository_id=%s AND model=%s"
        params = [organization_id, repository_id, model]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        rows = self.connection.execute(
            f"SELECT * FROM lineage_records WHERE {where} ORDER BY created_at DESC, lineage_id ASC",
            tuple(params),
        ).fetchall()
        records = [dict(r) for r in rows]
        for record in records:
            edges = self.connection.execute(
                "SELECT upstream_model, downstream_model FROM lineage_edges "
                "WHERE organization_id=%s AND repository_id=%s AND lineage_id=%s "
                "ORDER BY upstream_model, downstream_model",
                (organization_id, repository_id, record["lineage_id"]),
            ).fetchall()
            record["edges"] = [dict(e) for e in edges]
        return records

    def kpi_impact_for_kpi(self, organization_id, repository_id, kpi_name, *, environment=None,
                           limit=25, offset=0):
        return self._paged(
            "kpi_impact", "kpi_impact_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=" AND kpi_name=%s", extra_params=(kpi_name,),
        )

    def repository_settings(self, organization_id, repository_id):
        environments = self.connection.execute(
            "SELECT environment, connected, created_at FROM environments "
            "WHERE organization_id=%s AND repository_id=%s ORDER BY environment",
            (organization_id, repository_id),
        ).fetchall()
        return {
            "organization_id": organization_id,
            "repository_id": repository_id,
            "environments": [dict(r) for r in environments],
        }

    def evidence_coverage(self, organization_id, repository_id, *, environment=None):
        """Coverage counts derived from stored evidence, never fabricated."""
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        evidence_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM evidence WHERE {where}", tuple(params)
        ).fetchone()["total"]
        observation_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        baseline_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM metadata_baselines WHERE {where}", tuple(params)
        ).fetchone()["total"]
        incomplete = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations "
            f"WHERE {where} AND (evidence_coverage IS NULL OR evidence_coverage <> 'COMPLETE')",
            tuple(params),
        ).fetchone()["total"]
        if observation_total == 0 and baseline_total == 0:
            state = "UNKNOWN"
        elif incomplete == 0:
            state = "COMPLETE"
        else:
            state = "INCOMPLETE"
        return {
            "coverage": state,
            "evidence_records": evidence_total,
            "observations": observation_total,
            "baselines": baseline_total,
            "observations_missing_complete_evidence": incomplete,
        }

    def monitoring_status(self, organization_id, repository_id, *, environment=None):
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        latest = self.connection.execute(
            f"SELECT MAX(observed_at) AS latest FROM monitoring_observations WHERE {where}",
            tuple(params),
        ).fetchone()["latest"]
        observations = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        open_anomalies = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM anomalies WHERE {where}", tuple(params)
        ).fetchone()["total"]
        open_incidents = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM incidents WHERE {where} AND status <> 'resolved'",
            tuple(params),
        ).fetchone()["total"]
        coverage = self.evidence_coverage(organization_id, repository_id, environment=environment)
        # Missing evidence degrades coverage, never health.
        if open_incidents > 0:
            health = "DEGRADED"
        elif open_anomalies > 0:
            health = "ANOMALOUS"
        elif observations > 0:
            health = "HEALTHY"
        else:
            health = "UNKNOWN"
        return {
            "health": health,
            "observations": observations,
            "anomalies": open_anomalies,
            "unresolved_incidents": open_incidents,
            "latest_observation_at": latest,
            "evidence_coverage": coverage["coverage"],
        }

    def outbox_stats(self, organization_id=None, repository_id=None):
        if organization_id and repository_id:
            rows = self.connection.execute(
                "SELECT state, COUNT(*) AS total FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s GROUP BY state",
                (organization_id, repository_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT state, COUNT(*) AS total FROM outbox_events GROUP BY state"
            ).fetchall()
        return {row["state"]: row["total"] for row in rows}

    def close(self):
        self.connection.close()
