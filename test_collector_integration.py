"""The customer-side collector, end to end, against real systems.

    targeted collection request (Relium)
      -> collector reads it over the real public API
      -> collector queries a REAL PostgreSQL warehouse
      -> collector builds the snapshot from what it measured
      -> submits it over the real public API
      -> Relium's real worker recomputes the review
      -> final decision

Nothing here is hand-fed. The snapshot is not written by the test: it is
whatever the collector measured in the warehouse. That is the point - this is
the step the E2E harness used to perform by hand, and it is now performed by
product code a customer would actually run.

Two databases are used, exactly as in a real deployment:
  * RELIUM_TEST_POSTGRES_DSN   - Relium's own evidence store
  * RELIUM_TEST_WAREHOUSE_DSN  - the customer's warehouse

The decision is driven by real measured data. To prove the collector is
measuring rather than echoing, the same review is recomputed against two
warehouses whose only difference is the null rate of one column, and the
decision changes accordingly.
"""
from __future__ import annotations

import json
import os
import unittest
import uuid

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
WAREHOUSE_DSN = os.environ.get("RELIUM_TEST_WAREHOUSE_DSN")

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


class _StubQueue:
    """The GitHub webhook queue is irrelevant here; the review is begun directly."""

    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def _testclient_transport(client):
    """Drive the real ASGI application over a real HTTP round trip.

    Routing, authentication, validation and persistence are all the shipped
    code paths; only the socket is elided.
    """
    def send(method, url, body, headers):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        response = client.request(method, "/" + path, json=body, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    return send


def _model(name, deps=(), cols=(), schema="analytics"):
    return {"resource_type": "model", "name": name, "schema": schema, "alias": name,
            "database": "warehouse", "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols}}


@unittest.skipUnless(DSN and WAREHOUSE_DSN,
                     "RELIUM_TEST_POSTGRES_DSN and RELIUM_TEST_WAREHOUSE_DSN "
                     "must both name real PostgreSQL servers")
class CollectorEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        # The customer's warehouse. `raw.orders` is the external dependency the
        # review will require evidence about.
        cls.wh_schema = "raw"
        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute('DROP SCHEMA IF EXISTS raw CASCADE')
            conn.execute('CREATE SCHEMA raw')
            conn.execute("CREATE TABLE raw.orders (order_id bigint NOT NULL, "
                         "discount_amount numeric, customer_email text)")
            # A customer grants the collector SELECT and nothing else.
            readonly = os.environ.get("RELIUM_TEST_WAREHOUSE_READONLY_DSN")
            if readonly:
                role = readonly.split("://", 1)[1].split(":", 1)[0]
                conn.execute(f'GRANT USAGE ON SCHEMA raw TO "{role}"')
                conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA raw TO "{role}"')

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="collector-integration", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool)
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        import psycopg

        cls.client.__exit__(None, None, None)
        cls.pool.close()
        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS raw CASCADE")

    def setUp(self):
        from agent.api.auth import generate_token, hash_secret
        from agent.collector.config import CollectorConfig

        unique = uuid.uuid4().hex[:8]
        self.org = f"org-{unique}"
        self.repo = f"repo-{unique}"
        self.env = "production"
        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(self.org, self.repo, self.env)
            store.create_service_token(token_id, hash_secret(secret), self.org,
                                       self.repo, environment=self.env,
                                       description="collector integration")
        self.config = CollectorConfig(
            api_url="http://relium.test", api_token=presented,
            warehouse_dsn=WAREHOUSE_DSN, environment=self.env,
            collector_id=f"collector-{unique}")

    # -- fixtures ---------------------------------------------------------

    def _load_warehouse(self, *, rows=200, null_every=None):
        """Populate raw.orders. null_every=2 gives a 50% null rate."""
        import psycopg

        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute("TRUNCATE raw.orders")
            if null_every:
                conn.execute(
                    "INSERT INTO raw.orders (order_id, discount_amount, customer_email) "
                    "SELECT g, CASE WHEN mod(g, %s) = 0 THEN NULL ELSE g * 1.5 END, "
                    "'user' || g || '@example.com' FROM generate_series(1, %s) g",
                    (null_every, rows))
            else:
                conn.execute(
                    "INSERT INTO raw.orders (order_id, discount_amount, customer_email) "
                    "SELECT g, g * 1.5, 'user' || g || '@example.com' "
                    "FROM generate_series(1, %s) g", (rows,))

    def _begin_review(self, pull_number=101):
        """A real review requiring production evidence for raw.orders."""
        from agent.metadata_evidence.review_lifecycle import begin_review

        sources = {"source.a.raw.orders": {
            "schema": "raw", "name": "orders", "database": "warehouse",
            "columns": {"order_id": {}, "discount_amount": {}}}}
        base = {"nodes": {"model.a.fct_orders": _model(
            "fct_orders", ["source.a.raw.orders"], ["order_id"])},
            "sources": sources}
        head = {"nodes": {"model.a.fct_orders": _model(
            "fct_orders", ["source.a.raw.orders"],
            ["order_id", "discount_amount"])}, "sources": sources}
        with self.pool.acquire() as store:
            return begin_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, pull_number=pull_number,
                base_sha=BASE_SHA, head_sha=HEAD_SHA,
                base_manifest=base, head_manifest=head,
                changed_models=["fct_orders"], enforcement_mode="enforce",
                delivery_id=f"delivery-{uuid.uuid4().hex[:8]}")

    def _collector(self):
        from agent.collector.client import ReliumClient

        return ReliumClient(self.config,
                            transport=_testclient_transport(self.client))

    def _run_collector(self, **kwargs):
        from agent.collector.runner import run_collection

        return run_collection(self.config, client=self._collector(), **kwargs)

    def _run_worker(self, review_id):
        from agent.worker.lifecycle_worker import JobContext, registry

        with self.pool.acquire() as store:
            job = store.claim_outbox(self.org, self.repo, self.env, "worker-test")
            self.assertIsNotNone(job, "expected a claimable recomputation job")
            result = registry.dispatch(job["event_type"], JobContext(store, job))
            store.complete_outbox(self.org, self.repo, job["event_id"])
            return job, result

    def _review(self, review_id):
        with self.pool.acquire() as store:
            return store.get_review(self.org, self.repo, review_id)

    # -- the whole flow ---------------------------------------------------

    def test_collector_closes_the_loop_end_to_end(self):
        """request -> collector -> warehouse -> API -> worker -> decision."""
        self._load_warehouse(rows=200)          # zero nulls: healthy evidence
        outcome = self._begin_review()

        # Relium is waiting, undecided, with a targeted request outstanding.
        review = self._review(outcome.review_id)
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertIsNone(review["decision"])
        self.assertEqual(review["evidence_coverage"], "INCOMPLETE")

        # The collector does the rest with no help from the test.
        result = self._run_collector()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.review_id, outcome.review_id)
        self.assertGreaterEqual(result.relations_collected, 1)
        self.assertIn("null_rate", result.signals_collected)

        # Relium's real worker consumes what the collector submitted.
        job, worker_result = self._run_worker(outcome.review_id)
        self.assertEqual(job["event_type"], "metadata.review_recompute_requested")
        self.assertEqual(worker_result["status"], "recomputed")

        decided = self._review(outcome.review_id)
        self.assertEqual(decided["evidence_coverage"], "COMPLETE")
        self.assertEqual(decided["lifecycle_state"], "DECISION_READY")
        self.assertIsNotNone(decided["decision"])
        self.assertEqual(decided["decision"], "ALLOW",
                         "healthy measured evidence must decide ALLOW")

    def test_decision_follows_what_the_collector_actually_measured(self):
        """The same code, the same review shape, a different warehouse.

        If the collector were echoing the request rather than measuring the
        warehouse, both runs would decide identically.
        """
        self._load_warehouse(rows=200, null_every=2)   # 50% nulls
        outcome = self._begin_review(pull_number=102)

        result = self._run_collector()
        self.assertTrue(result.ok, result.reason)
        self._run_worker(outcome.review_id)

        decided = self._review(outcome.review_id)
        self.assertEqual(decided["decision"], "WARN",
                         "a 50% production null rate must not decide ALLOW")
        self.assertEqual(decided["evidence_coverage"], "COMPLETE")

        with self.pool.acquire() as store:
            attempts = store.review_attempts(self.org, self.repo, outcome.review_id)
        findings = (attempts[-1].get("payload") or {}).get("findings", [])
        self.assertIn("column.high_null_rate", {f.get("code") for f in findings})

    def test_submitted_snapshot_contains_no_customer_data(self):
        """raw.orders carries email addresses. None may cross the boundary."""
        self._load_warehouse(rows=50)
        outcome = self._begin_review(pull_number=103)
        self.assertTrue(self._run_collector().ok)

        with self.pool.acquire() as store:
            snapshot = store.latest_accepted_snapshot(
                self.org, self.repo, outcome.review_id)
        import json

        blob = json.dumps(snapshot, default=str)
        self.assertNotIn("@example.com", blob, "customer values escaped")
        self.assertNotIn("customer_email", blob,
                         "an unrequested column was collected")
        self.assertNotIn(WAREHOUSE_DSN, blob)

    def test_collector_preserves_review_identity_through_to_the_decision(self):
        self._load_warehouse(rows=100)
        outcome = self._begin_review(pull_number=104)
        self.assertTrue(self._run_collector().ok)

        with self.pool.acquire() as store:
            snapshot = store.latest_accepted_snapshot(
                self.org, self.repo, outcome.review_id)
            review = store.get_review(self.org, self.repo, outcome.review_id)
        self.assertEqual(snapshot["review_id"], outcome.review_id)
        self.assertEqual(snapshot["base_sha"], review["base_sha"])
        self.assertEqual(snapshot["head_sha"], review["head_sha"])
        self.assertEqual(snapshot["base_manifest_hash"], review["base_manifest_hash"])
        self.assertEqual(snapshot["head_manifest_hash"], review["head_manifest_hash"])

    def _measure(self, pull_number):
        """Drive the collector's own collection path and return (request, snapshot)."""
        from agent.collector.runner import collect_snapshot
        from agent.collector.warehouse import PostgresMetadataReader

        self._begin_review(pull_number=pull_number)
        client = self._collector()
        client.register()
        request = client.pending_requests(limit=1)[0]
        snapshot, _ = collect_snapshot(
            request, PostgresMetadataReader(WAREHOUSE_DSN), config=self.config)
        return client, request, snapshot

    def test_identical_payload_resubmission_is_idempotent(self):
        """The same measurement submitted twice is one snapshot, not two."""
        from agent.collector.runner import idempotency_key_for

        self._load_warehouse(rows=100)
        client, request, snapshot = self._measure(105)
        key = idempotency_key_for(request, snapshot)

        first_status, first_body = client.submit_snapshot(snapshot, key)
        second_status, second_body = client.submit_snapshot(snapshot, key)
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 200, "an exact replay must be idempotent")
        self.assertEqual(second_body["snapshot_id"], first_body["snapshot_id"])

    def test_conflicting_replay_is_still_rejected(self):
        """Same key, different evidence, must not overwrite what was accepted."""
        from agent.collector.runner import idempotency_key_for

        self._load_warehouse(rows=100)
        client, request, snapshot = self._measure(107)
        key = idempotency_key_for(request, snapshot)
        self.assertEqual(client.submit_snapshot(snapshot, key)[0], 202)

        tampered = json.loads(json.dumps(snapshot))
        tampered["relations"][0]["row_count"] = 999999
        status, _ = client.submit_snapshot(tampered, key)
        self.assertEqual(status, 409, "conflicting replay must be rejected")

    def test_re_collection_does_not_wedge_the_request(self):
        """A retry re-measures, so observed_at moves. Under the first version
        of the key that was a permanent 409 and the request could never be
        satisfied."""
        self._load_warehouse(rows=100)
        self._begin_review(pull_number=108)

        first = self._run_collector()
        self.assertEqual(first.status_code, 202)

        second = self._run_collector(request_id=first.request_id)
        self.assertNotEqual(second.status_code, 409,
                            "a re-measurement must not be a conflicting replay")
        self.assertTrue(second.ok, second.reason)

    @unittest.skipUnless(os.environ.get("RELIUM_TEST_WAREHOUSE_READONLY_DSN"),
                         "RELIUM_TEST_WAREHOUSE_READONLY_DSN not set")
    def test_collector_works_with_read_only_warehouse_credentials(self):
        """A customer grants SELECT and nothing else. The collector must not
        secretly need more than that."""
        from agent.collector.runner import collect_snapshot
        from agent.collector.warehouse import PostgresMetadataReader

        readonly_dsn = os.environ["RELIUM_TEST_WAREHOUSE_READONLY_DSN"]
        self._load_warehouse(rows=100)
        self._begin_review(pull_number=109)
        client = self._collector()
        client.register()
        request = client.pending_requests(limit=1)[0]

        snapshot, summary = collect_snapshot(
            request, PostgresMetadataReader(readonly_dsn), config=self.config)
        relation = snapshot["relations"][0]
        self.assertTrue(relation["exists_in_production"])
        self.assertEqual(relation["row_count"], 100)
        self.assertIn("null_rate", summary["signals_collected"])

    def test_absent_relation_is_reported_and_blocks_rather_than_passing(self):
        """The warehouse does not have the table the head code reads."""
        import psycopg

        with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
            conn.execute("ALTER TABLE raw.orders RENAME TO orders_hidden")
        try:
            outcome = self._begin_review(pull_number=106)
            result = self._run_collector()
            self.assertTrue(result.ok, result.reason)
            self.assertIn("raw.orders", result.relations_missing)

            self._run_worker(outcome.review_id)
            decided = self._review(outcome.review_id)
            self.assertEqual(decided["decision"], "BLOCK",
                             "a missing production relation must not pass")
        finally:
            with psycopg.connect(WAREHOUSE_DSN, autocommit=True) as conn:
                conn.execute("ALTER TABLE raw.orders_hidden RENAME TO orders")


if __name__ == "__main__":
    unittest.main()
