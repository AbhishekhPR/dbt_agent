"""Tenant-isolation security suite.

Every test here deliberately uses the SAME identifier in two tenants — the case
the previous isolation tests never exercised, and the reason a cross-tenant
disclosure reached production. Runs against a real PostgreSQL server.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
import uuid
from datetime import datetime, timezone

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

# Each test picks its own identifier and then deliberately uses that SAME
# identifier in both tenants. Per-test values keep the shared-identifier
# property under test while preventing state from leaking between tests.


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


class _Queue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def _now():
    return datetime.now(timezone.utc)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; security suite requires a real server")
class TenantIsolationSecurityTests(unittest.TestCase):
    """Victim = tenant A, attacker = tenant B, both using identical identifiers."""

    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="security-suite",
            job_queue=_Queue(),
            max_body_bytes=1024 * 1024,
            shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0,
            store_pool=cls.pool,
        )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        self.victim = self._tenant("org-victim", "repo-victim")
        self.attacker = self._tenant("org-attacker", "repo-attacker")
        self.shared = f"dep-shared-{uuid.uuid4().hex[:10]}"
        self.shared_key = f"idem-shared-{uuid.uuid4().hex[:10]}"

    def _tenant(self, org, repo):
        from agent.api.auth import generate_token, hash_secret

        # Two machine credentials per tenant. A machine no longer has one
        # undifferentiated capability: `operator_read` reads the dashboard
        # resources, `collector` submits pipeline events. Both are scoped to
        # this tenant, so what these tests prove — that one tenant cannot
        # reach another's data — is unchanged.
        read_id, read_secret, read_token = generate_token()
        ingest_id, ingest_secret, ingest_token = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(org, repo, "prod")
            store.create_service_token(read_id, hash_secret(read_secret), org, repo,
                                       environment="prod", description="security-suite",
                                       scope="operator_read")
            store.create_service_token(ingest_id, hash_secret(ingest_secret), org, repo,
                                       environment="prod", description="security-suite",
                                       scope="collector")
        return {"org": org, "repo": repo,
                "auth": {"Authorization": f"Bearer {read_token}"},
                "ingest_auth": {"Authorization": f"Bearer {ingest_token}"}}

    def _post(self, path, body, tenant, key=None):
        # Ingestion is machine work and carries the collector credential.
        headers = dict(tenant["ingest_auth"])
        headers["Idempotency-Key"] = key or uuid.uuid4().hex
        return self.client.post(path, json=body, headers=headers)

    def _get(self, path, tenant):
        return self.client.get(path, headers=tenant["auth"])

    def _create(self, tenant, deployment_id=None, key=None):
        return self._post("/api/deployments/events",
                          {"deployment_id": deployment_id or self.shared,
                           "event_type": "created"},
                          tenant, key=key)

    # -- 1. both tenants may own the same external identifier -----------------

    def test_both_tenants_create_the_same_deployment_id_successfully(self):
        a = self._create(self.victim)
        b = self._create(self.attacker)
        self.assertEqual(a.status_code, 202, a.text)
        self.assertEqual(b.status_code, 202, b.text)
        with self.pool.acquire() as store:
            rows = store.connection.execute(
                "SELECT organization_id FROM deployments WHERE deployment_id=%s ORDER BY organization_id",
                (self.shared,),
            ).fetchall()
        self.assertEqual([r["organization_id"] for r in rows],
                         ["org-attacker", "org-victim"])

    # -- 2-4. the exact live disclosure probe, now safe ------------------------

    def test_attacker_cannot_read_victim_deployment(self):
        self._create(self.victim)
        self._advance_victim_to_healthy()
        response = self._get(f"/api/deployments/{self.shared}", self.attacker)
        self.assertEqual(response.status_code, 404)

    def test_attacker_cannot_transition_victim_deployment(self):
        self._create(self.victim)
        self._advance_victim_to_healthy()
        # The attacker acts only within its own tenant; the victim is untouched.
        self._create(self.attacker)
        self._post("/api/deployments/events",
                   {"deployment_id": self.shared, "event_type": "approved"}, self.attacker)
        victim = self._get(f"/api/deployments/{self.shared}", self.victim).json()
        self.assertEqual(victim["status"], "healthy",
                         "victim deployment state changed after an attacker write")

    def test_attacker_cannot_infer_victim_deployment_status(self):
        """The regression fixture for the live finding.

        The victim owns the shared identifier at status 'healthy'. The attacker submits the same
        identifier and must observe nothing that distinguishes it from an
        identifier that exists nowhere.
        """
        self._create(self.victim)
        self._advance_victim_to_healthy()

        taken = self._create(self.attacker, deployment_id=self.shared)
        free_id = f"dep-free-{uuid.uuid4().hex[:8]}"
        free = self._create(self.attacker, deployment_id=free_id)

        self.assertEqual(taken.status_code, free.status_code)
        self.assertEqual(taken.json()["status"], free.json()["status"])
        self.assertNotEqual(taken.json()["status"], "healthy",
                            "victim lifecycle state leaked to the attacker")

        victim = self._get(f"/api/deployments/{self.shared}", self.victim).json()
        self.assertEqual(victim["status"], "healthy")

    def _advance_victim_to_healthy(self):
        for state in ("approved", "deployment_started", "deployment_succeeded",
                      "post_deployment_monitoring", "healthy"):
            self._post("/api/deployments/events",
                       {"deployment_id": self.shared, "event_type": state}, self.victim)

    # -- 5. byte-equivalent non-disclosing responses --------------------------

    def test_existing_and_nonexistent_foreign_ids_are_byte_equivalent(self):
        self._create(self.victim)
        foreign = self._get(f"/api/deployments/{self.shared}", self.attacker)
        absent = self._get(f"/api/deployments/dep-{uuid.uuid4().hex}", self.attacker)
        self.assertEqual(foreign.status_code, absent.status_code)
        foreign_body = {k: v for k, v in foreign.json().items() if k != "request_id"}
        absent_body = {k: v for k, v in absent.json().items() if k != "request_id"}
        self.assertEqual(json.dumps(foreign_body, sort_keys=True),
                         json.dumps(absent_body, sort_keys=True))

    def test_no_raw_database_error_escapes(self):
        self._create(self.victim)
        for response in (
            self._post("/api/deployments/events",
                       {"deployment_id": self.shared, "event_type": "approved"}, self.attacker),
            self._get(f"/api/deployments/{self.shared}", self.attacker),
        ):
            self.assertNotEqual(response.status_code, 500, response.text)
            body = response.text.lower()
            for needle in ("duplicate key", "violates", "psycopg", "traceback",
                           "constraint", "select ", "insert into"):
                self.assertNotIn(needle, body)

    # -- 6-13. cross-tenant references are rejected ---------------------------

    def test_cross_tenant_anomaly_reference_is_rejected(self):
        self._create(self.victim)
        response = self._post("/api/anomalies",
                              {"deployment_id": self.shared, "kind": "k", "severity": "high",
                               "evidence": {}}, self.attacker)
        self.assertEqual(response.status_code, 404, response.text)

    def test_cross_tenant_incident_reference_is_rejected(self):
        self._create(self.victim)
        anomaly = self._post("/api/anomalies",
                             {"deployment_id": self.shared, "kind": "k", "severity": "high",
                              "evidence": {}}, self.victim).json()
        response = self._post("/api/incidents",
                              {"anomaly_id": anomaly["anomaly_id"]}, self.attacker)
        self.assertEqual(response.status_code, 404, response.text)

    def test_cross_tenant_incident_detail_is_not_found(self):
        self._create(self.victim)
        anomaly = self._post("/api/anomalies",
                             {"deployment_id": self.shared, "kind": "k", "severity": "high",
                              "evidence": {}}, self.victim).json()
        incident = self._post("/api/incidents",
                              {"anomaly_id": anomaly["anomaly_id"]}, self.victim).json()
        for path in (f"/api/incidents/{incident['incident_id']}",
                     f"/api/incidents/{incident['incident_id']}/rca"):
            self.assertEqual(self._get(path, self.attacker).status_code, 404)

    def test_cross_tenant_delivery_journal_is_not_visible(self):
        with self.pool.acquire() as store:
            store.record_delivery(self.victim["org"], self.victim["repo"], "prod",
                                  channel="github", event_key="shared-key", payload={"x": 1})
        body = self._get("/api/delivery-status", self.attacker).json()
        self.assertEqual(body["channels"], {})

    def test_cross_tenant_repository_settings_is_not_found(self):
        response = self._get(f"/api/repositories/{self.victim['repo']}/settings", self.attacker)
        self.assertEqual(response.status_code, 404)

    # -- 14-15. idempotency keys do not collide across tenants ----------------

    def test_identical_idempotency_keys_in_separate_tenants_do_not_collide(self):
        a = self._create(self.victim, deployment_id="dep-a", key=self.shared_key)
        b = self._create(self.attacker, deployment_id="dep-b", key=self.shared_key)
        self.assertEqual(a.status_code, 202, a.text)
        self.assertEqual(b.status_code, 202, b.text)
        self.assertFalse(a.json().get("replayed"))
        self.assertFalse(b.json().get("replayed"))
        self.assertEqual(a.json()["deployment_id"], "dep-a")
        self.assertEqual(b.json()["deployment_id"], "dep-b")

    def test_concurrent_identical_ids_in_separate_tenants_remain_isolated(self):
        results = {}

        def submit(name, tenant):
            results[name] = self._create(tenant, deployment_id=self.shared,
                                         key=f"race-{name}").status_code

        threads = [threading.Thread(target=submit, args=("victim", self.victim)),
                   threading.Thread(target=submit, args=("attacker", self.attacker))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["victim"], 202)
        self.assertEqual(results["attacker"], 202)
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT COUNT(*) AS c FROM deployments WHERE deployment_id=%s", (self.shared,)
            ).fetchone()["c"]
        self.assertEqual(count, 2)

    # -- 16. the database itself rejects cross-tenant inserts -----------------

    def test_database_constraints_reject_direct_cross_tenant_insert(self):
        self._create(self.victim)
        with self.pool.acquire() as store:
            store.ensure_tenant("org-attacker", "repo-attacker", "prod")
            with self.assertRaises(Exception) as caught:
                store.connection.execute(
                    "INSERT INTO deployment_transitions "
                    "(deployment_id, organization_id, repository_id, environment, "
                    "from_status, to_status, sequence) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (self.shared, "org-attacker", "repo-attacker", "prod", "reviewed", "approved", 1),
                )
            self.assertIn("ForeignKeyViolation", type(caught.exception).__name__)

    def test_database_rejects_cross_tenant_incident_to_anomaly_link(self):
        self._create(self.victim)
        anomaly = self._post("/api/anomalies",
                             {"deployment_id": self.shared, "kind": "k", "severity": "high",
                              "evidence": {}}, self.victim).json()
        with self.pool.acquire() as store:
            with self.assertRaises(Exception) as caught:
                store.connection.execute(
                    "INSERT INTO incidents (incident_id, organization_id, repository_id, "
                    "environment, deployment_id, anomaly_id, status) "
                    "VALUES (%s, %s, %s, %s, NULL, %s, 'open')",
                    (uuid.uuid4().hex, "org-attacker", "repo-attacker", "prod",
                     anomaly["anomaly_id"]),
                )
            self.assertIn("ForeignKeyViolation", type(caught.exception).__name__)

    # -- 17. isolation survives backup/restore --------------------------------

    def test_backup_restore_preserves_isolation_constraints(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        pg_dump = shutil.which("pg_dump") or str(
            Path(r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"))
        psql = shutil.which("psql") or str(
            Path(r"C:\Program Files\PostgreSQL\18\bin\psql.exe"))
        if not Path(pg_dump).exists():
            self.skipTest("pg_dump unavailable")

        # Both tenants own the shared identifier, and the victim additionally
        # owns one the attacker must never be able to reference after restore.
        self._create(self.victim)
        self._create(self.attacker)
        victim_only = f"dep-victim-only-{uuid.uuid4().hex[:8]}"
        self._create(self.victim, deployment_id=victim_only)
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "dump.sql"
            subprocess.run([pg_dump, "--no-owner", "--no-privileges", "-f", str(dump), DSN],
                           check=True, capture_output=True)
            _reset_schema(DSN)
            subprocess.run([psql, DSN, "-f", str(dump)], check=True, capture_output=True)

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        restored = PostgresLifecycleStore(DSN)
        try:
            rows = restored.connection.execute(
                "SELECT COUNT(*) AS c FROM deployments WHERE deployment_id=%s", (self.shared,)
            ).fetchone()["c"]
            self.assertEqual(rows, 2, "both tenants' rows must survive restore")
            # The composite foreign key must still forbid the attacker from
            # referencing a deployment only the victim owns.
            with self.assertRaises(Exception) as caught:
                restored.connection.execute(
                    "INSERT INTO deployment_transitions "
                    "(deployment_id, organization_id, repository_id, environment, "
                    "from_status, to_status, sequence) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (victim_only, "org-attacker", "repo-attacker", "prod", "reviewed", "approved", 9),
                )
            self.assertIn("ForeignKeyViolation", type(caught.exception).__name__)
        finally:
            restored.close()

    # -- 20. same-tenant behaviour does not regress ---------------------------

    def test_same_tenant_behaviour_does_not_regress(self):
        self._create(self.victim)
        transitioned = self._post("/api/deployments/events",
                                  {"deployment_id": self.shared, "event_type": "approved"},
                                  self.victim)
        self.assertEqual(transitioned.status_code, 202)
        self.assertTrue(transitioned.json()["transition_applied"])
        detail = self._get(f"/api/deployments/{self.shared}", self.victim).json()
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(len(detail["transitions"]), 1)

    def test_same_tenant_idempotent_replay_still_works(self):
        key = uuid.uuid4().hex
        first = self._create(self.victim, key=key)
        second = self._create(self.victim, key=key)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["replayed"])


if __name__ == "__main__":
    unittest.main()
