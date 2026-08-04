"""Real-PostgreSQL lifecycle-store tests.

These require an actual PostgreSQL server reachable via
RELIUM_TEST_POSTGRES_DSN. They are skipped (not failed) when that variable
is unset so `python -m unittest discover` still works on a machine without
PostgreSQL; CI always sets it against a real postgres service container
(see .github/workflows/test.yml). No test here may substitute SQLite,
an in-memory store, or a mock connection for the real adapter.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)

    def test_apply_migrations_from_empty_database(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        tables = {
            row["table_name"]
            for row in store.connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
        }
        expected = {
            "organizations", "repositories", "environments", "configuration_versions",
            "evidence", "deployments", "deployment_transitions", "metadata_baselines",
            "monitoring_observations", "anomalies", "incidents", "rca_reports",
            "rca_evidence_links", "lineage_records", "lineage_edges", "kpi_impact",
            "event_receipts", "outbox_events", "outbox_dead_letters", "delivery_journal",
            "audit_events", "retention_tombstones", "schema_migrations",
        }
        self.assertTrue(expected.issubset(tables), f"missing tables: {expected - tables}")
        store.close()

    def test_apply_migrations_twice_is_safe(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from agent.postgres_migrate import applied_versions

        store = PostgresLifecycleStore(DSN)
        first = applied_versions(store.connection)
        store.ensure_schema()
        store.ensure_schema()
        second = applied_versions(store.connection)
        self.assertEqual(first, second)
        store.close()

    def test_invalid_migration_rolls_back_atomically(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        bad_dir = Path(__file__).with_name("agent") / "migrations" / "postgres"
        bad_file = bad_dir / "9999_broken_for_test.sql"
        bad_file.write_text("CREATE TABLE this_should_not_persist (x INTEGER); NOT VALID SQL HERE;", encoding="utf-8")
        try:
            with self.assertRaises(Exception):
                store.ensure_schema()
            exists = store.connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='this_should_not_persist'"
            ).fetchone()
            self.assertIsNone(exists, "partially-applied broken migration was not rolled back")
            recorded = store.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=9999"
            ).fetchone()
            self.assertIsNone(recorded)
        finally:
            bad_file.unlink()
            store.close()


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresContractParityTests(unittest.TestCase):
    """The same behavioral contract as test_lifecycle_store.py's SQLite tests, run against real PostgreSQL."""

    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-1", "repo-1", "prod")
        self.store.ensure_tenant("org-2", "repo-2", "prod")

    def tearDown(self):
        self.store.close()

    def test_evidence_is_immutable_and_tenant_scoped(self):
        evidence = self.store.append_evidence("org-1", "repo-1", "prod", {"kind": "manifest", "hash": "abc"})
        self.assertEqual(evidence["payload"]["hash"], "abc")
        with self.assertRaises(ValueError):
            self.store.append_evidence("org-1", "repo-1", "prod", {"kind": "manifest", "hash": "abc"}, evidence_id=evidence["evidence_id"])
        self.assertEqual(self.store.list_evidence("org-2", "repo-2", "prod"), [])

    def test_deployment_transitions_are_append_only_and_idempotent(self):
        deployment = self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-1", "merge_sha": "sha"})
        self.assertEqual(deployment["deployment_id"], "dep-1")
        self.assertEqual(self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-1", "merge_sha": "sha"}), deployment)
        self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "reviewed")
        self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "approved")
        with self.assertRaises(ValueError):
            self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "reviewed")
        self.assertEqual(len(self.store.transitions("org-1", "repo-1", "prod", "dep-1")), 1)

    def test_outbox_claim_is_idempotent_and_tenant_scoped(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-2"})
        event = self.store.claim_outbox("org-1", "repo-1", "prod", "worker-1")
        self.assertEqual(event["deployment_id"], "dep-2")
        self.assertIsNone(self.store.claim_outbox("org-1", "repo-1", "prod", "worker-2"))
        self.assertIsNone(self.store.claim_outbox("org-2", "repo-2", "prod", "worker-1"))

    def test_policy_detector_and_threshold_versions_are_persisted(self):
        versions = self.store.record_versions("org-1", "repo-1", "prod", policy="policy-v1", detector="detector-v1", threshold="threshold-v1")
        self.assertEqual(versions["policy_version"], "policy-v1")
        self.assertEqual(self.store.latest_versions("org-1", "repo-1", "prod")["detector_version"], "detector-v1")

    def test_disconnect_and_delete_tombstone(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-3"})
        self.store.disconnect_repository("org-1", "repo-1")
        with self.assertRaises(ValueError):
            self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-4"})
        tombstone = self.store.delete_tenant("org-1")
        self.assertEqual(tombstone["organization_id"], "org-1")
        self.assertEqual(self.store.list_evidence("org-1", "repo-1", "prod"), [])


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresTenantIsolationTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-a", "repo-a", "prod")
        self.store.ensure_tenant("org-b", "repo-b", "prod")

    def tearDown(self):
        self.store.close()

    def test_organization_cannot_read_or_mutate_another(self):
        self.store.create_deployment("org-a", "repo-a", "prod", {"deployment_id": "dep-a"})
        with self.assertRaises(ValueError):
            self.store.append_transition("org-b", "repo-b", "prod", "dep-a", "reviewed")
        self.assertEqual(self.store.list_evidence("org-b", "repo-b", "prod"), [])

    def test_repository_cannot_use_environment_or_deployment_ids_from_another_repository(self):
        self.store.create_deployment("org-a", "repo-a", "prod", {"deployment_id": "dep-shared-id"})
        with self.assertRaises(ValueError):
            # org-b/repo-b never had this deployment created under its own tenancy
            self.store.append_transition("org-b", "repo-b", "prod", "dep-shared-id", "reviewed")

    def test_incident_rca_and_evidence_ids_do_not_cross_tenant_boundaries(self):
        self.store.create_deployment("org-a", "repo-a", "prod", {"deployment_id": "dep-a"})
        anomaly_a = self.store.create_anomaly("org-a", "repo-a", "prod", deployment_id="dep-a", kind="k", payload={})
        incident_a = self.store.create_incident("org-a", "repo-a", "prod", deployment_id="dep-a", anomaly_id=anomaly_a["anomaly_id"])
        self.assertEqual(self.store.anomalies("org-b", "repo-b", "prod"), [])
        self.assertIsNone(self.store.get_incident("nonexistent-in-org-b"))
        # incident lookup by ID is possible (incidents are globally-unique keys), but
        # listing is always tenant-scoped, so org-b's queries never surface org-a's rows.
        self.assertEqual(
            [i for i in self.store.anomalies("org-b", "repo-b", "prod")],
            [],
        )

    def test_delivery_journal_cannot_publish_into_another_repository(self):
        j = self.store.record_delivery("org-a", "repo-a", "prod", channel="github", event_key="pr-1", payload={})
        self.assertEqual(self.store.deliveries("org-b", "repo-b", "prod"), [])
        self.assertNotEqual(j["repository_id"], "repo-b")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresOutboxConcurrencyTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.Store = PostgresLifecycleStore
        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-1", "repo-1", "prod")

    def tearDown(self):
        self.store.close()

    def test_state_and_outbox_commit_atomically(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-atomic"})
        events = self.store.connection.execute(
            "SELECT 1 FROM outbox_events WHERE deployment_id='dep-atomic'"
        ).fetchall()
        self.assertEqual(len(events), 1)

    def test_rollback_leaves_neither_state_nor_outbox(self):
        with self.assertRaises(ValueError):
            self.store.create_deployment("org-nonexistent", "repo-1", "prod", {"deployment_id": "dep-should-not-exist"})
        dep = self.store.connection.execute(
            "SELECT 1 FROM deployments WHERE deployment_id='dep-should-not-exist'"
        ).fetchone()
        outbox = self.store.connection.execute(
            "SELECT 1 FROM outbox_events WHERE deployment_id='dep-should-not-exist'"
        ).fetchone()
        self.assertIsNone(dep)
        self.assertIsNone(outbox)

    def test_duplicate_event_creates_one_effective_transition(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-dup"})
        self.store.append_transition("org-1", "repo-1", "prod", "dep-dup", "reviewed")
        self.store.append_transition("org-1", "repo-1", "prod", "dep-dup", "reviewed")  # duplicate/no-op
        self.assertEqual(len(self.store.transitions("org-1", "repo-1", "prod", "dep-dup")), 0)

    def test_two_workers_cannot_effectively_process_the_same_event(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-race"})
        store_a = self.Store(DSN)
        store_b = self.Store(DSN)
        results = {}

        def claim(name, store):
            results[name] = store.claim_outbox("org-1", "repo-1", "prod", name)

        t1 = threading.Thread(target=claim, args=("worker-a", store_a))
        t2 = threading.Thread(target=claim, args=("worker-b", store_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        store_a.close()
        store_b.close()
        claimed = [r for r in results.values() if r is not None]
        self.assertEqual(len(claimed), 1, "FOR UPDATE SKIP LOCKED allowed the same event to be claimed twice")

    def test_expired_claims_recover(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-expire"})
        event = self.store.claim_outbox("org-1", "repo-1", "prod", "worker-1")
        self.store.connection.execute(
            "UPDATE outbox_events SET lease_expires_at = now() - interval '1 hour' WHERE event_id=%s",
            (event["event_id"],),
        )
        self.store.connection.commit()
        recovered = self.store.claim_outbox("org-1", "repo-1", "prod", "worker-2")
        self.assertEqual(recovered["event_id"], event["event_id"])

    def test_attempts_and_dead_letter_survive_restart(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-dl"})
        event = self.store.claim_outbox("org-1", "repo-1", "prod", "worker-1")
        self.store.fail_outbox(event["event_id"], error="boom", max_attempts=1, retry_backoff_seconds=0)
        self.store.close()
        # Simulate a process restart: fresh connection, fresh store instance.
        reconnected = self.Store(DSN)
        dl = reconnected.dead_letters("org-1", "repo-1", "prod")
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["attempts"], 1)
        reconnected.close()
        self.store = self.Store(DSN)  # so tearDown has a live connection to close

    def test_crash_after_commit_resumes_through_the_outbox(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-crash"})
        self.store.close()
        resumed = self.Store(DSN)
        event = resumed.claim_outbox("org-1", "repo-1", "prod", "worker-after-crash")
        self.assertIsNotNone(event)
        self.assertEqual(event["deployment_id"], "dep-crash")
        resumed.close()
        self.store = self.Store(DSN)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresMonitoringAndRcaTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-1", "repo-1", "prod")
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-1"})

    def tearDown(self):
        self.store.close()

    def test_monitoring_observations_persist_and_reload(self):
        self.store.append_observation("org-1", "repo-1", "prod", deployment_id="dep-1", model="fct_orders", metric="row_count", payload={"value": 100})
        obs = self.store.observations("org-1", "repo-1", "prod", deployment_id="dep-1")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["payload"]["value"], 100)

    def test_cardinality_collapse_anomaly_persists_correctly(self):
        anomaly = self.store.create_anomaly(
            "org-1", "repo-1", "prod", deployment_id="dep-1", kind="cardinality_collapse",
            payload={"rows_before": 10000, "rows_after": 9990, "stable_row_count": True},
        )
        again = self.store.create_anomaly(
            "org-1", "repo-1", "prod", deployment_id="dep-1", kind="cardinality_collapse", payload={"different": True},
        )
        self.assertEqual(anomaly["anomaly_id"], again["anomaly_id"])

    def test_incident_creation_is_idempotent(self):
        anomaly = self.store.create_anomaly("org-1", "repo-1", "prod", deployment_id="dep-1", kind="k", payload={})
        first = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-1", anomaly_id=anomaly["anomaly_id"], incident_id="inc-1")
        second = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-1", anomaly_id=anomaly["anomaly_id"], incident_id="inc-1")
        self.assertEqual(first, second)

    def test_rca_work_survives_restart(self):
        anomaly = self.store.create_anomaly("org-1", "repo-1", "prod", deployment_id="dep-1", kind="k", payload={})
        incident = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-1", anomaly_id=anomaly["anomaly_id"])
        self.store.create_rca(incident["incident_id"], "org-1", "repo-1", "prod", status="completed", primary_cause={"model": "x"}, confidence="high")
        self.store.close()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        resumed = PostgresLifecycleStore(DSN)
        rcas = resumed.rca_for_incident(incident["incident_id"])
        self.assertEqual(len(rcas), 1)
        self.assertEqual(rcas[0]["primary_cause"]["model"], "x")
        resumed.close()
        self.store = PostgresLifecycleStore(DSN)

    def test_exactly_one_completed_rca_is_retained(self):
        anomaly = self.store.create_anomaly("org-1", "repo-1", "prod", deployment_id="dep-1", kind="k", payload={})
        incident = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-1", anomaly_id=anomaly["anomaly_id"])
        first = self.store.create_rca(incident["incident_id"], "org-1", "repo-1", "prod", status="completed", primary_cause={"model": "a"})
        second = self.store.create_rca(incident["incident_id"], "org-1", "repo-1", "prod", status="completed", primary_cause={"model": "b"})
        self.assertEqual(first["rca_id"], second["rca_id"])
        self.assertEqual(first["primary_cause"]["model"], "a")

    def test_evidence_links_alternatives_and_confidence_reload_correctly(self):
        ev = self.store.append_evidence("org-1", "repo-1", "prod", {"kind": "manifest"})
        anomaly = self.store.create_anomaly("org-1", "repo-1", "prod", deployment_id="dep-1", kind="k", payload={})
        incident = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-1", anomaly_id=anomaly["anomaly_id"])
        rca = self.store.create_rca(
            incident["incident_id"], "org-1", "repo-1", "prod", status="completed",
            primary_cause={"model": "a"}, alternative_causes=[{"model": "b"}],
            confidence="medium", evidence_links=[(ev["evidence_id"], "supporting")],
        )
        links = self.store.connection.execute(
            "SELECT * FROM rca_evidence_links WHERE rca_id=%s", (rca["rca_id"],)
        ).fetchall()
        self.assertEqual(len(links), 1)
        self.assertEqual(rca["alternative_causes"][0]["model"], "b")
        self.assertEqual(rca["confidence"], "medium")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresBackupRestoreTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-1", "repo-1", "prod")

    def tearDown(self):
        self.store.close()

    def test_backup_and_restore_preserve_normalized_hashes(self):
        pg_dump = _pg_bin("pg_dump")
        if pg_dump is None:
            self.skipTest("pg_dump not found on PATH")
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-backup"})
        anomaly = self.store.create_anomaly("org-1", "repo-1", "prod", deployment_id="dep-backup", kind="k", payload={"v": 1})
        incident = self.store.create_incident("org-1", "repo-1", "prod", deployment_id="dep-backup", anomaly_id=anomaly["anomaly_id"])
        self.store.create_rca(incident["incident_id"], "org-1", "repo-1", "prod", status="completed", primary_cause={"model": "a"})
        before = _normalized_snapshot(self.store)
        self.store.close()

        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "dump.sql"
            subprocess.run([pg_dump, "--no-owner", "--no-privileges", "-f", str(dump_path), DSN], check=True, capture_output=True)
            _reset_schema(DSN)
            psql = _pg_bin("psql")
            subprocess.run([psql, DSN, "-f", str(dump_path)], check=True, capture_output=True)

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        restored = PostgresLifecycleStore(DSN)
        after = _normalized_snapshot(restored)
        self.assertEqual(before, after)
        restored.close()
        self.store = PostgresLifecycleStore(DSN)


def _pg_bin(name):
    import shutil

    found = shutil.which(name)
    if found:
        return found
    default = Path(r"C:\Program Files\PostgreSQL\18\bin") / f"{name}.exe"
    return str(default) if default.exists() else None


def _normalized_snapshot(store):
    tables = ["deployments", "anomalies", "incidents", "rca_reports", "outbox_events", "delivery_journal"]
    snapshot = {}
    for table in tables:
        rows = store.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        normalized = []
        for row in rows:
            normalized.append({k: v for k, v in row.items() if not k.endswith("_at")})
        snapshot[table] = normalized
    return snapshot


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class PostgresRetentionAndSecurityTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant("org-1", "repo-1", "prod")
        self.store.ensure_tenant("org-2", "repo-2", "prod")

    def tearDown(self):
        self.store.close()

    def test_tenant_deletion_follows_documented_policy_and_cross_tenant_records_survive(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-del"})
        self.store.create_deployment("org-2", "repo-2", "prod", {"deployment_id": "dep-keep"})
        self.store.delete_tenant("org-1")
        self.assertIsNotNone(
            self.store.connection.execute("SELECT 1 FROM deployments WHERE deployment_id='dep-keep'").fetchone()
        )
        self.assertIsNone(
            self.store.connection.execute("SELECT 1 FROM deployments WHERE deployment_id='dep-del'").fetchone()
        )

    def test_audit_trail_is_preserved_across_tenant_deletion(self):
        self.store.append_audit("org-1", "repo-1", actor="system", event_type="test.event")
        self.store.delete_tenant("org-1")
        self.assertEqual(len(self.store.audit_events("org-1")), 1)

    def test_sql_injection_payloads_are_treated_as_data_not_code(self):
        payload = {"note": "'; DROP TABLE deployments; --"}
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-inj", **payload})
        self.assertIsNotNone(
            self.store.connection.execute("SELECT 1 FROM deployments WHERE deployment_id='dep-inj'").fetchone()
        )
        still_there = self.store.connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='deployments'"
        ).fetchone()
        self.assertIsNotNone(still_there, "SQL injection payload dropped a table")

    def test_application_role_is_not_a_postgresql_superuser_and_is_least_privileged(self):
        row = self.store.connection.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        self.assertTrue(row["rolcanlogin"], "application role must be able to log in")
        self.assertFalse(row["rolsuper"], "application role must not be a superuser")
        self.assertFalse(row["rolcreatedb"], "application role must not have CREATEDB")
        self.assertFalse(row["rolcreaterole"], "application role must not have CREATEROLE")
        self.assertFalse(row["rolreplication"], "application role must not have replication rights")
        self.assertFalse(row["rolbypassrls"], "application role must not bypass RLS")


if __name__ == "__main__":
    unittest.main()
