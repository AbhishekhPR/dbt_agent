"""Public lifecycle and dashboard API tests against a real PostgreSQL server.

Requires RELIUM_TEST_POSTGRES_DSN. Skipped (not failed) when unset so the suite
still runs on a machine without PostgreSQL; CI always sets it against a real
postgres service container. No test here substitutes SQLite, an in-memory store
or a mocked connection for the API's production persistence path.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


class _StubQueue:
    """Stands in for the webhook job queue; the API paths never touch it."""

    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def _now():
    return datetime.now(timezone.utc)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; public API suite requires a real server")
class PublicApiTestCase(unittest.TestCase):
    """Builds the real served application over a real PostgreSQL pool."""

    reset_schema = True

    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        if cls.reset_schema:
            _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="test-webhook-secret",
            job_queue=_StubQueue(),
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
        self.org = "org-api"
        self.repo = "repo-api"
        self.env = "prod"
        self.token = self._issue_token(self.org, self.repo, self.env)
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def _issue_token(self, org, repo, env, *, expires_at=None, revoked=False):
        from agent.api.auth import generate_token, hash_secret

        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(org, repo, env)
            store.create_service_token(
                token_id, hash_secret(secret), org, repo,
                environment=env, description="test", expires_at=expires_at,
            )
            if revoked:
                store.revoke_service_token(token_id)
        return presented

    # -- helpers ---------------------------------------------------------------

    def _post(self, path, body, *, key=None, headers=None):
        request_headers = dict(self.auth)
        if headers:
            request_headers.update(headers)
        if key is not None:
            request_headers["Idempotency-Key"] = key
        return self.client.post(path, json=body, headers=request_headers)

    def _create_deployment(self, deployment_id=None, *, key=None):
        deployment_id = deployment_id or f"dep-{uuid.uuid4().hex[:8]}"
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "created",
             "deployment": {"merge_sha": "abc123"}},
            key=key or uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 202, response.text)
        return deployment_id

    def _create_anomaly(self, deployment_id, kind="cardinality_collapse"):
        response = self._post(
            "/api/anomalies",
            {"deployment_id": deployment_id, "kind": kind, "severity": "high",
             "evidence": {"rows_before": 1000, "rows_after": 1000, "distinct_after": 1}},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["anomaly_id"]


class RouteSurfaceTests(PublicApiTestCase):
    def test_served_application_exposes_every_required_route(self):
        from agent.api.contract import MANDATORY_ROUTES, served_routes

        served = {(e["method"], e["path"]) for e in served_routes(self.app)}
        missing = MANDATORY_ROUTES - served
        self.assertEqual(missing, set(), f"missing routes: {sorted(missing)}")

    def test_existing_health_and_webhook_behaviour_is_intact(self):
        # The queue is running under the test lifespan, so health is ok.
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        # Unsigned webhook requests are still rejected by signature auth,
        # not by the new service-token path.
        response = self.client.post(
            "/github/webhook", content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64,
                     "X-GitHub-Event": "pull_request",
                     "X-GitHub-Delivery": "delivery-1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_every_dashboard_resource_has_a_real_handler(self):
        from agent.api.contract import contract_drift

        drift = contract_drift(self.app)
        self.assertEqual(drift["unserved_dashboard_resources"], [])

    def test_machine_readable_contract_matches_the_served_route_table(self):
        from agent.api.contract import build_contract, contract_drift, served_routes

        contract = build_contract(self.app)
        self.assertEqual(contract["routes"], served_routes(self.app))
        self.assertTrue(contract_drift(self.app)["drift_free"], contract_drift(self.app))

    def test_unknown_route_returns_not_found(self):
        self.assertEqual(self.client.get("/api/does-not-exist").status_code, 404)

    def test_readiness_reports_database_and_migrations(self):
        body = self.client.get("/readyz").json()
        self.assertEqual(body["checks"]["database"], "ok")
        self.assertEqual(body["checks"]["migrations"], "current")


class AuthenticationTests(PublicApiTestCase):
    def test_missing_token_is_rejected(self):
        self.assertEqual(self.client.get("/api/deployments").status_code, 401)

    def test_invalid_token_is_rejected(self):
        response = self.client.get(
            "/api/deployments", headers={"Authorization": "Bearer rlm_deadbeef.wrong"}
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_authorization_header_is_rejected(self):
        response = self.client.get("/api/deployments", headers={"Authorization": "Basic abc"})
        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_rejected(self):
        token = self._issue_token(self.org, self.repo, self.env,
                                  expires_at=_now() - timedelta(hours=1))
        response = self.client.get(
            "/api/deployments", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_revoked_token_is_rejected(self):
        token = self._issue_token(self.org, self.repo, self.env, revoked=True)
        response = self.client.get(
            "/api/deployments", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_authorized_tenant_can_access_its_resources(self):
        self.assertEqual(self.client.get("/api/deployments", headers=self.auth).status_code, 200)

    def test_token_value_never_appears_in_responses(self):
        body = self.client.get("/api/deployments", headers=self.auth).text
        self.assertNotIn(self.token, body)
        self.assertNotIn(self.token.split(".", 1)[1], body)


class TenantIsolationTests(PublicApiTestCase):
    def setUp(self):
        super().setUp()
        self.other_token = self._issue_token("org-other", "repo-other", "prod")
        self.other_auth = {"Authorization": f"Bearer {self.other_token}"}

    def test_organization_a_cannot_read_organization_b_collections(self):
        self._create_deployment("dep-isolation-1")
        response = self.client.get("/api/deployments", headers=self.other_auth)
        self.assertEqual(response.status_code, 200)
        ids = [item["deployment_id"] for item in response.json()["items"]]
        self.assertNotIn("dep-isolation-1", ids)

    def test_repository_a_cannot_read_repository_b_resource_by_id(self):
        self._create_deployment("dep-isolation-2")
        response = self.client.get("/api/deployments/dep-isolation-2", headers=self.other_auth)
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_incident_id_does_not_leak_existence(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]

        cross = self.client.get(f"/api/incidents/{incident_id}", headers=self.other_auth)
        absent = self.client.get(f"/api/incidents/{uuid.uuid4()}", headers=self.other_auth)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(absent.status_code, 404)
        self.assertEqual(cross.json()["status"], absent.json()["status"])

    def test_repository_settings_outside_token_scope_is_not_found(self):
        response = self.client.get(
            f"/api/repositories/{self.repo}/settings", headers=self.other_auth
        )
        self.assertEqual(response.status_code, 404)

    def test_environment_outside_token_scope_is_rejected(self):
        response = self.client.get(
            "/api/deployments?environment=staging", headers=self.auth
        )
        self.assertEqual(response.status_code, 404)

    def test_body_supplied_tenant_fields_are_not_trusted(self):
        # A caller naming another tenant in the body must not escape its token.
        deployment_id = f"dep-{uuid.uuid4().hex[:8]}"
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "created",
             "organization_id": "org-other", "repository_id": "repo-other"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 202)
        leaked = self.client.get(f"/api/deployments/{deployment_id}", headers=self.other_auth)
        self.assertEqual(leaked.status_code, 404)


class DeploymentApiTests(PublicApiTestCase):
    def test_valid_deployment_event_is_accepted_and_persisted(self):
        deployment_id = self._create_deployment()
        response = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reviewed")

    def test_response_includes_stable_event_and_deployment_identity(self):
        key = uuid.uuid4().hex
        deployment_id = f"dep-{uuid.uuid4().hex[:8]}"
        body = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "created"}, key=key,
        ).json()
        self.assertEqual(body["deployment_id"], deployment_id)
        self.assertEqual(body["event_id"], key)
        self.assertIn("request_id", body)

    def test_duplicate_event_is_idempotent(self):
        key = uuid.uuid4().hex
        deployment_id = f"dep-{uuid.uuid4().hex[:8]}"
        payload = {"deployment_id": deployment_id, "event_type": "created"}
        first = self._post("/api/deployments/events", payload, key=key)
        second = self._post("/api/deployments/events", payload, key=key)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["replayed"])
        with self.pool.acquire() as store:
            rows = store.connection.execute(
                "SELECT COUNT(*) AS total FROM deployments WHERE deployment_id=%s",
                (deployment_id,),
            ).fetchone()
        self.assertEqual(rows["total"], 1)

    def test_conflicting_idempotency_replay_is_rejected(self):
        key = uuid.uuid4().hex
        self._post("/api/deployments/events",
                   {"deployment_id": "dep-conflict", "event_type": "created"}, key=key)
        conflicting = self._post(
            "/api/deployments/events",
            {"deployment_id": "dep-conflict-other", "event_type": "created"}, key=key,
        )
        self.assertEqual(conflicting.status_code, 409)

    def test_illegal_transition_leaves_no_partial_state(self):
        # 'reviewed' may only advance to 'approved'; jumping to 'healthy' is illegal.
        deployment_id = self._create_deployment()
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "healthy"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["transition_applied"])
        self.assertIn("Invalid deployment transition", response.json()["deferred_reason"])
        detail = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth).json()
        self.assertEqual(detail["transitions"], [])
        self.assertEqual(detail["status"], "reviewed")

    def test_replaying_the_current_status_is_an_accepted_no_op(self):
        deployment_id = self._create_deployment()
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "reviewed"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["transition_applied"])
        detail = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth).json()
        self.assertEqual(detail["transitions"], [])

    def test_out_of_order_event_is_deferred_without_partial_state(self):
        # 'deployment_succeeded' arriving before 'approved'/'deployment_started'.
        deployment_id = self._create_deployment()
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "deployment_succeeded"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["transition_applied"])
        detail = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth).json()
        self.assertEqual(detail["status"], "reviewed")
        self.assertEqual(detail["transitions"], [])

    def test_legal_transition_is_recorded(self):
        deployment_id = self._create_deployment()
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": deployment_id, "event_type": "approved"},
            key=uuid.uuid4().hex,
        )
        self.assertTrue(response.json()["transition_applied"])
        detail = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth).json()
        self.assertEqual(len(detail["transitions"]), 1)
        self.assertEqual(detail["transitions"][0]["to_status"], "approved")

    def test_state_and_outbox_are_atomic(self):
        deployment_id = self._create_deployment()
        with self.pool.acquire() as store:
            outbox = store.connection.execute(
                "SELECT COUNT(*) AS total FROM outbox_events WHERE deployment_id=%s",
                (deployment_id,),
            ).fetchone()
        self.assertGreaterEqual(outbox["total"], 1)

    def test_accepted_event_survives_restart(self):
        deployment_id = self._create_deployment()
        # A fresh store instance stands in for a restarted process.
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        resumed = PostgresLifecycleStore(DSN)
        try:
            record = resumed.get_deployment(self.org, self.repo, deployment_id)
            self.assertIsNotNone(record)
        finally:
            resumed.close()

    def test_unknown_event_type_is_rejected(self):
        response = self._post(
            "/api/deployments/events",
            {"deployment_id": "dep-x", "event_type": "teleported"}, key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 422)

    def test_missing_idempotency_key_is_rejected(self):
        response = self.client.post(
            "/api/deployments/events",
            json={"deployment_id": "dep-y", "event_type": "created"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)


class MonitoringApiTests(PublicApiTestCase):
    def test_baseline_submission_persists(self):
        response = self._post(
            "/api/monitoring/baselines",
            {"model": "fct_orders", "baseline": {"row_count": 1000},
             "observed_at": _now().isoformat(), "evidence_coverage": "COMPLETE",
             "source": "dbt"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_observation_submission_persists_and_is_readable(self):
        deployment_id = self._create_deployment()
        response = self._post(
            "/api/monitoring/observations",
            {"deployment_id": deployment_id, "model": "fct_orders", "metric": "row_count",
             "value": {"value": 990}, "observed_at": _now().isoformat(),
             "evidence_coverage": "COMPLETE"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 201, response.text)
        listing = self.client.get(
            f"/api/monitoring/observations?deployment_id={deployment_id}", headers=self.auth
        ).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["metric"], "row_count")

    def test_duplicate_observation_is_idempotent(self):
        key = uuid.uuid4().hex
        payload = {"model": "m", "metric": "row_count", "value": {"value": 1},
                   "observed_at": _now().isoformat()}
        first = self._post("/api/monitoring/observations", payload, key=key)
        second = self._post("/api/monitoring/observations", payload, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["observation_id"], second.json()["observation_id"])

    def test_late_observation_is_flagged_not_reordered(self):
        response = self._post(
            "/api/monitoring/observations",
            {"model": "m", "metric": "freshness", "value": {"lag_minutes": 90},
             "observed_at": (_now() - timedelta(hours=2)).isoformat()},
            key=uuid.uuid4().hex,
        )
        self.assertTrue(response.json()["late"])

    def test_missing_evidence_affects_coverage_not_health(self):
        self._post(
            "/api/monitoring/observations",
            {"model": "m", "metric": "row_count", "value": {"value": 5},
             "observed_at": _now().isoformat(), "evidence_coverage": "INCOMPLETE"},
            key=uuid.uuid4().hex,
        )
        coverage = self.client.get("/api/evidence-coverage", headers=self.auth).json()
        status = self.client.get("/api/monitoring", headers=self.auth).json()
        self.assertEqual(coverage["coverage"], "INCOMPLETE")
        self.assertNotEqual(status["health"], "DEGRADED")

    def test_cardinality_collapse_evidence_survives_round_trip(self):
        deployment_id = self._create_deployment()
        evidence = {"rows_before": 1000, "rows_after": 1000, "distinct_before": 1000,
                    "distinct_after": 1, "stable_row_count": True}
        self._post(
            "/api/monitoring/observations",
            {"deployment_id": deployment_id, "model": "fct_orders", "metric": "cardinality",
             "value": evidence, "observed_at": _now().isoformat()},
            key=uuid.uuid4().hex,
        )
        listing = self.client.get(
            f"/api/monitoring/observations?deployment_id={deployment_id}", headers=self.auth
        ).json()
        self.assertEqual(listing["items"][0]["value"], evidence)

    def test_invalid_timestamp_is_rejected(self):
        response = self._post(
            "/api/monitoring/observations",
            {"model": "m", "metric": "row_count", "value": {"v": 1},
             "observed_at": "not-a-timestamp"},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 422)

    def test_unsupported_metric_is_rejected(self):
        response = self._post(
            "/api/monitoring/observations",
            {"model": "m", "metric": "vibes", "value": {"v": 1},
             "observed_at": _now().isoformat()},
            key=uuid.uuid4().hex,
        )
        self.assertEqual(response.status_code, 422)


class AnomalyIncidentRcaTests(PublicApiTestCase):
    def test_anomaly_creation_is_idempotent(self):
        deployment_id = self._create_deployment()
        key = uuid.uuid4().hex
        payload = {"deployment_id": deployment_id, "kind": "cardinality_collapse",
                   "severity": "high", "evidence": {"distinct_after": 1}}
        first = self._post("/api/anomalies", payload, key=key)
        second = self._post("/api/anomalies", payload, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["anomaly_id"], second.json()["anomaly_id"])

    def test_incident_creation_is_idempotent_and_rca_is_queued(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        key = uuid.uuid4().hex
        first = self._post("/api/incidents", {"anomaly_id": anomaly_id}, key=key)
        second = self._post("/api/incidents", {"anomaly_id": anomaly_id}, key=key)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["incident_id"], second.json()["incident_id"])
        self.assertEqual(first.json()["rca_state"], "queued")

    def test_rca_job_is_durably_persisted_for_the_worker(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        self._post("/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex)
        with self.pool.acquire() as store:
            row = store.connection.execute(
                "SELECT COUNT(*) AS total FROM outbox_events WHERE event_type='incident.rca_requested'"
            ).fetchone()
        self.assertGreaterEqual(row["total"], 1)

    def test_pending_rca_reports_state_rather_than_inventing_a_cause(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]
        rca = self.client.get(f"/api/incidents/{incident_id}/rca", headers=self.auth).json()
        self.assertEqual(rca["state"], "pending")

    def test_exactly_one_completed_rca_survives_duplicate_completion(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]
        with self.pool.acquire() as store:
            store.create_rca(incident_id, self.org, self.repo, self.env,
                             status="completed", primary_cause={"model": "a"}, confidence="high")
            store.create_rca(incident_id, self.org, self.repo, self.env,
                             status="completed", primary_cause={"model": "b"}, confidence="low")
        rca = self.client.get(f"/api/incidents/{incident_id}/rca", headers=self.auth).json()
        self.assertEqual(rca["primary_cause"]["model"], "a")

    def test_rca_response_includes_required_evidence_fields(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]
        with self.pool.acquire() as store:
            store.create_rca(
                incident_id, self.org, self.repo, self.env, status="completed",
                primary_cause={"model": "fct_orders", "change": "dedup removed"},
                alternative_causes=[{"model": "stg_orders"}],
                contributing_factors=[{"factor": "late upstream"}],
                downstream_symptoms=[{"model": "dim_customers"}],
                unrelated_concurrent_changes=[{"pr": 42}],
                confidence="high", unevaluated_evidence=[{"evidence": "warehouse row counts"}],
            )
            store.connection.execute(
                "UPDATE rca_reports SET attributed_deployment_id=%s, affected_model=%s, "
                "downstream_models=%s, affected_kpis=%s, remediation=%s, "
                "rollback_recommendation=%s, verification_steps=%s, lineage_level=%s, "
                "lineage_completeness=%s, evidence_coverage=%s WHERE incident_id=%s",
                (deployment_id, "fct_orders", store._Jsonb(["dim_customers"]),
                 store._Jsonb(["revenue"]), store._Jsonb(["restore dedup"]), "rollback",
                 store._Jsonb(["re-run dbt test"]), "model", "PARTIAL", "COMPLETE",
                 incident_id),
            )
        detail = self.client.get(f"/api/incidents/{incident_id}", headers=self.auth).json()
        rca = detail["rca"]
        for field in ("attributed_deployment_id", "affected_model", "downstream_models",
                      "affected_kpis", "primary_cause", "alternative_causes",
                      "contributing_factors", "downstream_symptoms",
                      "unrelated_concurrent_changes", "confidence", "unevaluated_evidence",
                      "remediation", "rollback_recommendation", "verification_steps",
                      "lineage_level", "lineage_completeness", "evidence_coverage"):
            self.assertIn(field, rca, f"missing {field}")
        self.assertEqual(rca["confidence"], "high")
        self.assertEqual(rca["alternative_causes"][0]["model"], "stg_orders")
        self.assertEqual(rca["unevaluated_evidence"][0]["evidence"], "warehouse row counts")

    def test_insufficient_evidence_is_reported_as_unattributed(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]
        with self.pool.acquire() as store:
            store.create_rca(incident_id, self.org, self.repo, self.env,
                             status="unattributed", primary_cause=None, confidence="low")
        rca = self.client.get(f"/api/incidents/{incident_id}/rca", headers=self.auth).json()
        self.assertEqual(rca["state"], "unattributed")
        self.assertIsNone(rca["primary_cause"])

    def test_unknown_anomaly_is_not_found(self):
        response = self._post(
            "/api/incidents", {"anomaly_id": str(uuid.uuid4())}, key=uuid.uuid4().hex
        )
        self.assertEqual(response.status_code, 404)


class DashboardApiTests(PublicApiTestCase):
    def test_reviews_collection_and_detail(self):
        review_id = uuid.uuid4().hex
        created = self._post(
            "/api/reviews",
            {"decision": "ALLOW", "pull_number": 7, "commit_sha": "deadbeef",
             "enforcement_mode": "shadow", "risk_score": 10,
             "evidence_coverage": "COMPLETE"},
            key=review_id,
        )
        self.assertEqual(created.status_code, 201, created.text)
        listing = self.client.get("/api/reviews", headers=self.auth).json()
        self.assertGreaterEqual(listing["total"], 1)
        detail = self.client.get(f"/api/reviews/{review_id}", headers=self.auth).json()
        self.assertEqual(detail["decision"], "ALLOW")

    def test_deployments_collection_and_detail(self):
        deployment_id = self._create_deployment()
        listing = self.client.get("/api/deployments", headers=self.auth).json()
        self.assertGreaterEqual(listing["total"], 1)
        detail = self.client.get(f"/api/deployments/{deployment_id}", headers=self.auth)
        self.assertEqual(detail.status_code, 200)

    def test_monitoring_status_path(self):
        response = self.client.get("/api/monitoring", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("health", response.json())

    def test_anomalies_collection(self):
        deployment_id = self._create_deployment()
        self._create_anomaly(deployment_id)
        listing = self.client.get("/api/anomalies", headers=self.auth).json()
        self.assertGreaterEqual(listing["total"], 1)

    def test_incident_and_rca_detail_paths(self):
        deployment_id = self._create_deployment()
        anomaly_id = self._create_anomaly(deployment_id)
        incident_id = self._post(
            "/api/incidents", {"anomaly_id": anomaly_id}, key=uuid.uuid4().hex
        ).json()["incident_id"]
        self.assertEqual(
            self.client.get(f"/api/incidents/{incident_id}", headers=self.auth).status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/incidents/{incident_id}/rca", headers=self.auth).status_code, 200
        )

    def test_lineage_path(self):
        with self.pool.acquire() as store:
            store.record_lineage(self.org, self.repo, self.env, "fct_orders",
                                 {"grain": "order_id"},
                                 edges=[("stg_orders", "fct_orders")], completeness="PARTIAL")
        response = self.client.get("/api/models/fct_orders/lineage", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshots"][0]["edges"][0]["upstream_model"], "stg_orders")

    def test_kpi_impact_path(self):
        deployment_id = self._create_deployment()
        with self.pool.acquire() as store:
            store.record_kpi_impact(self.org, self.repo, self.env,
                                    deployment_id=deployment_id, kpi_name="revenue",
                                    impact={"delta_pct": -4.2})
        response = self.client.get("/api/kpis/revenue/impact", headers=self.auth).json()
        self.assertEqual(response["total"], 1)

    def test_repository_settings_path(self):
        response = self.client.get(
            f"/api/repositories/{self.repo}/settings", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["repository_id"], self.repo)

    def test_evidence_coverage_path(self):
        response = self.client.get("/api/evidence-coverage", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("coverage", response.json())

    def test_delivery_status_path_shows_each_channel_independently(self):
        with self.pool.acquire() as store:
            gh = store.record_delivery(self.org, self.repo, self.env, channel="github",
                                       event_key="pr-1", payload={"body": "x"})
            store.mark_delivered(self.org, self.repo, gh["journal_id"], remote_id="gh-1")
            store.record_delivery(self.org, self.repo, self.env, channel="dashboard",
                                  event_key="dash-1", payload={})
        body = self.client.get("/api/delivery-status", headers=self.auth).json()
        self.assertEqual(body["channels"]["github"][0]["status"], "PUBLISHED")
        self.assertEqual(body["channels"]["dashboard"][0]["status"], "PENDING")

    def test_pagination_is_deterministic_and_bounded(self):
        for index in range(5):
            self._create_deployment(f"dep-page-{index}")
        first = self.client.get("/api/deployments?limit=2&offset=0", headers=self.auth).json()
        again = self.client.get("/api/deployments?limit=2&offset=0", headers=self.auth).json()
        second = self.client.get("/api/deployments?limit=2&offset=2", headers=self.auth).json()
        self.assertEqual(first["items"], again["items"])
        self.assertEqual(len(first["items"]), 2)
        self.assertNotEqual(
            [i["deployment_id"] for i in first["items"]],
            [i["deployment_id"] for i in second["items"]],
        )

    def test_maximum_page_size_is_enforced(self):
        response = self.client.get("/api/deployments?limit=1000", headers=self.auth)
        self.assertEqual(response.status_code, 422)

    def test_invalid_pagination_is_rejected(self):
        self.assertEqual(
            self.client.get("/api/deployments?limit=abc", headers=self.auth).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/deployments?offset=-1", headers=self.auth).status_code, 422
        )

    def test_filters_cannot_produce_sql_injection(self):
        response = self.client.get(
            "/api/deployments?status=%27%3B+DROP+TABLE+deployments%3B+--", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        with self.pool.acquire() as store:
            still_there = store.connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='deployments'"
            ).fetchone()
        self.assertIsNotNone(still_there)

    def test_responses_contain_no_raw_sql_or_credentials(self):
        deployment_id = self._create_deployment()
        for path in ("/api/deployments", f"/api/deployments/{deployment_id}",
                     "/api/monitoring", "/api/evidence-coverage"):
            body = self.client.get(path, headers=self.auth).text.lower()
            for needle in ("select ", "insert into", "update ", "password", "postgresql://"):
                self.assertNotIn(needle, body, f"{needle!r} leaked from {path}")


class FailureBehaviourTests(PublicApiTestCase):
    def test_malformed_json_is_handled_safely(self):
        response = self.client.post(
            "/api/deployments/events", content=b"{not json",
            headers={**self.auth, "Idempotency-Key": uuid.uuid4().hex,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_object_body_is_rejected(self):
        response = self.client.post(
            "/api/deployments/events", content=b"[1,2,3]",
            headers={**self.auth, "Idempotency-Key": uuid.uuid4().hex,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)

    def test_oversized_payload_is_rejected(self):
        response = self.client.post(
            "/api/deployments/events",
            content=b'{"deployment_id":"x","event_type":"created","pad":"' + b"a" * (600 * 1024) + b'"}',
            headers={**self.auth, "Idempotency-Key": uuid.uuid4().hex,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)

    def test_request_id_is_returned_without_exposing_secrets(self):
        response = self.client.get("/api/deployments", headers=self.auth)
        self.assertIn("X-Request-Id", response.headers)
        self.assertEqual(response.json()["request_id"], response.headers["X-Request-Id"])

    def test_supplied_request_id_is_echoed(self):
        response = self.client.get(
            "/api/deployments", headers={**self.auth, "X-Request-Id": "corr-123"}
        )
        self.assertEqual(response.headers["X-Request-Id"], "corr-123")

    def test_concurrent_duplicate_requests_produce_one_effective_result(self):
        key = uuid.uuid4().hex
        deployment_id = f"dep-{uuid.uuid4().hex[:8]}"
        payload = {"deployment_id": deployment_id, "event_type": "created"}
        results = []

        def submit():
            results.append(self._post("/api/deployments/events", payload, key=key).status_code)

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results).count(202), 1, f"statuses={sorted(results)}")
        with self.pool.acquire() as store:
            total = store.connection.execute(
                "SELECT COUNT(*) AS total FROM deployments WHERE deployment_id=%s",
                (deployment_id,),
            ).fetchone()["total"]
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
