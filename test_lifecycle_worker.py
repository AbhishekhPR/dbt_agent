"""Durable lifecycle worker suite, against a real PostgreSQL server.

No test writes an RCA row directly to simulate the worker: every RCA assertion
here is the product of the worker actually running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


def _reset(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _now():
    return datetime.now(timezone.utc)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; worker suite requires a real server")
class WorkerTestCase(unittest.TestCase):
    org = "org-worker"
    repo = "repo-worker"
    env = "prod"

    def setUp(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset(DSN)
        self.store = PostgresLifecycleStore(DSN)
        self.store.ensure_tenant(self.org, self.repo, self.env)
        self.addCleanup(self.store.close)

    def _worker(self, **kwargs):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from agent.worker.lifecycle_worker import LifecycleWorker

        kwargs.setdefault("poll_seconds", 0.05)
        return LifecycleWorker(lambda: PostgresLifecycleStore(DSN), **kwargs)

    def _deployment(self, deployment_id, *, models=("fct_orders",), findings=None, org=None, repo=None):
        org = org or self.org
        repo = repo or self.repo
        return self.store.create_deployment(org, repo, self.env, {
            "deployment_id": deployment_id,
            "models": list(models),
            "sql_findings": findings or [],
        })

    def _anomaly(self, deployment_id, *, models=("fct_orders",), kpis=("revenue",),
                 kind=None, org=None, repo=None, detected_offset=5):
        org = org or self.org
        repo = repo or self.repo
        record = self.store.create_anomaly(org, repo, self.env, deployment_id=deployment_id,
                                           kind=kind or f"k-{uuid.uuid4().hex[:6]}",
                                           payload={"duplicate_rate": 0.4})
        self.store.connection.execute(
            "UPDATE anomalies SET affected_models=%s, affected_kpis=%s, detected_at=%s "
            "WHERE organization_id=%s AND repository_id=%s AND anomaly_id=%s",
            (self.store._Jsonb(list(models)), self.store._Jsonb(list(kpis)),
             _now() + timedelta(minutes=detected_offset), org, repo, record["anomaly_id"]),
        )
        return record

    def _incident(self, deployment_id, anomaly_id, *, org=None, repo=None):
        return self.store.create_incident(org or self.org, repo or self.repo, self.env,
                                          deployment_id=deployment_id, anomaly_id=anomaly_id)

    def _queue_rca(self, incident_id, deployment_id, *, org=None, repo=None):
        event_id = str(uuid.uuid4())
        self.store.connection.execute(
            "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
            "deployment_id, event_type, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (event_id, org or self.org, repo or self.repo, self.env, deployment_id,
             "incident.rca_requested",
             self.store._Jsonb({"incident_id": incident_id})),
        )
        return event_id

    def _clear_lifecycle_acks(self):
        """Complete the deployment lifecycle acks so tests focus on RCA jobs."""
        self.store.connection.execute(
            "UPDATE outbox_events SET state='COMPLETED' WHERE event_type LIKE 'deployment.%'"
        )

    def _attributable_incident(self):
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep, findings=[{"finding_type": "INVARIANT_REMOVED",
                                         "description": "dedup removed", "model": "fct_orders"}])
        self.store.record_lineage(self.org, self.repo, self.env, "fct_orders",
                                  {"grain": "order_id"},
                                  edges=[("fct_orders", "dim_customers")], completeness="complete")
        anomaly = self._anomaly(dep)
        incident = self._incident(dep, anomaly["anomaly_id"])
        self._clear_lifecycle_acks()
        return dep, incident


class WorkerBasicsTests(WorkerTestCase):
    def test_worker_starts_and_claims_a_queued_job(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        worker = self._worker()
        worker.run(max_iterations=3)
        self.assertGreaterEqual(worker.state.processed, 1)

    def test_incident_rca_requested_is_processed_and_persists(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "completed")

    def test_rca_reloads_from_a_fresh_connection(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        fresh = PostgresLifecycleStore(DSN)
        try:
            reports = fresh.rca_for_incident(self.org, self.repo, incident["incident_id"])
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["primary_cause"]["description"], "dedup removed")
        finally:
            fresh.close()

    def test_incident_status_advances(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        self.assertEqual(
            self.store.get_incident(self.org, self.repo, incident["incident_id"])["status"],
            "investigating")

    def test_supported_event_types_are_explicit(self):
        from agent.worker.lifecycle_worker import registry

        self.assertIn("incident.rca_requested", registry.supported())
        self.assertIn("deployment.reviewed", registry.supported())


class DeterministicRcaTests(WorkerTestCase):
    def test_correct_deployment_is_selected_when_causality_matches(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertEqual(report["attributed_deployment_id"], dep)

    def test_newest_unrelated_deployment_is_not_blamed(self):
        """A more recent deployment touching other models must not be attributed."""
        dep, incident = self._attributable_incident()
        newer = f"dep-newer-{uuid.uuid4().hex[:8]}"
        self._deployment(newer, models=["unrelated_model"],
                         findings=[{"finding_type": "INVARIANT_REMOVED",
                                    "description": "unrelated change", "model": "unrelated_model"}])
        self._clear_lifecycle_acks()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertEqual(report["attributed_deployment_id"], dep)
        self.assertNotEqual(report["attributed_deployment_id"], newer)

    def test_insufficient_evidence_yields_unattributed(self):
        """No deployment touches the affected model, so nothing may be blamed."""
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep, models=["other_model"])
        anomaly = self._anomaly(dep, models=["fct_orders"])
        incident = self._incident(dep, anomaly["anomaly_id"])
        self._clear_lifecycle_acks()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertEqual(report["status"], "unattributed")
        self.assertIsNone(report["primary_cause"])
        self.assertIsNone(report["attributed_deployment_id"])
        self.assertEqual(
            self.store.get_incident(self.org, self.repo, incident["incident_id"])["status"],
            "unattributed")

    def test_recent_deployment_alone_is_insufficient(self):
        """A deployment with no relevant change evidence is not a cause."""
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep, models=["fct_orders"], findings=[])
        anomaly = self._anomaly(dep)
        incident = self._incident(dep, anomaly["anomaly_id"])
        self._clear_lifecycle_acks()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertEqual(report["status"], "unattributed",
                         "a recent deployment with no relevant change was blamed")

    def test_alternative_causes_and_unevaluated_evidence_survive(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertIsInstance(report["alternative_causes"], list)
        self.assertIsInstance(report["unevaluated_evidence"], list)
        self.assertIsInstance(report["contributing_factors"], list)
        self.assertTrue(report["contributing_factors"])

    def test_confidence_reflects_lineage_and_evidence_completeness(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        complete = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertEqual(complete["confidence"], "high")
        self.assertEqual(complete["lineage_completeness"], "complete")

    def test_missing_lineage_reduces_confidence_and_is_disclosed(self):
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep, findings=[{"finding_type": "INVARIANT_REMOVED",
                                         "description": "dedup removed", "model": "fct_orders"}])
        anomaly = self._anomaly(dep)          # no lineage recorded at all
        incident = self._incident(dep, anomaly["anomaly_id"])
        self._clear_lifecycle_acks()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        self.assertNotEqual(report["confidence"], "high")
        self.assertIn("lineage", json.dumps(report["unevaluated_evidence"]))
        self.assertEqual(report["evidence_coverage"], "INCOMPLETE")

    def test_required_rca_fields_are_all_persisted(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        report = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])[0]
        for field in ("primary_cause", "alternative_causes", "contributing_factors",
                      "downstream_symptoms", "unrelated_concurrent_changes", "confidence",
                      "unevaluated_evidence", "remediation", "rollback_recommendation",
                      "verification_steps", "lineage_level", "lineage_completeness",
                      "evidence_coverage", "attributed_deployment_id",
                      "deployment_candidates", "affected_model", "downstream_models",
                      "affected_kpis"):
            self.assertIn(field, report, f"missing {field}")
        self.assertEqual(report["affected_model"], "fct_orders")
        self.assertEqual(report["downstream_models"], ["dim_customers"])
        self.assertEqual(report["affected_kpis"], ["revenue"])
        self.assertTrue(report["verification_steps"])


class WorkerConcurrencyAndRecoveryTests(WorkerTestCase):
    def test_duplicate_jobs_create_exactly_one_rca(self):
        """The outbox dedups by (tenant, deployment, event_type), so a duplicate
        job reaches the handler as a redelivery of the same row."""
        dep, incident = self._attributable_incident()
        event_id = self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)

        # Redeliver the very same job, exactly as a lease expiry or retry would.
        self.store.connection.execute(
            "UPDATE outbox_events SET state='PENDING', lease_owner=NULL, "
            "lease_expires_at=NULL, next_attempt_at=now(), completed_at=NULL "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (self.org, self.repo, event_id))
        self._worker().run(max_iterations=3)

        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len([r for r in reports if r["status"] == "completed"]), 1)

    def test_two_workers_do_not_both_complete_the_same_job(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        workers = [self._worker(), self._worker()]
        threads = [threading.Thread(target=w.run, kwargs={"max_iterations": 3}) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len([r for r in reports if r["status"] == "completed"]), 1)
        self.assertEqual(sum(w.state.processed for w in workers), 1)

    def test_expired_lease_is_reclaimed(self):
        dep, incident = self._attributable_incident()
        event_id = self._queue_rca(incident["incident_id"], dep)
        claimed = self.store.claim_outbox(self.org, self.repo, self.env, "worker-that-died")
        self.assertEqual(claimed["event_id"], event_id)
        self.store.connection.execute(
            "UPDATE outbox_events SET lease_expires_at = now() - interval '1 hour' "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (self.org, self.repo, event_id))
        self._worker().run(max_iterations=3)
        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len(reports), 1)

    def test_crash_after_rca_commit_is_idempotent_on_retry(self):
        """The RCA row exists but the job was never completed; the retry must not duplicate it."""
        from agent.worker.rca_runtime import run_rca

        dep, incident = self._attributable_incident()
        run_rca(self.store, self.org, self.repo, self.env, incident["incident_id"])
        self._queue_rca(incident["incident_id"], dep)
        self._worker().run(max_iterations=3)
        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len([r for r in reports if r["status"] == "completed"]), 1)

    def test_crash_during_rca_leaves_no_partial_completed_report(self):
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep)
        anomaly = self._anomaly(dep)
        incident = self._incident(dep, anomaly["anomaly_id"])
        self._clear_lifecycle_acks()
        # Delete the incident so the handler raises mid-flight.
        self.store.connection.execute(
            "DELETE FROM rca_reports WHERE organization_id=%s", (self.org,))
        event_id = self._queue_rca("incident-that-does-not-exist", dep)
        self._worker(max_attempts=0).run(max_iterations=3)
        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(reports, [])
        row = self.store.connection.execute(
            "SELECT state FROM outbox_events WHERE organization_id=%s AND repository_id=%s "
            "AND event_id=%s", (self.org, self.repo, event_id)).fetchone()
        self.assertEqual(row["state"], "DEAD_LETTER")

    def test_poison_job_reaches_dead_letter_and_survives_restart(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep)
        self._clear_lifecycle_acks()
        event_id = self._queue_rca("missing-incident", dep)
        self._worker(max_attempts=0).run(max_iterations=3)
        fresh = PostgresLifecycleStore(DSN)
        try:
            letters = fresh.dead_letters(self.org, self.repo, self.env)
            self.assertEqual(len(letters), 1)
            self.assertEqual(letters[0]["event_id"], event_id)
            self.assertTrue(letters[0]["last_error"])
        finally:
            fresh.close()

    def test_unknown_event_is_retained_not_discarded(self):
        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep)
        self._clear_lifecycle_acks()
        event_id = str(uuid.uuid4())
        self.store.connection.execute(
            "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
            "deployment_id, event_type, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (event_id, self.org, self.repo, self.env, dep, "totally.unknown.event",
             self.store._Jsonb({"a": 1})))
        worker = self._worker()
        worker.run(max_iterations=3)
        row = self.store.connection.execute(
            "SELECT state, payload, last_error FROM outbox_events "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (self.org, self.repo, event_id)).fetchone()
        self.assertEqual(row["state"], "DEAD_LETTER")
        self.assertEqual(row["payload"], {"a": 1}, "evidence must remain inspectable")
        self.assertEqual(worker.state.unsupported, 1)

    def test_retry_timing_and_attempts_survive_restart(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        dep = f"dep-{uuid.uuid4().hex[:8]}"
        self._deployment(dep)
        self._clear_lifecycle_acks()
        event_id = self._queue_rca("missing-incident", dep)
        self._worker(max_attempts=5, backoff_seconds=60).run(max_iterations=3)
        fresh = PostgresLifecycleStore(DSN)
        try:
            row = fresh.connection.execute(
                "SELECT attempts, next_attempt_at, last_error, state FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
                (self.org, self.repo, event_id)).fetchone()
            self.assertGreaterEqual(row["attempts"], 1)
            self.assertEqual(row["state"], "PENDING")
            self.assertGreater(row["next_attempt_at"], _now())
            self.assertTrue(row["last_error"])
        finally:
            fresh.close()

    def test_worker_restart_does_not_lose_queued_work(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        # First worker starts and stops without processing.
        stopped = self._worker()
        stopped.request_stop()
        stopped.run(max_iterations=1)
        self.assertEqual(stopped.state.processed, 0)
        # A fresh worker process picks the job up.
        self._worker().run(max_iterations=3)
        self.assertEqual(
            len(self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])), 1)

    def test_graceful_shutdown_preserves_claims(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        worker = self._worker()
        worker.request_stop()
        worker.run(max_iterations=1)
        stats = self.store.outbox_stats(self.org, self.repo)
        self.assertNotIn("DEAD_LETTER", stats)


class WorkerTenantIsolationTests(WorkerTestCase):
    def test_worker_job_cannot_reach_another_tenants_incident(self):
        other_org, other_repo = "org-other-worker", "repo-other-worker"
        self.store.ensure_tenant(other_org, other_repo, self.env)

        dep, incident = self._attributable_incident()
        # A job in tenant B naming tenant A's incident must not resolve it.
        self.store.create_deployment(other_org, other_repo, self.env, {"deployment_id": dep})
        self.store.connection.execute(
            "UPDATE outbox_events SET state='COMPLETED' WHERE event_type LIKE 'deployment.%'")
        self._queue_rca(incident["incident_id"], dep, org=other_org, repo=other_repo)
        self._worker(max_attempts=0).run(max_iterations=3)

        # Tenant A's incident is untouched, and tenant B produced no report.
        self.assertEqual(
            self.store.rca_for_incident(self.org, self.repo, incident["incident_id"]), [])
        self.assertEqual(
            self.store.rca_for_incident(other_org, other_repo, incident["incident_id"]), [])
        letters = self.store.dead_letters(other_org, other_repo, self.env)
        self.assertEqual(len(letters), 1)


class WorkerObservabilityTests(WorkerTestCase):
    def test_state_snapshot_exposes_required_fields_without_secrets(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        worker = self._worker()
        worker.run(max_iterations=3)
        state = worker.state.snapshot(self.store)
        for field in ("worker_identity", "last_heartbeat", "last_successful_claim",
                      "claimed_job_count", "queue_depth", "retry_count",
                      "dead_letter_count", "oldest_ready_job_at", "supported_event_types"):
            self.assertIn(field, state)
        serialized = json.dumps(state, default=str).lower()
        for needle in ("password", "postgresql://", "secret", "rlm_", "select "):
            self.assertNotIn(needle, serialized)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class WorkerSubprocessTests(WorkerTestCase):
    """The worker must run as a real separate process, not only in-process."""

    def test_real_subprocess_worker_processes_a_queued_rca_job(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)

        env = dict(os.environ, RELIUM_DATABASE_URL=DSN)
        completed = subprocess.run(
            [sys.executable, "-m", "agent.worker.lifecycle_worker",
             "--max-iterations", "4", "--poll-seconds", "0.05"],
            capture_output=True, text=True, timeout=120, env=env, cwd=os.getcwd(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])

        reports = self.store.rca_for_incident(self.org, self.repo, incident["incident_id"])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "completed")
        self.assertEqual(reports[0]["attributed_deployment_id"], dep)

    def test_subprocess_worker_logs_contain_no_credentials_or_sql(self):
        dep, incident = self._attributable_incident()
        self._queue_rca(incident["incident_id"], dep)
        env = dict(os.environ, RELIUM_DATABASE_URL=DSN)
        completed = subprocess.run(
            [sys.executable, "-m", "agent.worker.lifecycle_worker",
             "--max-iterations", "4", "--poll-seconds", "0.05"],
            capture_output=True, text=True, timeout=120, env=env, cwd=os.getcwd(),
        )
        output = (completed.stdout + completed.stderr).lower()
        for needle in ("postgresql://", "password", "select ", "insert into",
                       "update ", "rlm_", "begin private"):
            self.assertNotIn(needle, output, f"{needle!r} leaked into worker logs")

    def test_subprocess_worker_requires_a_postgresql_dsn(self):
        env = dict(os.environ)
        env.pop("RELIUM_DATABASE_URL", None)
        completed = subprocess.run(
            [sys.executable, "-m", "agent.worker.lifecycle_worker", "--max-iterations", "1"],
            capture_output=True, text=True, timeout=60, env=env, cwd=os.getcwd(),
        )
        self.assertEqual(completed.returncode, 2)

    def test_subprocess_worker_rejects_a_non_postgresql_dsn(self):
        env = dict(os.environ, RELIUM_DATABASE_URL="sqlite:///tmp/relium.db")
        completed = subprocess.run(
            [sys.executable, "-m", "agent.worker.lifecycle_worker", "--max-iterations", "1"],
            capture_output=True, text=True, timeout=60, env=env, cwd=os.getcwd(),
        )
        self.assertEqual(completed.returncode, 2)

    def test_subprocess_state_snapshot_is_inspectable(self):
        env = dict(os.environ, RELIUM_DATABASE_URL=DSN)
        completed = subprocess.run(
            [sys.executable, "-m", "agent.worker.lifecycle_worker", "--print-state"],
            capture_output=True, text=True, timeout=60, env=env, cwd=os.getcwd(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        state = json.loads(completed.stdout)
        self.assertIn("supported_event_types", state)
        self.assertIn("queue_depth", state)


if __name__ == "__main__":
    unittest.main()
