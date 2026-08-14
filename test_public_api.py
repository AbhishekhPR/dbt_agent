"""Public lifecycle and dashboard API tests against a real PostgreSQL server.

Requires RELIUM_TEST_POSTGRES_DSN. Skipped (not failed) when unset so the suite
still runs on a machine without PostgreSQL; CI always sets it against a real
postgres service container. No test here substitutes SQLite, an in-memory store
or a mocked connection for the API's production persistence path.
"""
from __future__ import annotations

import itertools
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


# GitHub's real permission shape, from GET /repos/{owner}/{repo}.
WRITE_PERMISSIONS = {"admin": False, "maintain": False, "push": True,
                     "triage": True, "pull": True}
READ_PERMISSIONS = {"admin": False, "maintain": False, "push": False,
                    "triage": False, "pull": True}
NO_PERMISSIONS = {"admin": False, "maintain": False, "push": False,
                  "triage": False, "pull": False}


class _StubIdentity:
    """Stands in for GitHub's user-to-server endpoints.

    Only the network calls are stood in for. The session lifecycle, the
    permission policy and the capability checks under test are the real ones.
    """

    def __init__(self):
        self.permissions = WRITE_PERMISSIONS
        self.login = "octocat"

    def authorize_url(self, client_id, redirect_uri, state):
        return f"https://github.test/authorize?state={state}"

    def exchange_code(self, *, client_id, client_secret, code, redirect_uri, now=None):
        from agent.api.github_identity import UserCredential

        return UserCredential(access_token="gh-user-token", expires_at=None,
                              refresh_token=None, refresh_expires_at=None)

    def refresh_credential(self, **kwargs):  # pragma: no cover - not reached here
        from agent.api.github_identity import UserCredential

        return UserCredential(access_token="gh-user-token-2", expires_at=None,
                              refresh_token=None, refresh_expires_at=None)

    def fetch_viewer(self, access_token, **kwargs):
        return {"login": self.login, "user_id": 99, "name": "Octo Cat"}

    def fetch_repository_permissions(self, access_token, owner, repository, **kwargs):
        return self.permissions


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; public API suite requires a real server")
class PublicApiTestCase(unittest.TestCase):
    """Builds the real served application over a real PostgreSQL pool."""

    reset_schema = True

    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        from agent.api.session_crypto import generate_key, load_key
        from agent.api.sessions import SessionManager

        if cls.reset_schema:
            _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        # A real SessionManager over a scripted GitHub. The session and
        # capability paths under test are the served ones; only the calls that
        # would leave the machine are stood in for.
        cls.identity = _StubIdentity()
        cls.sessions = SessionManager(
            client_id="test-client", client_secret="test-secret",
            encryption_key=load_key(generate_key()), identity=cls.identity)
        cls.app = create_http_app(
            webhook_secret="test-webhook-secret",
            job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024,
            shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0,
            store_pool=cls.pool,
            session_manager=cls.sessions,
            cors_allowed_origins=("https://app.relium.test",),
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
        # Two machine credentials, because a machine no longer has one
        # undifferentiated capability. `operator_read` reads the dashboard
        # resources; `collector` submits evidence and pipeline events. Neither
        # can perform governance — that requires a signed-in human, and the
        # tests below assert it.
        self.token = self._issue_token(self.org, self.repo, self.env,
                                       scope="operator_read")
        self.auth = {"Authorization": f"Bearer {self.token}"}
        self.ingest_token = self._issue_token(self.org, self.repo, self.env,
                                              scope="collector")
        self.ingest_auth = {"Authorization": f"Bearer {self.ingest_token}"}

    def _issue_token(self, org, repo, env, *, expires_at=None, revoked=False,
                     scope="operator_read"):
        from agent.api.auth import generate_token, hash_secret

        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(org, repo, env)
            store.create_service_token(
                token_id, hash_secret(secret), org, repo,
                environment=env, description="test", expires_at=expires_at,
                scope=scope,
            )
            if revoked:
                store.revoke_service_token(token_id)
        return presented

    # -- session helpers -------------------------------------------------------

    def _sign_in(self, permissions=None, *, org=None, repo=None, env=None,
                 login="octocat"):
        """Mint a real dashboard session through the real SessionManager."""
        self.identity.permissions = permissions or WRITE_PERMISSIONS
        self.identity.login = login
        with self.pool.acquire() as store:
            url, nonce = self.sessions.begin_authorization(
                store, redirect_to=None, redirect_uri="https://api.relium.test/cb")
            state = url.split("state=")[1]
            result = self.sessions.complete_authorization(
                store, code="code", state=state, nonce=nonce,
                redirect_uri="https://api.relium.test/cb",
                organization_id=org or self.org,
                repository_id=repo or self.repo,
                environment=env or self.env)
        return result

    def _session_headers(self, session, *, origin="https://app.relium.test",
                         csrf=True):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if csrf:
            headers["X-Relium-CSRF"] = session["csrf_token"]
        return headers

    def _session_cookies(self, session):
        return {"relium_session": session["session_id"]}

    def _session_post(self, path, body, session, *, key=None, origin="https://app.relium.test",
                      csrf=True):
        headers = self._session_headers(session, origin=origin, csrf=csrf)
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.post(path, json=body, headers=headers,
                                cookies=self._session_cookies(session))

    def _session_get(self, path, session):
        return self.client.get(path, cookies=self._session_cookies(session))

    _pull_counter = itertools.count(1)

    def _next_pull(self):
        return next(self._pull_counter)

    def _review(self, pull_number=None):
        """A review carried through the real lifecycle, not an inserted row.

        The review id is a digest of (repository, pull number, head SHA), so
        every test takes its own pull number. Sharing one would make these
        tests order-dependent through the database.
        """
        from agent.metadata_evidence.review_lifecycle import begin_review

        pull_number = pull_number or (4000 + self._next_pull())

        base = {"nodes": {"model.a.fct_orders": {
            "resource_type": "model", "name": "fct_orders", "schema": "analytics",
            "alias": "fct_orders", "database": "warehouse",
            "depends_on": {"nodes": ["source.a.raw.orders"]},
            "columns": {"order_id": {"name": "order_id"}}}},
            "sources": {"source.a.raw.orders": {
                "schema": "raw", "name": "orders", "database": "warehouse",
                "columns": {"order_id": {}}}}}
        head = {"nodes": base["nodes"], "sources": {"source.a.raw.orders": {
            "schema": "raw", "name": "orders", "database": "warehouse",
            "columns": {"order_id": {}, "customer_id": {}}}}}

        with self.pool.acquire() as store:
            outcome = begin_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, pull_number=pull_number,
                base_sha="a" * 40, head_sha=f"{pull_number:040d}",
                base_manifest=base, head_manifest=head,
                changed_models=["fct_orders"], enforcement_mode="enforce")
        return outcome


    def _clear_governance(self, review_id):
        """Remove change requests and exceptions left by an earlier run.

        The review id is deterministic, so without this the second execution
        of this suite against the same database would assert `created` against
        rows the first execution made.
        """
        with self.pool.acquire() as store:
            store.connection.execute(
                "DELETE FROM review_change_requests WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s",
                (self.org, self.repo, review_id))
            store.connection.execute(
                "DELETE FROM review_exceptions WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s",
                (self.org, self.repo, review_id))
            store.connection.execute(
                "DELETE FROM outbox_events WHERE organization_id=%s "
                "AND repository_id=%s AND subject_id=%s "
                "AND event_type='review.change_request_submitted'",
                (self.org, self.repo, review_id))

    # -- request changes --------------------------------------------------


    def _post_rerun(self, review_id, body=None, session=None, token=None):
        """Re-run is a governance action, so it is performed by a person.

        ``token`` is retained so the tests can prove a machine credential is
        refused here no matter how well it authenticates.
        """
        if token is not None:
            return self.client.post(
                f"/api/reviews/{review_id}/rerun", json=body or {},
                headers={"Authorization": f"Bearer {token}",
                         "Idempotency-Key": uuid.uuid4().hex})
        return self._session_post(f"/api/reviews/{review_id}/rerun", body or {},
                                  session or self._sign_in(), key=uuid.uuid4().hex)


    def _request_changes(self, review_id, message="Please restore the refund join.",
                         session=None, token=None, actor=None):
        body = {"message": message}
        if actor:
            body["actor"] = actor
        if token is not None:
            return self.client.post(
                f"/api/reviews/{review_id}/request-changes", json=body,
                headers={"Authorization": f"Bearer {token}",
                         "Idempotency-Key": uuid.uuid4().hex})
        return self._session_post(f"/api/reviews/{review_id}/request-changes", body,
                                  session or self._sign_in(), key=uuid.uuid4().hex)


    def _approve_exception(self, review_id, reason="Finance signed off for FY27.",
                           session=None, token=None, actor=None, scope=None,
                           attempt=None):
        body = {"reason": reason}
        if actor:
            body["actor"] = actor
        if scope:
            body["scope"] = scope
        if attempt is not None:
            body["attempt"] = attempt
        if token is not None:
            return self.client.post(
                f"/api/reviews/{review_id}/exceptions", json=body,
                headers={"Authorization": f"Bearer {token}",
                         "Idempotency-Key": uuid.uuid4().hex})
        return self._session_post(f"/api/reviews/{review_id}/exceptions", body,
                                  session or self._sign_in(), key=uuid.uuid4().hex)


    # -- helpers ---------------------------------------------------------------

    def _post(self, path, body, *, key=None, headers=None):
        """Ingestion POSTs. These are machine work, so they carry the collector
        credential rather than the read credential."""
        request_headers = dict(self.ingest_auth)
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
            headers=self.ingest_auth,
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


class ReviewDetailSurfaceTests(PublicApiTestCase):
    """The read surface a dashboard needs to render a review's evidence.

    Every field asserted here was already persisted by the lifecycle store and
    unreachable over HTTP, so a dashboard could show a verdict but never the
    evidence behind it.
    """

    reset_schema = False

    FORBIDDEN = ("-----begin", "postgresql://", "password", "private key",
                 "select ", "insert into", "drop table", "ghp_", "ghs_")

    def _get(self, path):
        response = self.client.get(path, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_review_detail_carries_identity_attempt_and_health(self):
        outcome = self._review(pull_number=4242)
        body = self._get(f"/api/reviews/{outcome.review_id}")

        self.assertEqual(body["review_id"], outcome.review_id)
        self.assertEqual(body["pull_number"], 4242)
        self.assertEqual(body["base_sha"], "a" * 40)
        self.assertEqual(body["head_sha"], f"{4242:040d}")
        self.assertEqual(body["attempt"], outcome.attempt)
        self.assertEqual(body["lifecycle_state"], outcome.lifecycle_state)
        self.assertEqual(body["health"], outcome.health)
        self.assertTrue(body["base_manifest_hash"])
        self.assertTrue(body["head_manifest_hash"])
        self.assertTrue(body["metadata_required"])
        # Waiting is not a verdict, and the API must not invent one.
        self.assertIsNone(body["decision"])

    def test_review_detail_exposes_only_the_allowlisted_change_plan(self):
        outcome = self._review(pull_number=4243)
        with self.pool.acquire() as store:
            record = store.get_review(self.org, self.repo, outcome.review_id)
            payload = dict(record["payload"])
            payload["internal_note"] = "must not cross the API boundary"
            payload["raw_manifest"] = {"nodes": {"model.secret": {"raw_sql": "select *"}}}
            plan = dict(payload["plan"])
            plan["notes"] = ["internal planner note"]
            plan["required_evidence_level"] = "full"
            payload["plan"] = plan
            store.connection.execute(
                "UPDATE reviews SET payload=%s WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s",
                (store._Jsonb(payload), self.org, self.repo, outcome.review_id),
            )

        body = self._get(f"/api/reviews/{outcome.review_id}")

        self.assertEqual(set(body["change_plan"]), {
            "changed_models", "added_dependencies", "removed_dependencies",
            "downstream_models", "direct_edges", "targets",
        })
        self.assertEqual(body["change_plan"]["changed_models"], ["fct_orders"])
        self.assertIsInstance(body["change_plan"]["added_dependencies"], list)
        self.assertIsInstance(body["change_plan"]["removed_dependencies"], list)
        self.assertIsInstance(body["change_plan"]["downstream_models"], list)
        self.assertTrue(body["change_plan"]["targets"])
        for target in body["change_plan"]["targets"]:
            self.assertEqual(set(target), {
                "relation_name", "model_unique_id", "dependency_kind",
                "columns", "reason",
            })
        serialized = json.dumps(body).lower()
        self.assertNotIn("internal_note", serialized)
        self.assertNotIn("raw_manifest", serialized)
        self.assertNotIn("required_evidence_level", serialized)
        self.assertNotIn("internal planner note", serialized)

    def test_review_detail_tolerates_malformed_persisted_change_plan_shapes(self):
        empty_plan = {
            "changed_models": [],
            "added_dependencies": [],
            "removed_dependencies": [],
            "downstream_models": [],
            "direct_edges": None,
            "targets": [],
        }
        malformed_payloads = (
            42,
            {"plan": 42},
            {"plan": {"targets": 42}},
        )

        for index, malformed in enumerate(malformed_payloads, start=1):
            with self.subTest(malformed=malformed):
                outcome = self._review(pull_number=4250 + index)
                with self.pool.acquire() as store:
                    store.connection.execute(
                        "UPDATE reviews SET payload=%s WHERE organization_id=%s "
                        "AND repository_id=%s AND review_id=%s",
                        (store._Jsonb(malformed), self.org, self.repo, outcome.review_id),
                    )

                body = self._get(f"/api/reviews/{outcome.review_id}")
                self.assertEqual(body["change_plan"], empty_plan)

    def test_findings_expose_measured_value_and_threshold(self):
        outcome = self._review()
        body = self._get(f"/api/reviews/{outcome.review_id}/findings")

        self.assertEqual(body["review_id"], outcome.review_id)
        findings = body["attempts"][0]["findings"]
        self.assertTrue(findings, "a waiting review still has evidence findings")
        for finding in findings:
            for field in ("code", "severity", "category", "message", "detail"):
                self.assertIn(field, finding)

    def test_attempts_expose_the_lifecycle_transitions(self):
        outcome = self._review()
        body = self._get(f"/api/reviews/{outcome.review_id}/attempts")

        self.assertEqual(body["current_attempt"], outcome.attempt)
        self.assertTrue(body["attempts"])
        states = [t["to_state"] for t in body["transitions"]]
        self.assertIn("CODE_ANALYSIS_COMPLETE", states)
        self.assertIn("WAITING_FOR_METADATA", states)

    def test_collection_requests_show_what_was_asked_for(self):
        outcome = self._review(pull_number=4244)
        body = self._get(f"/api/reviews/{outcome.review_id}/collection-requests")

        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertEqual(item["request_id"], outcome.request_id)
        self.assertEqual(item["state"], "PENDING")
        # The request must be bound to the exact code state under review, or
        # the evidence it returns cannot be attributed to this review.
        self.assertEqual(item["head_sha"], f"{4244:040d}")
        self.assertEqual(item["base_sha"], "a" * 40)
        self.assertIn("plan", item)

    def test_snapshots_and_publications_are_empty_before_collection(self):
        outcome = self._review(pull_number=4243)

        snapshots = self._get(f"/api/reviews/{outcome.review_id}/snapshots")
        self.assertEqual(snapshots["total"], 0)

        publications = self._get(f"/api/reviews/{outcome.review_id}/publications")
        self.assertIsNone(publications["github"]["comment_id"])
        self.assertEqual(publications["github"]["pull_number"], 4243)

    def test_publication_identity_is_reported_once_recorded(self):
        outcome = self._review()
        with self.pool.acquire() as store:
            store.record_review_publication(
                self.org, self.repo, outcome.review_id,
                comment_id="991", check_run_id="7723")

        body = self._get(f"/api/reviews/{outcome.review_id}/publications")
        self.assertEqual(body["github"]["comment_id"], "991")
        self.assertEqual(body["github"]["check_run_id"], "7723")

    def test_detail_routes_are_tenant_scoped(self):
        outcome = self._review()
        other = self._issue_token("org-other", "repo-other", self.env)
        for suffix in ("", "/findings", "/attempts", "/collection-requests",
                       "/snapshots", "/publications"):
            response = self.client.get(
                f"/api/reviews/{outcome.review_id}{suffix}",
                headers={"Authorization": f"Bearer {other}"})
            self.assertEqual(response.status_code, 404,
                             f"{suffix} leaked across tenants")

    def test_detail_routes_require_authentication(self):
        outcome = self._review()
        for suffix in ("/findings", "/attempts", "/collection-requests",
                       "/snapshots", "/publications"):
            response = self.client.get(f"/api/reviews/{outcome.review_id}{suffix}")
            self.assertEqual(response.status_code, 401, suffix)

    def test_detail_routes_disclose_nothing_sensitive(self):
        outcome = self._review()
        for suffix in ("", "/findings", "/attempts", "/collection-requests",
                       "/snapshots", "/publications", "/evidence-coverage"):
            text = json.dumps(
                self._get(f"/api/reviews/{outcome.review_id}{suffix}")).lower()
            for needle in self.FORBIDDEN:
                self.assertNotIn(needle, text, f"{suffix} leaked {needle!r}")

    # -- re-run analysis --------------------------------------------------

    def _settle_requests(self, review_id):
        """Close every actionable request for this review.

        The review id is a digest of (repo, PR, head SHA), so re-running this
        suite against the same database reaches the same review — and a rerun
        left open by the previous run would make the next one answer
        `already_running`. Settling first makes each test independent of run
        history rather than of test order alone.
        """
        with self.pool.acquire() as store:
            for r in store.collection_requests_for_review(
                    self.org, self.repo, review_id):
                if r["state"] in ("PENDING", "ACKNOWLEDGED"):
                    store.close_collection_request(
                        self.org, self.repo, r["request_id"], state="COMPLETED")

    def test_rerun_creates_a_new_collection_request(self):
        """A re-run must ask for fresh evidence, not replay the old answer."""
        outcome = self._review(pull_number=4300)
        self._settle_requests(outcome.review_id)

        response = self._post_rerun(outcome.review_id)
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["rerun_id"])
        self.assertNotEqual(body["rerun_id"], outcome.request_id)

        requests = self._get(
            f"/api/reviews/{outcome.review_id}/collection-requests")
        states = {r["request_id"]: r["state"] for r in requests["items"]}
        self.assertEqual(states[body["rerun_id"]], "PENDING")
        # Exactly one request is actionable: the one this re-run created.
        actionable = [r for r in requests["items"]
                      if r["state"] in ("PENDING", "ACKNOWLEDGED")]
        self.assertEqual([r["request_id"] for r in actionable], [body["rerun_id"]])

    def test_rerun_does_not_alter_previous_attempts(self):
        outcome = self._review(pull_number=4301)
        before = self._get(f"/api/reviews/{outcome.review_id}/attempts")
        self._settle_requests(outcome.review_id)
        self._post_rerun(outcome.review_id)
        after = self._get(f"/api/reviews/{outcome.review_id}/attempts")
        self.assertEqual(before["attempts"], after["attempts"],
                         "a re-run rewrote history")

    def test_a_second_click_returns_the_run_already_in_flight(self):
        """Double-click must not queue a second collection."""
        outcome = self._review(pull_number=4302)
        first = self._post_rerun(outcome.review_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "already_running")
        # The request begin_review raised is still open, so it IS the run.
        self.assertEqual(first.json()["rerun_id"], outcome.request_id)

        second = self._post_rerun(outcome.review_id)
        self.assertEqual(second.json()["rerun_id"], first.json()["rerun_id"])
        actionable = [r for r in self._get(
            f"/api/reviews/{outcome.review_id}/collection-requests")["items"]
            if r["state"] in ("PENDING", "ACKNOWLEDGED")]
        self.assertEqual(len(actionable), 1, "a second run was queued")

    def test_rerun_against_a_changed_head_is_refused(self):
        """A moved HEAD is a different review, and must be said so."""
        outcome = self._review(pull_number=4303)
        response = self._post_rerun(outcome.review_id, {"head_sha": "f" * 40})
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertIn("HEAD has changed", detail)
        self.assertIn("gh-", detail, "the new review id should be named")

    def test_rerun_of_a_review_needing_no_metadata_is_refused(self):
        outcome = self._review(pull_number=4304)
        with self.pool.acquire() as store:
            store.connection.execute(
                "UPDATE reviews SET metadata_required=false WHERE "
                "organization_id=%s AND repository_id=%s AND review_id=%s",
                (self.org, self.repo, outcome.review_id))
        response = self._post_rerun(outcome.review_id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("no external production dependency",
                      response.json()["detail"])

    def test_rerun_is_tenant_scoped(self):
        outcome = self._review(pull_number=4305)
        intruder = self._sign_in(org="org-intruder", repo="repo-intruder",
                                 login="intruder")
        response = self._post_rerun(outcome.review_id, session=intruder)
        self.assertEqual(response.status_code, 404,
                         "another tenant could re-run this review")

    def test_rerun_requires_authentication(self):
        outcome = self._review(pull_number=4306)
        response = self.client.post(
            f"/api/reviews/{outcome.review_id}/rerun", json={},
            headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 401)

    def test_rerun_of_an_unknown_review_is_not_found(self):
        self.assertEqual(self._post_rerun("gh-does-not-exist").status_code, 404)

    def test_rerun_names_the_collection_request_without_shadowing(self):
        """The envelope reserves top-level `request_id` for the correlation id.

        A handler returning its own `request_id` has it silently overwritten,
        so the caller receives a plausible-looking value that identifies the
        wrong thing. The collection request is named explicitly instead.
        """
        outcome = self._review(pull_number=4308)
        self._settle_requests(outcome.review_id)
        body = self._post_rerun(outcome.review_id).json()

        self.assertIn("collection_request_id", body)
        self.assertEqual(body["collection_request_id"], body["rerun_id"])
        self.assertTrue(body["collection_request_id"].startswith("req-"))
        # The envelope's own key is a correlation id, and is NOT the request.
        self.assertNotEqual(body["request_id"], body["collection_request_id"])

    def test_rerun_is_recorded_in_the_audit_trail(self):
        outcome = self._review(pull_number=4307)
        self._settle_requests(outcome.review_id)
        self._post_rerun(outcome.review_id)
        with self.pool.acquire() as store:
            events = [e for e in store.audit_events(self.org, self.repo)
                      if e["event_type"] == "review.rerun_requested"
                      and e["reference_id"] == outcome.review_id]
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "dashboard")

    def test_request_changes_records_a_durable_intent(self):
        outcome = self._review(pull_number=4400)
        self._clear_governance(outcome.review_id)
        response = self._request_changes(outcome.review_id)
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["state"], "PENDING")
        self.assertTrue(body["change_request_id"])

        listed = self._get(f"/api/reviews/{outcome.review_id}/change-requests")
        self.assertEqual(listed["total"], 1)
        item = listed["items"][0]
        self.assertEqual(item["actor"], "github:octocat",
                         "the actor must be the signed-in GitHub user")
        self.assertEqual(item["attempt"], outcome.attempt)
        self.assertEqual(item["pull_number"], 4400)
        # PENDING, not published: GitHub has not been called yet, and the
        # record must not claim otherwise.
        self.assertEqual(item["state"], "PENDING")
        self.assertIsNone(item["remote_review_id"])

    def test_request_changes_is_not_duplicated_by_a_second_click(self):
        outcome = self._review(pull_number=4401)
        self._clear_governance(outcome.review_id)
        first = self._request_changes(outcome.review_id)
        second = self._request_changes(outcome.review_id)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "already_requested")
        self.assertEqual(first.json()["change_request_id"],
                         second.json()["change_request_id"])
        listed = self._get(f"/api/reviews/{outcome.review_id}/change-requests")
        self.assertEqual(listed["total"], 1, "a second GitHub review was queued")

    def test_request_changes_requires_a_message(self):
        outcome = self._review(pull_number=4402)
        response = self._request_changes(outcome.review_id, message="  ")
        self.assertEqual(response.status_code, 422)

    def test_request_changes_is_tenant_scoped(self):
        outcome = self._review(pull_number=4403)
        intruder = self._sign_in(org="org-other-cr", repo="repo-other-cr",
                                 login="intruder")
        response = self._request_changes(outcome.review_id, session=intruder)
        self.assertEqual(response.status_code, 404)

    def test_request_changes_enqueues_a_worker_job(self):
        from agent.metadata_evidence.change_request import EVENT_TYPE

        outcome = self._review(pull_number=4404)
        self._clear_governance(outcome.review_id)
        body = self._request_changes(outcome.review_id).json()
        with self.pool.acquire() as store:
            rows = store.connection.execute(
                "SELECT event_type, payload FROM outbox_events "
                "WHERE subject_id=%s AND event_type=%s",
                (outcome.review_id, EVENT_TYPE)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["change_request_id"],
                         body["change_request_id"])

    # -- approve exception ------------------------------------------------

    def test_exception_never_rewrites_the_relium_decision(self):
        """The whole point: BLOCK stays BLOCK."""
        outcome = self._review(pull_number=4500)
        self._clear_governance(outcome.review_id)
        with self.pool.acquire() as store:
            store.record_review_decision(
                self.org, self.repo, outcome.review_id, decision="BLOCK",
                evidence_coverage="COMPLETE", health=100, attempt=2,
                trigger="manual", payload={"findings": []})
        self.assertEqual(self._get(f"/api/reviews/{outcome.review_id}")["decision"],
                         "BLOCK")

        response = self._approve_exception(outcome.review_id)
        self.assertEqual(response.status_code, 201, response.text)

        after = self._get(f"/api/reviews/{outcome.review_id}")
        self.assertEqual(after["decision"], "BLOCK",
                         "an exception rewrote the Relium decision")
        listed = self._get(f"/api/reviews/{outcome.review_id}/exceptions")
        self.assertEqual(listed["decision"], "BLOCK")
        self.assertEqual(listed["active_exception"]["overridden_decision"], "BLOCK")
        self.assertEqual(listed["active_exception"]["actor"], "github:octocat")

    def test_exception_requires_a_reason(self):
        outcome = self._review(pull_number=4501)
        self.assertEqual(
            self._approve_exception(outcome.review_id, reason="  ").status_code, 422)
        blank = self._session_post(
            f"/api/reviews/{outcome.review_id}/exceptions", {}, self._sign_in(),
            key=uuid.uuid4().hex)
        self.assertEqual(blank.status_code, 422)

    def test_exception_binds_to_the_exact_attempt(self):
        outcome = self._review(pull_number=4502)
        self._clear_governance(outcome.review_id)
        self._approve_exception(outcome.review_id)
        listed = self._get(f"/api/reviews/{outcome.review_id}/exceptions")
        self.assertEqual(listed["active_exception"]["attempt"], outcome.attempt)
        self.assertEqual(listed["active_exception"]["scope"], "attempt")

    def test_a_later_attempt_does_not_inherit_an_attempt_scoped_exception(self):
        """A new attempt analysed new evidence. An old override must not carry."""
        outcome = self._review(pull_number=4503)
        self._clear_governance(outcome.review_id)
        self._approve_exception(outcome.review_id)
        with self.pool.acquire() as store:
            store.record_review_decision(
                self.org, self.repo, outcome.review_id, decision="BLOCK",
                evidence_coverage="COMPLETE", health=100,
                attempt=outcome.attempt + 1, trigger="manual",
                payload={"findings": []})
        listed = self._get(f"/api/reviews/{outcome.review_id}/exceptions")
        self.assertEqual(listed["attempt"], outcome.attempt + 1)
        self.assertIsNone(listed["active_exception"],
                          "a newer attempt inherited an older exception")
        # The historical record survives, bound to its own attempt.
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["attempt"], outcome.attempt)

    def test_a_review_scoped_exception_is_honoured_across_attempts(self):
        outcome = self._review(pull_number=4504)
        self._clear_governance(outcome.review_id)
        self._approve_exception(outcome.review_id, scope="review")
        with self.pool.acquire() as store:
            store.record_review_decision(
                self.org, self.repo, outcome.review_id, decision="BLOCK",
                evidence_coverage="COMPLETE", health=100,
                attempt=outcome.attempt + 1, trigger="manual",
                payload={"findings": []})
        listed = self._get(f"/api/reviews/{outcome.review_id}/exceptions")
        self.assertIsNotNone(listed["active_exception"])
        self.assertEqual(listed["active_exception"]["scope"], "review")

    def test_duplicate_exception_returns_the_existing_one(self):
        outcome = self._review(pull_number=4505)
        self._clear_governance(outcome.review_id)
        first = self._approve_exception(outcome.review_id)
        second = self._approve_exception(outcome.review_id)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "already_approved")
        self.assertEqual(first.json()["exception_id"], second.json()["exception_id"])
        self.assertEqual(
            self._get(f"/api/reviews/{outcome.review_id}/exceptions")["total"], 1)

    def test_exception_revocation_is_audited(self):
        outcome = self._review(pull_number=4506)
        self._clear_governance(outcome.review_id)
        session = self._sign_in()
        approved = self._approve_exception(outcome.review_id, session=session).json()
        response = self._session_post(
            f"/api/reviews/{outcome.review_id}/exceptions/"
            f"{approved['exception_id']}/revoke",
            {"reason": "Finance withdrew approval."}, session,
            key=uuid.uuid4().hex)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["state"], "revoked")
        self.assertEqual(body["revoked_by"], "github:octocat")
        self.assertEqual(body["revocation_reason"], "Finance withdrew approval.")

        listed = self._get(f"/api/reviews/{outcome.review_id}/exceptions")
        self.assertIsNone(listed["active_exception"])
        # The audit trail is append-only, so counting across runs would be
        # asserting run history. Assert THIS revocation is recorded instead.
        with self.pool.acquire() as store:
            events = [e for e in store.audit_events(self.org, self.repo)
                      if e["event_type"] == "review.exception_revoked"
                      and (e["payload"] or {}).get("exception_id")
                      == approved["exception_id"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "github:octocat",
                         "the audit row must name the authenticated human")

    def test_exceptions_are_tenant_scoped(self):
        outcome = self._review(pull_number=4507)
        self._clear_governance(outcome.review_id)
        self._approve_exception(outcome.review_id)
        intruder = self._sign_in(org="org-other-exc", repo="repo-other-exc",
                                 login="intruder")
        self.assertEqual(
            self._approve_exception(outcome.review_id, session=intruder).status_code,
            404)
        listed = self._session_get(
            f"/api/reviews/{outcome.review_id}/exceptions", intruder)
        self.assertEqual(listed.status_code, 404,
                         "an exception leaked across tenants")

    def test_exception_for_an_unrecorded_attempt_is_refused(self):
        outcome = self._review(pull_number=4508)
        response = self._approve_exception(outcome.review_id, attempt=99)
        self.assertEqual(response.status_code, 409)
        self.assertIn("not recorded", response.json()["detail"])

    def test_unknown_review_is_not_found_on_every_detail_route(self):
        for suffix in ("/findings", "/attempts", "/collection-requests",
                       "/snapshots", "/publications"):
            response = self.client.get(f"/api/reviews/does-not-exist{suffix}",
                                       headers=self.auth)
            self.assertEqual(response.status_code, 404, suffix)


class BrowserAuthorizationBoundaryTests(PublicApiTestCase):
    """The boundary between a person, a machine, and neither.

    Every test here is a refusal that must hold. They exist because the
    previous model had one credential that could do everything, shipped inside
    the dashboard's JavaScript.
    """

    # A review id is a digest of (repository, pull number, head SHA), so tests
    # that shared a pull number would share a review — and an approval in one
    # would come back as "already approved" in the next.
    _boundary_pulls = itertools.count(9100)

    def _review_id(self):
        return self._review(pull_number=next(self._boundary_pulls)).review_id

    # -- unauthenticated ---------------------------------------------------

    def test_unauthenticated_dashboard_read_is_denied(self):
        for path in ("/api/reviews", "/api/deployments", "/api/incidents"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_unauthenticated_governance_write_is_denied(self):
        review_id = self._review_id()
        for path in (f"/api/reviews/{review_id}/rerun",
                     f"/api/reviews/{review_id}/request-changes",
                     f"/api/reviews/{review_id}/exceptions"):
            response = self.client.post(
                path, json={"message": "x", "reason": "x"},
                headers={"Idempotency-Key": uuid.uuid4().hex})
            self.assertEqual(response.status_code, 401, path)

    # -- human authorization ----------------------------------------------

    def test_github_user_with_access_can_read(self):
        session = self._sign_in(READ_PERMISSIONS)
        self.assertEqual(self._session_get("/api/reviews", session).status_code, 200)

    def test_read_only_collaborator_cannot_approve_or_revoke(self):
        review_id = self._review_id()
        writer = self._sign_in(WRITE_PERMISSIONS)
        approved = self._approve_exception(review_id, session=writer)
        self.assertEqual(approved.status_code, 201, approved.text)

        reader = self._sign_in(READ_PERMISSIONS)
        self.assertEqual(
            self._approve_exception(review_id, session=reader).status_code, 403)
        revoke = self._session_post(
            f"/api/reviews/{review_id}/exceptions/"
            f"{approved.json()['exception_id']}/revoke",
            {"reason": "not mine to revoke"}, reader, key=uuid.uuid4().hex)
        self.assertEqual(revoke.status_code, 403)

    def test_read_only_collaborator_cannot_rerun_or_request_changes(self):
        review_id = self._review_id()
        reader = self._sign_in(READ_PERMISSIONS)
        self.assertEqual(
            self._post_rerun(review_id, session=reader).status_code, 403)
        self.assertEqual(
            self._request_changes(review_id, session=reader).status_code, 403)

    def test_triage_alone_is_not_write_authority(self):
        review_id = self._review_id()
        triage = self._sign_in({"admin": False, "maintain": False, "push": False,
                                "triage": True, "pull": True})
        self.assertEqual(
            self._approve_exception(review_id, session=triage).status_code, 403)

    def test_authorized_github_user_can_perform_a_governance_write(self):
        review_id = self._review_id()
        writer = self._sign_in(WRITE_PERMISSIONS)
        self.assertEqual(
            self._approve_exception(review_id, session=writer).status_code, 201)

    def test_losing_repository_access_blocks_the_next_governance_write(self):
        review_id = self._review_id()
        session = self._sign_in(WRITE_PERMISSIONS)
        self.assertEqual(
            self._approve_exception(review_id, session=session).status_code, 201)
        # GitHub now reports no access. The next write must not reuse the
        # authorization recorded at sign-in.
        self.identity.permissions = NO_PERMISSIONS
        self.assertEqual(
            self._post_rerun(review_id, session=session).status_code, 401)

    # -- machine credentials ----------------------------------------------

    def test_collector_token_cannot_perform_governance(self):
        review_id = self._review_id()
        for call in (self._post_rerun, self._request_changes, self._approve_exception):
            self.assertEqual(call(review_id, token=self.ingest_token).status_code, 403,
                             call.__name__)

    def test_operator_read_token_cannot_perform_governance(self):
        review_id = self._review_id()
        for call in (self._post_rerun, self._request_changes, self._approve_exception):
            self.assertEqual(call(review_id, token=self.token).status_code, 403,
                             call.__name__)

    def test_collector_token_cannot_browse_the_dashboard(self):
        for path in ("/api/reviews", "/api/incidents", "/api/deployments"):
            response = self.client.get(path, headers=self.ingest_auth)
            self.assertEqual(response.status_code, 403, path)

    def test_human_session_cannot_submit_collector_metadata(self):
        session = self._sign_in(WRITE_PERMISSIONS)
        response = self._session_post("/api/metadata-snapshots", {"snapshot": {}},
                                      session, key=uuid.uuid4().hex)
        self.assertEqual(response.status_code, 403)

    def test_human_session_cannot_claim_collector_work(self):
        session = self._sign_in(WRITE_PERMISSIONS)
        self.assertEqual(
            self._session_get("/api/collection-requests", session).status_code, 403)

    # -- actor -------------------------------------------------------------

    def test_body_supplied_actor_is_rejected(self):
        review_id = self._review_id()
        writer = self._sign_in(WRITE_PERMISSIONS)
        for response in (
            self._approve_exception(review_id, session=writer, actor="someone-else"),
            self._request_changes(review_id, session=writer, actor="someone-else"),
        ):
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("actor", response.text)

    def test_audit_row_records_the_authenticated_human(self):
        review_id = self._review_id()
        writer = self._sign_in(WRITE_PERMISSIONS, login="real-person")
        approved = self._approve_exception(review_id, session=writer)
        self.assertEqual(approved.status_code, 201, approved.text)
        with self.pool.acquire() as store:
            events = [e for e in store.audit_events(self.org, self.repo)
                      if e["event_type"] == "review.exception_approved"
                      and (e["payload"] or {}).get("exception_id")
                      == approved.json()["exception_id"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "github:real-person")

    # -- csrf and origin ---------------------------------------------------

    def test_mutation_without_csrf_token_is_refused(self):
        review_id = self._review_id()
        session = self._sign_in(WRITE_PERMISSIONS)
        response = self._session_post(
            f"/api/reviews/{review_id}/exceptions", {"reason": "no csrf"},
            session, key=uuid.uuid4().hex, csrf=False)
        self.assertEqual(response.status_code, 403)

    def test_mutation_with_a_foreign_csrf_token_is_refused(self):
        review_id = self._review_id()
        session = self._sign_in(WRITE_PERMISSIONS)
        other = self._sign_in(WRITE_PERMISSIONS)
        response = self.client.post(
            f"/api/reviews/{review_id}/exceptions", json={"reason": "borrowed"},
            headers={"Origin": "https://app.relium.test",
                     "X-Relium-CSRF": other["csrf_token"],
                     "Idempotency-Key": uuid.uuid4().hex},
            cookies=self._session_cookies(session))
        self.assertEqual(response.status_code, 403)

    def test_mutation_without_an_origin_is_refused(self):
        review_id = self._review_id()
        session = self._sign_in(WRITE_PERMISSIONS)
        response = self._session_post(
            f"/api/reviews/{review_id}/exceptions", {"reason": "no origin"},
            session, key=uuid.uuid4().hex, origin=None)
        self.assertEqual(response.status_code, 403)

    def test_mutation_from_a_foreign_origin_is_refused(self):
        review_id = self._review_id()
        session = self._sign_in(WRITE_PERMISSIONS)
        response = self._session_post(
            f"/api/reviews/{review_id}/exceptions", {"reason": "evil"},
            session, key=uuid.uuid4().hex, origin="https://evil.example")
        self.assertEqual(response.status_code, 403)

    # -- session lifetime --------------------------------------------------

    def test_logout_ends_the_session(self):
        session = self._sign_in(WRITE_PERMISSIONS)
        self.assertEqual(self._session_get("/api/reviews", session).status_code, 200)
        with self.pool.acquire() as store:
            self.sessions.revoke(store, session["session_id"], "logout")
        self.assertEqual(self._session_get("/api/reviews", session).status_code, 401)

    def test_an_unknown_session_cookie_is_refused(self):
        response = self.client.get("/api/reviews",
                                   cookies={"relium_session": "not-a-session"})
        self.assertEqual(response.status_code, 401)

    # -- cross tenant ------------------------------------------------------

    def test_a_session_cannot_read_another_repository(self):
        review_id = self._review_id()
        intruder = self._sign_in(WRITE_PERMISSIONS, org="org-x", repo="repo-x",
                                 login="intruder")
        self.assertEqual(
            self._session_get(f"/api/reviews/{review_id}", intruder).status_code, 404)


if __name__ == "__main__":
    unittest.main()
