from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.lifecycle_models import ALLOWED_TRANSITIONS


class SQLiteLifecycleStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def ensure_schema(self):
        schema = Path(__file__).with_name("lifecycle_schema.sql").read_text(encoding="utf-8")
        self.connection.executescript(schema)
        self.connection.commit()

    def ensure_tenant(self, organization_id, repository_id, environment):
        self.connection.execute("INSERT OR IGNORE INTO tenants VALUES (?, ?, ?, 1)", (organization_id, repository_id, environment))
        self.connection.commit()

    def append_evidence(self, organization_id, repository_id, environment, payload, *, evidence_id=None):
        self._tenant(organization_id, repository_id, environment)
        evidence_id = evidence_id or str(uuid.uuid4())
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        existing = self.connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if existing:
            raise ValueError("Evidence references are immutable")
        self.connection.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?)", (evidence_id, organization_id, repository_id, environment, serialized, digest))
        self.connection.commit()
        return {"evidence_id": evidence_id, "hash": digest, "payload": payload}

    def list_evidence(self, organization_id, repository_id, environment):
        try:
            self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        except ValueError:
            if not self.connection.execute("SELECT 1 FROM retention_tombstones WHERE organization_id=?", (organization_id,)).fetchone():
                raise
            return []
        return [dict(row) for row in self.connection.execute("SELECT evidence_id, content_hash AS hash, payload FROM evidence WHERE organization_id=? AND repository_id=? AND environment=?", (organization_id, repository_id, environment))]

    def create_deployment(self, organization_id, repository_id, environment, payload):
        self._tenant(organization_id, repository_id, environment)
        deployment_id = payload["deployment_id"]
        existing = self.connection.execute("SELECT payload, status FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if existing:
            return {"deployment_id": deployment_id, **json.loads(existing["payload"]), "status": existing["status"]}
        serialized = json.dumps(payload, sort_keys=True)
        self.connection.execute("INSERT INTO deployments VALUES (?, ?, ?, ?, ?, ?)", (deployment_id, organization_id, repository_id, environment, serialized, "reviewed"))
        self.connection.execute("INSERT INTO outbox_events VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, 0)", (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id, "deployment.reviewed", serialized))
        self.connection.commit()
        return {"deployment_id": deployment_id, **payload, "status": "reviewed"}

    def append_transition(self, organization_id, repository_id, environment, deployment_id, to_status):
        self._tenant(organization_id, repository_id, environment)
        row = self.connection.execute("SELECT status FROM deployments WHERE deployment_id=? AND organization_id=? AND repository_id=? AND environment=?", (deployment_id, organization_id, repository_id, environment)).fetchone()
        if not row:
            raise ValueError("Unknown deployment")
        if to_status == row["status"]:
            return
        if to_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
            raise ValueError(f"Invalid deployment transition {row['status']} -> {to_status}")
        seq = self.connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM deployment_transitions WHERE deployment_id=?", (deployment_id,)).fetchone()["next"]
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("UPDATE deployments SET status=? WHERE deployment_id=?", (to_status, deployment_id))
        self.connection.execute("INSERT INTO deployment_transitions (deployment_id, organization_id, repository_id, environment, from_status, to_status, sequence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (deployment_id, organization_id, repository_id, environment, row["status"], to_status, seq, now))
        self.connection.execute("INSERT OR IGNORE INTO outbox_events VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, 0)", (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id, f"deployment.{to_status}", json.dumps({"deployment_id": deployment_id, "status": to_status})))
        self.connection.commit()

    def transitions(self, organization_id, repository_id, environment, deployment_id):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        return [dict(row) for row in self.connection.execute("SELECT * FROM deployment_transitions WHERE deployment_id=? ORDER BY sequence", (deployment_id,))]

    def claim_outbox(self, organization_id, repository_id, environment, worker):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute("SELECT * FROM outbox_events WHERE organization_id=? AND repository_id=? AND environment=? AND state='PENDING' ORDER BY rowid LIMIT 1", (organization_id, repository_id, environment)).fetchone()
        if not row:
            return None
        self.connection.execute("UPDATE outbox_events SET state='CLAIMED', lease_owner=?, attempts=attempts+1 WHERE event_id=? AND state='PENDING'", (worker, row["event_id"]))
        self.connection.commit()
        return dict(row)

    def disconnect_repository(self, organization_id, repository_id):
        self.connection.execute("UPDATE tenants SET connected=0 WHERE organization_id=? AND repository_id=?", (organization_id, repository_id))
        self.connection.commit()

    def record_versions(self, organization_id, repository_id, environment, *, policy, detector, threshold):
        self._tenant(organization_id, repository_id, environment)
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("INSERT OR REPLACE INTO configuration_versions VALUES (?, ?, ?, ?, ?, ?, ?)", (organization_id, repository_id, environment, policy, detector, threshold, now))
        self.connection.commit()
        return {"policy_version": policy, "detector_version": detector, "threshold_version": threshold}

    def latest_versions(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute("SELECT policy_version, detector_version, threshold_version FROM configuration_versions WHERE organization_id=? AND repository_id=? AND environment=?", (organization_id, repository_id, environment)).fetchone()
        return dict(row) if row else {}

    def list_lineage(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        return [json.loads(row["payload"]) | {"lineage_id": row["lineage_id"]} for row in self.connection.execute("SELECT lineage_id, payload FROM lineage_records WHERE organization_id=? AND repository_id=? AND environment=?", (organization_id, repository_id, environment))]

    def delete_tenant(self, organization_id):
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("INSERT OR REPLACE INTO retention_tombstones VALUES (?, ?)", (organization_id, now))
        for table in ("outbox_events", "deployment_transitions", "deployments", "evidence", "configuration_versions", "tenants"):
            self.connection.execute(f"DELETE FROM {table} WHERE organization_id=?", (organization_id,))
        self.connection.commit()
        return {"organization_id": organization_id, "deleted_at": now}

    def _tenant(self, organization_id, repository_id, environment, *, allow_disconnected=False):
        row = self.connection.execute("SELECT connected FROM tenants WHERE organization_id=? AND repository_id=? AND environment=?", (organization_id, repository_id, environment)).fetchone()
        if not row or (not allow_disconnected and not row["connected"]):
            raise ValueError("Unknown, disconnected, or unauthorized tenant")
