"""The metadata evidence export endpoint, over the real HTTP surface.

Requires RELIUM_TEST_POSTGRES_DSN; skipped without it, like the other
PostgreSQL suites.

Two questions only this level can answer: does the route serve a downloadable
artifact with the right headers, and can the wrong credential reach it. The
bundle's contents are covered in
test_production_metadata_comparison_postgres.py.
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG, REPO, ENV = "export-org", "export-repo", "production"
T = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def orders(*, row_count=1000, null_rate=0.01, exists=True):
    return {
        "relation_name": "orders",
        "relation_database": "warehouse",
        "relation_schema": "analytics",
        "model_unique_id": "model.jaffle.orders",
        "relation_type": "table",
        "exists_in_production": True,
        "collection_status": "COLLECTED",
        "schema_fingerprint": "fp-orders",
        "row_count": row_count,
        "freshness_lag_seconds": 300,
        "columns": [{
            "column_name": "customer_id",
            "exists_in_production": exists,
            "collection_status": "COLLECTED",
            "data_type": "BIGINT",
            "is_nullable": True,
            "null_rate": null_rate,
            "distinct_count": 400,
            "cardinality": 0.4,
            # Deliberately supplied so the export can be proven to drop them.
            "min_value": "alice@example.com",
            "max_value": "zoe@example.com",
        }],
    }


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class MetadataEvidenceExportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.api.session_crypto import generate_key, load_key
        from agent.api.sessions import SessionManager
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="export-secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
            session_manager=SessionManager(
                client_id="c", client_secret="s",
                encryption_key=load_key(generate_key()), identity=None),
            cors_allowed_origins=("https://app.relium.test",),
        )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def _token(self, scope, *, org=ORG, repo=REPO, env=ENV):
        from agent.api.auth import generate_token, hash_secret

        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(org, repo, env)
            store.create_service_token(token_id, hash_secret(secret), org, repo,
                                       environment=env, description="test",
                                       scope=scope)
        return {"Authorization": f"Bearer {presented}"}

    def setUp(self):
        self.read_auth = self._token("operator_read")
        self.collector_auth = self._token("collector")

    _n = iter(range(1000, 4000))

    def _review_with_two_observations(self, *, changed=True):
        """A review whose latest attempt compared two real snapshots."""
        from agent.metadata_evidence.recompute import recompute_review
        from agent.metadata_evidence.review_lifecycle import (
            validate_and_bind_snapshot,
        )

        n = next(self._n)
        environment = f"production-{n}"
        review_id = f"rev-{n}"
        with self.pool.acquire() as store:
            store.ensure_tenant(ORG, REPO, environment)
            store.upsert_pr_review(
                ORG, REPO, environment, review_id=review_id, pull_number=n,
                base_sha="a" * 40, head_sha=f"{n:040d}",
                base_manifest_hash="bh", head_manifest_hash="hh",
                enforcement_mode="enforce", policy_version="v1", policy_hash="ph",
                metadata_required=True, payload={"plan": {"targets": []}})
            store.record_review_decision(
                ORG, REPO, review_id, decision=None, evidence_coverage="UNKNOWN",
                health=100, attempt=1, trigger="initial")

            ids = []
            for index, relations in enumerate((
                [orders(row_count=1000, null_rate=0.01)],
                [orders(row_count=800 if changed else 1000,
                        null_rate=0.12 if changed else 0.01, exists=not changed)],
            )):
                snapshot_id = f"snap-{uuid.uuid4().hex[:16]}"
                store.submit_metadata_snapshot(
                    ORG, REPO, environment, snapshot_id=snapshot_id,
                    idempotency_key=f"idem-{snapshot_id}", payload_hash="ph",
                    evidence_hash="eh",
                    observed_at=T + timedelta(hours=index),
                    collected_at=T + timedelta(hours=index),
                    review_id=review_id, relations=relations,
                    collector_version="9.9.9", adapter_type="postgres",
                    provenance={"host": "warehouse.internal"})
                validate_and_bind_snapshot(
                    store, organization_id=ORG, repository_id=REPO,
                    environment=environment, review_id=review_id,
                    snapshot=store.get_snapshot(ORG, REPO, snapshot_id))
                outcome = recompute_review(
                    store, organization_id=ORG, repository_id=REPO,
                    environment=environment, review_id=review_id)
                ids.append(snapshot_id)
        return review_id, outcome["attempt"], ids

    def _url(self, review_id, attempt):
        return f"/api/reviews/{review_id}/attempts/{attempt}/metadata-evidence.json"

    # -- the happy path ----------------------------------------------------

    def test_the_export_is_served_as_a_named_attachment(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        response = self.client.get(self._url(review_id, attempt),
                                   headers=self.read_auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        self.assertEqual(
            response.headers["content-disposition"],
            f'attachment; filename="relium-metadata-evidence-{review_id}'
            f'-attempt-{attempt}.json"')

    def test_the_export_binds_the_exact_snapshot_pair(self):
        review_id, attempt, ids = self._review_with_two_observations()
        bundle = self.client.get(self._url(review_id, attempt),
                                 headers=self.read_auth).json()
        self.assertEqual(bundle["comparison"]["baseline_snapshot_id"], ids[0])
        self.assertEqual(bundle["comparison"]["current_snapshot_id"], ids[1])
        self.assertEqual(bundle["baseline_observation"]["snapshot_id"], ids[0])
        self.assertEqual(bundle["current_observation"]["snapshot_id"], ids[1])
        self.assertEqual(bundle["review_id"], review_id)
        self.assertEqual(bundle["attempt"], attempt)

    def test_the_body_is_the_artifact_and_nothing_else(self):
        """No request_id is mixed into a file a customer keeps."""
        review_id, attempt, _ids = self._review_with_two_observations()
        response = self.client.get(self._url(review_id, attempt),
                                   headers=self.read_auth)
        self.assertNotIn("request_id", response.json())
        self.assertIn("X-Request-Id", response.headers)

    def test_two_downloads_are_byte_identical(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        first = self.client.get(self._url(review_id, attempt),
                                headers=self.read_auth).content
        second = self.client.get(self._url(review_id, attempt),
                                 headers=self.read_auth).content
        self.assertEqual(first, second)

    def test_a_no_baseline_attempt_still_exports(self):
        review_id, _attempt, ids = self._review_with_two_observations()
        # Attempt 2 is the one computed against the FIRST snapshot alone.
        bundle = self.client.get(self._url(review_id, 2),
                                 headers=self.read_auth).json()
        self.assertEqual(bundle["comparison"]["status"], "no_baseline")
        self.assertIsNone(bundle["baseline_observation"])
        self.assertEqual(bundle["current_observation"]["snapshot_id"], ids[0])

    # -- disclosure --------------------------------------------------------

    def test_no_raw_values_credentials_manifests_or_paths_are_served(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        blob = self.client.get(self._url(review_id, attempt),
                               headers=self.read_auth).text
        for forbidden in ("alice@example.com", "zoe@example.com", "min_value",
                          "max_value", "evidence_hash", "idempotency_key",
                          "payload_hash", "provenance", "warehouse.internal",
                          "collector_version", "9.9.9", "adapter_type",
                          "manifest", "compiled_code", "postgresql://",
                          "Authorization", "Bearer", "password", "secret",
                          "PRIVATE KEY", "/home/", "base_sha", "head_sha"):
            self.assertNotIn(forbidden, blob, f"{forbidden} leaked")

    def test_the_bounded_metadata_that_should_be_there_is_there(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        bundle = self.client.get(self._url(review_id, attempt),
                                 headers=self.read_auth).json()
        column = bundle["baseline_observation"]["relations"][0]["columns"][0]
        self.assertEqual(column["column_name"], "customer_id")
        self.assertEqual(column["data_type"], "BIGINT")
        self.assertEqual(column["null_rate"], 0.01)
        self.assertEqual(column["cardinality"], 0.4)

    # -- authorization -----------------------------------------------------

    def test_an_unauthenticated_download_is_refused(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        response = self.client.get(self._url(review_id, attempt))
        self.assertEqual(response.status_code, 401)

    def test_a_collector_token_cannot_download_dashboard_evidence(self):
        """The capability model separates ingest from reading the dashboard,
        and this route is one more reason that separation has to hold."""
        review_id, attempt, _ids = self._review_with_two_observations()
        response = self.client.get(self._url(review_id, attempt),
                                   headers=self.collector_auth)
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn("baseline_observation", response.text)

    def test_another_tenant_cannot_download_this_evidence(self):
        review_id, attempt, _ids = self._review_with_two_observations()
        other = self._token("operator_read", org="other-org", repo="other-repo")
        response = self.client.get(self._url(review_id, attempt), headers=other)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("baseline_observation", response.text)

    # -- absent evidence ---------------------------------------------------

    def test_an_attempt_with_no_comparison_is_a_404_not_an_empty_file(self):
        review_id, _attempt, _ids = self._review_with_two_observations()
        # Attempt 1 is the waiting attempt: it recorded SQL NULL.
        response = self.client.get(self._url(review_id, 1), headers=self.read_auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], "not_found")

    def test_an_unknown_attempt_is_a_404(self):
        review_id, _attempt, _ids = self._review_with_two_observations()
        self.assertEqual(
            self.client.get(self._url(review_id, 99),
                            headers=self.read_auth).status_code, 404)

    def test_a_non_numeric_attempt_is_a_404_not_a_crash(self):
        review_id, _attempt, _ids = self._review_with_two_observations()
        self.assertEqual(
            self.client.get(self._url(review_id, "latest"),
                            headers=self.read_auth).status_code, 404)

    def test_an_unknown_review_is_a_404(self):
        self.assertEqual(
            self.client.get(self._url("rev-does-not-exist", 2),
                            headers=self.read_auth).status_code, 404)

    def test_an_error_response_is_json_not_a_downloaded_file(self):
        response = self.client.get(self._url("rev-does-not-exist", 2),
                                   headers=self.read_auth)
        self.assertNotIn("content-disposition", response.headers)
        json.loads(response.text)


if __name__ == "__main__":
    unittest.main()
