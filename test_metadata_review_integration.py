"""End-to-end metadata review lifecycle over the real served application.

Drives the whole flow through the actual Starlette app and the actual worker
handler against real PostgreSQL:

    webhook-shaped review
      -> PostgreSQL review row
      -> targeted collection request
      -> WAITING_FOR_METADATA with no decision
      -> snapshot submitted over the public HTTP API
      -> durable recomputation job
      -> worker recomputation
      -> metadata-backed decision
      -> dashboard

Nothing here fakes a snapshot by writing rows directly: every snapshot arrives
through the public API exactly as a customer collector would send it.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
OTHER_HEAD_SHA = "3" * 40

# The exact document shape run 31410071252 certified for PR #57, so the
# preservation tests assert against real persisted evidence rather than an
# invented one.
CERTIFIED_SEMANTIC = {
    "status": "evaluated",
    "change_count": 1,
    "models": [{
        "model_name": "int_customer_orders",
        "status": "evaluated",
        "changes": [{
            "kind": "filter_changed",
            "scope": "where",
            "before_sql": None,
            "after_sql": "NOT status IS NULL /* relium semantic e2e 629de6afc0-main */",
            "model_name": "int_customer_orders",
            "model_unique_id": "model.relium_e2e_dbt.int_customer_orders",
        }],
    }],
}


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _model(name, deps=(), cols=(), schema="analytics"):
    return {"resource_type": "model", "name": name, "schema": schema, "alias": name,
            "database": "warehouse", "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols}}


def _manifests():
    """Base reads only order_id; head introduces a dependency on the external
    source column discount_amount."""
    sources = {"source.a.raw.orders": {
        "schema": "raw", "name": "orders", "database": "warehouse",
        "columns": {"order_id": {}, "discount_amount": {}}}}
    base = {"nodes": {"model.a.fct_orders": _model(
        "fct_orders", ["source.a.raw.orders"], ["order_id"])}, "sources": sources}
    head = {"nodes": {"model.a.fct_orders": _model(
        "fct_orders", ["source.a.raw.orders"], ["order_id", "net_revenue"])},
        "sources": sources}
    return base, head


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class MetadataReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="integration-secret", job_queue=_StubQueue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool)
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        from agent.api.auth import generate_token, hash_secret

        unique = uuid.uuid4().hex[:8]
        self.org = f"org-{unique}"
        self.repo = f"repo-{unique}"
        self.env = "production"
        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(self.org, self.repo, self.env)
            store.create_service_token(token_id, hash_secret(secret), self.org,
                                       self.repo, environment=self.env,
                                       description="integration", scope="collector")
        # The collector credential. It submits snapshots and claims work; it
        # is deliberately not allowed to browse the dashboard.
        self.auth = {"Authorization": f"Bearer {presented}"}
        read_id, read_secret, read_token = generate_token()
        with self.pool.acquire() as store:
            store.create_service_token(read_id, hash_secret(read_secret), self.org,
                                       self.repo, environment=self.env,
                                       description="integration-read",
                                       scope="operator_read")
        self.read_auth = {"Authorization": f"Bearer {read_token}"}
        self.base_manifest, self.head_manifest = _manifests()

    # -- helpers ---------------------------------------------------------

    def _begin(self, *, head_sha=HEAD_SHA, mode="enforce", pull_number=11,
               code_health=100, code_findings=(), health_explanation=None,
               semantic_evidence=None):
        from agent.metadata_evidence.review_lifecycle import begin_review

        with self.pool.acquire() as store:
            return begin_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, pull_number=pull_number,
                base_sha=BASE_SHA, head_sha=head_sha,
                base_manifest=self.base_manifest, head_manifest=self.head_manifest,
                changed_models=["fct_orders"], enforcement_mode=mode,
                delivery_id=f"delivery-{uuid.uuid4().hex[:8]}",
                code_health=code_health, code_findings=code_findings,
                health_explanation=health_explanation,
                semantic_evidence=semantic_evidence)

    def _post(self, path, body, *, key=None):
        headers = dict(self.auth)
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.post(path, json=body, headers=headers)

    def _get(self, path, *, collector=False):
        """Dashboard reads by default; `collector=True` for the collector's own work."""
        return self.client.get(path,
                               headers=self.auth if collector else self.read_auth)

    def _snapshot_body(self, outcome, *, columns=None, observed_at=None,
                       completeness="COMPLETE", ttl_seconds=3600, **overrides):
        columns = columns if columns is not None else [
            {"column_name": "order_id", "data_type": "bigint", "null_rate": 0.0},
            {"column_name": "discount_amount", "data_type": "numeric", "null_rate": 0.01},
        ]
        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
        body = {
            "review_id": outcome.review_id,
            "request_id": outcome.request_id,
            "environment": self.env,
            "attempt": outcome.attempt,
            "completeness": completeness,
            "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
            "ttl_seconds": ttl_seconds,
            "base_sha": review["base_sha"],
            "head_sha": review["head_sha"],
            "relations": [{
                "relation_name": "raw.orders",
                "relation_schema": "raw",
                "exists_in_production": True,
                "schema_fingerprint": "fp-prod",
                "row_count": 10000,
                "columns": columns,
            }],
        }
        body.update(overrides)
        return body

    def _submit(self, outcome, *, key=None, **kwargs):
        return self._post("/api/metadata-snapshots",
                          self._snapshot_body(outcome, **kwargs),
                          key=key or f"idem-{uuid.uuid4().hex[:12]}")

    def _run_worker(self, review_id):
        """Execute the real recomputation handler through the worker registry.

        The outbox holds more than recomputation jobs - a recomputed decision
        also enqueues its republication - so this claims until it reaches the
        recomputation job for THIS review rather than assuming the queue holds
        exactly one claimable item. Every job it passes over is still
        dispatched and completed, so nothing is left half-claimed.
        """
        from agent.metadata_evidence.recompute import EVENT_TYPE as RECOMPUTE
        from agent.worker.lifecycle_worker import JobContext, registry

        with self.pool.acquire() as store:
            for _ in range(20):
                job = store.claim_outbox(self.org, self.repo, self.env, "worker-test")
                if job is None:
                    break
                result = registry.dispatch(job["event_type"], JobContext(store, job))
                store.complete_outbox(self.org, self.repo, job["event_id"])
                if (job["event_type"] == RECOMPUTE
                        and (job.get("payload") or {}).get("review_id") == review_id):
                    return job, result
        self.fail(f"no recomputation job was claimable for {review_id}")

    # -- 1..6 review persistence and waiting ------------------------------

    def test_review_persists_to_postgresql(self):
        outcome = self._begin()
        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
        self.assertIsNotNone(review)
        self.assertEqual(review["pull_number"], 11)
        self.assertEqual(review["base_sha"], BASE_SHA)
        self.assertEqual(review["head_sha"], HEAD_SHA)
        self.assertTrue(review["base_manifest_hash"])
        self.assertTrue(review["head_manifest_hash"])
        self.assertTrue(review["policy_version"])
        self.assertEqual(review["enforcement_mode"], "enforce")
        self.assertEqual(review["attempt"], 1)

    def test_duplicate_webhook_is_idempotent(self):
        first = self._begin()
        second = self._begin()
        self.assertEqual(first.review_id, second.review_id)
        self.assertEqual(second.attempt, 1)
        with self.pool.acquire() as store:
            attempts = store.review_attempts(self.org, self.repo, first.review_id)
            requests = store.get_collection_request(
                self.org, self.repo, first.request_id)
        self.assertEqual(len(attempts), 1)
        self.assertIsNotNone(requests)

    def test_new_head_sha_creates_a_separate_review(self):
        first = self._begin(head_sha=HEAD_SHA)
        second = self._begin(head_sha=OTHER_HEAD_SHA)
        self.assertNotEqual(first.review_id, second.review_id)
        with self.pool.acquire() as store:
            self.assertIsNotNone(store.get_review(self.org, self.repo, first.review_id))
            self.assertIsNotNone(store.get_review(self.org, self.repo, second.review_id))

    def test_required_metadata_creates_a_bounded_targeted_request(self):
        outcome = self._begin()
        self.assertTrue(outcome.metadata_required)
        self.assertIsNotNone(outcome.request_id)
        with self.pool.acquire() as store:
            request = store.get_collection_request(
                self.org, self.repo, outcome.request_id)
        names = {t["relation_name"] for t in request["targets"]}
        self.assertIn("raw.orders", names)
        self.assertLessEqual(len(names), 3, "request must stay bounded")
        self.assertEqual(request["base_sha"], BASE_SHA)
        self.assertEqual(request["head_sha"], HEAD_SHA)
        self.assertTrue(request["head_manifest_hash"])
        self.assertEqual(request["plan"]["attempt"], 1)

    def test_review_waits_with_no_decision(self):
        outcome = self._begin()
        self.assertTrue(outcome.waiting)
        self.assertIsNone(outcome.decision)
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertEqual(outcome.coverage, "INCOMPLETE")
        self.assertEqual(outcome.health, 100)
        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
        self.assertIsNone(review["decision"])
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")

    def test_request_is_persisted_before_the_waiting_state(self):
        """A crash between the two must never leave a review waiting on a
        request that does not exist."""
        outcome = self._begin()
        with self.pool.acquire() as store:
            transitions = store.review_transitions(self.org, self.repo, outcome.review_id)
            request = store.get_collection_request(self.org, self.repo, outcome.request_id)
        states = [t["to_state"] for t in transitions]
        self.assertIsNotNone(request)
        self.assertLess(states.index("METADATA_REQUESTED"),
                        states.index("WAITING_FOR_METADATA"))

    # -- 8..18 snapshot submission and validation -------------------------

    def test_snapshot_accepted_through_the_public_api(self):
        outcome = self._begin()
        response = self._submit(outcome)
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertTrue(payload["recomputation_queued"])

    def test_duplicate_snapshot_creates_one_effective_snapshot(self):
        """A true replay resends the SAME payload. The identical body is sent
        twice deliberately: a helper that regenerated observed_at would make
        this a conflicting replay instead of a duplicate."""
        outcome = self._begin()
        key = f"idem-{uuid.uuid4().hex[:12]}"
        body = self._snapshot_body(outcome)
        first = self._post("/api/metadata-snapshots", body, key=key)
        second = self._post("/api/metadata-snapshots", body, key=key)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["snapshot_id"], second.json()["snapshot_id"])
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT count(*) AS n FROM metadata_snapshots WHERE organization_id=%s",
                (self.org,)).fetchone()["n"]
            jobs = store.review_recomputation_jobs(self.org, self.repo,
                                                   review_id=outcome.review_id)
        self.assertEqual(count, 1)
        self.assertEqual(len(jobs), 1, "one snapshot, one recomputation")

    def test_conflicting_replay_is_rejected(self):
        outcome = self._begin()
        key = f"idem-{uuid.uuid4().hex[:12]}"
        self._submit(outcome, key=key)
        conflicting = self._submit(
            outcome, key=key,
            columns=[{"column_name": "order_id", "data_type": "bigint",
                      "null_rate": 0.99}])
        self.assertIn(conflicting.status_code, (409, 422), conflicting.text)

    def test_wrong_head_sha_is_rejected(self):
        outcome = self._begin()
        response = self._submit(outcome, head_sha="9" * 40)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(response.json()["checks"]["head_sha"])

    def test_wrong_base_sha_is_rejected(self):
        outcome = self._begin()
        response = self._submit(outcome, base_sha="8" * 40)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["checks"]["base_sha"])

    def test_wrong_manifest_hash_is_rejected(self):
        outcome = self._begin()
        response = self._submit(outcome, head_manifest_hash="not-the-hash")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["checks"]["head_manifest_hash"])

    def test_wrong_review_attempt_is_rejected(self):
        outcome = self._begin()
        response = self._submit(outcome, attempt=99)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["checks"]["attempt"])

    def test_unknown_review_is_not_found(self):
        outcome = self._begin()
        body = self._snapshot_body(outcome)
        body["review_id"] = "gh-does-not-exist"
        response = self._post("/api/metadata-snapshots", body,
                              key=f"idem-{uuid.uuid4().hex[:12]}")
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_review_is_not_found_not_forbidden(self):
        """Another tenant's review must be indistinguishable from a missing
        one - a 403 would confirm it exists."""
        from agent.api.auth import generate_token, hash_secret

        outcome = self._begin()
        other_org, other_repo = f"org-{uuid.uuid4().hex[:8]}", f"repo-{uuid.uuid4().hex[:8]}"
        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(other_org, other_repo, self.env)
            store.create_service_token(token_id, hash_secret(secret), other_org,
                                       other_repo, environment=self.env,
                                       description="other tenant")
        body = self._snapshot_body(outcome)
        body["request_id"] = None
        response = self.client.post(
            "/api/metadata-snapshots", json=body,
            headers={"Authorization": f"Bearer {presented}",
                     "Idempotency-Key": f"idem-{uuid.uuid4().hex[:12]}"})
        self.assertEqual(response.status_code, 404, response.text)

    def test_rejected_snapshot_leaves_no_accepted_binding(self):
        outcome = self._begin()
        self._submit(outcome, head_sha="9" * 40)
        with self.pool.acquire() as store:
            accepted = store.review_bindings(self.org, self.repo, outcome.review_id,
                                             state="ACCEPTED")
            rejected = store.review_bindings(self.org, self.repo, outcome.review_id,
                                             state="REJECTED")
            jobs = store.review_recomputation_jobs(self.org, self.repo,
                                                   review_id=outcome.review_id)
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("head_sha", rejected[0]["rejection_reason"])
        self.assertEqual(jobs, [], "a rejected snapshot must not queue recomputation")

    # -- 19..21 recomputation ---------------------------------------------

    def test_snapshot_arrival_queues_recomputation(self):
        outcome = self._begin()
        self._submit(outcome)
        with self.pool.acquire() as store:
            jobs = store.review_recomputation_jobs(self.org, self.repo,
                                                   review_id=outcome.review_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["event_type"], "metadata.review_recompute_requested")

    def test_real_worker_consumes_recomputation_and_decides(self):
        outcome = self._begin()
        self._submit(outcome)
        job, result = self._run_worker(outcome.review_id)
        self.assertEqual(job["event_type"], "metadata.review_recompute_requested")
        self.assertEqual(result["status"], "recomputed")
        self.assertEqual(result["decision"], "ALLOW")
        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
            attempts = store.review_attempts(self.org, self.repo, outcome.review_id)
        self.assertEqual(review["decision"], "ALLOW")
        self.assertEqual(review["evidence_coverage"], "COMPLETE")
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")
        self.assertEqual([a["attempt"] for a in attempts], [1, 2])
        self.assertIsNone(attempts[0]["decision"], "waiting attempt is preserved")

    def test_recomputation_replaces_pending_metadata_finding_but_preserves_code_risk(self):
        code_finding = {
            "code": "LEFT_JOIN_NULLIFIED",
            "severity": "block",
            "category": "code",
            "message": (
                "A WHERE filtering on right-side columns can convert LEFT JOIN "
                "to INNER JOIN, dropping rows"
            ),
            "relation": "int_subscription_revenue",
            "detail": {
                "title": "LEFT JOIN possibly nullified by WHERE",
                "source_severity": "high",
            },
        }
        health_explanation = {
            "score": 65,
            "label": "Code review health",
            "basis": "static_code_and_manifest_analysis",
            "deductions": [{
                "component": "ast",
                "points": 35,
                "reason": code_finding["message"],
            }],
        }
        outcome = self._begin(
            pull_number=40,
            code_health=65,
            code_findings=[code_finding],
            health_explanation=health_explanation,
            semantic_evidence=CERTIFIED_SEMANTIC,
        )
        first_before = self._attempts_by_number(outcome.review_id)[1]
        self.assertIn(
            "metadata.pending",
            {finding["code"] for finding in first_before["payload"]["findings"]},
        )

        self._submit(outcome)
        _, result = self._run_worker(outcome.review_id)

        attempts = self._attempts_by_number(outcome.review_id)
        first_after = attempts[1]
        second = attempts[2]
        self.assertEqual(first_after["payload"], first_before["payload"])
        second_codes = {f["code"] for f in second["payload"]["findings"]}
        self.assertIn("LEFT_JOIN_NULLIFIED", second_codes)
        self.assertNotIn("metadata.pending", second_codes)
        self.assertEqual(second["decision"], "BLOCK")
        self.assertEqual(second["health"], 65)
        self.assertEqual(second["payload"]["primary_reason"], code_finding["message"])
        self.assertEqual(second["payload"]["health_explanation"], health_explanation)
        self.assertEqual(result["primary_reason"], code_finding["message"])

        api_attempts = self._get(
            f"/api/reviews/{outcome.review_id}/attempts"
        ).json()["attempts"]
        current = next(item for item in api_attempts if item["attempt"] == 2)
        self.assertEqual(current["primary_reason"], code_finding["message"])
        self.assertEqual(current["health_explanation"]["score"], 65)

    def test_duplicate_recomputation_produces_one_final_decision(self):
        outcome = self._begin()
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        from agent.metadata_evidence.recompute import recompute_review

        with self.pool.acquire() as store:
            repeat = recompute_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, review_id=outcome.review_id)
            attempts = store.review_attempts(self.org, self.repo, outcome.review_id)
        self.assertEqual(repeat["status"], "already_recomputed")
        self.assertFalse(repeat["applied"])
        self.assertEqual(len(attempts), 2)

    def test_worker_restart_does_not_lose_the_accepted_snapshot(self):
        outcome = self._begin()
        self._submit(outcome)
        # Simulate a crash after claim: the lease expires and the job returns.
        with self.pool.acquire() as store:
            claimed = store.claim_outbox(self.org, self.repo, self.env, "worker-doomed")
            self.assertIsNotNone(claimed)
            store.connection.execute(
                "UPDATE outbox_events SET lease_expires_at = now() - interval '1 minute' "
                "WHERE organization_id=%s AND event_id=%s",
                (self.org, claimed["event_id"]))
        job, result = self._run_worker(outcome.review_id)
        self.assertEqual(result["status"], "recomputed")
        with self.pool.acquire() as store:
            snapshot = store.latest_accepted_snapshot(self.org, self.repo,
                                                      outcome.review_id)
        self.assertIsNotNone(snapshot)

    # -- semantic evidence survives metadata recomputation ------------------
    #
    # A recomputation changes production evidence, not code provenance. A
    # review is bound to one pull_number/head_sha and attempts carry no SHA of
    # their own, so attempt 2 describes exactly the code attempt 1 described
    # and the semantic evidence computed for it is still true. Attempt 2
    # previously stored NULL, and the dashboard - which correctly reads only
    # the current attempt - therefore told the customer that semantic
    # differences "were not persisted" while the product held them.

    def _begin_with_semantic(self, evidence, *, pull_number=41,
                             head_sha=None):
        from agent.metadata_evidence.review_lifecycle import begin_review

        with self.pool.acquire() as store:
            return begin_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, pull_number=pull_number,
                base_sha=BASE_SHA, head_sha=head_sha or HEAD_SHA,
                base_manifest=self.base_manifest, head_manifest=self.head_manifest,
                changed_models=["fct_orders"], enforcement_mode="enforce",
                delivery_id=f"delivery-{uuid.uuid4().hex[:8]}",
                semantic_evidence=evidence)

    def _attempts_by_number(self, review_id):
        with self.pool.acquire() as store:
            return {a["attempt"]: a
                    for a in store.review_attempts(self.org, self.repo, review_id)}

    def test_semantic_evidence_is_preserved_onto_the_recomputed_attempt(self):
        outcome = self._begin_with_semantic(CERTIFIED_SEMANTIC)
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        attempts = self._attempts_by_number(outcome.review_id)
        self.assertEqual(sorted(attempts), [1, 2])
        self.assertEqual(attempts[2]["semantic_evidence"],
                         attempts[1]["semantic_evidence"])
        self.assertEqual(attempts[2]["semantic_evidence"], CERTIFIED_SEMANTIC)

    def test_recomputed_attempt_carries_comparison_and_decision_too(self):
        """Preserving semantics must not displace the new metadata evidence."""
        outcome = self._begin_with_semantic(CERTIFIED_SEMANTIC, pull_number=42)
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        second = self._attempts_by_number(outcome.review_id)[2]
        self.assertIsNotNone(second["semantic_evidence"])
        self.assertIsNotNone(second["metadata_comparison"])
        self.assertIsNotNone(second["decision"])
        self.assertEqual(second["trigger"], "metadata_snapshot")
        self.assertIsNotNone(second["snapshot_id"])

    def test_semantic_evidence_is_never_taken_from_another_review(self):
        """The carry-forward is scoped to one review, not to 'the latest'."""
        donor = self._begin_with_semantic(CERTIFIED_SEMANTIC, pull_number=43)
        self._submit(donor)
        self._run_worker(donor.review_id)

        # A different review, on a different head, that compared nothing.
        bare = self._begin(pull_number=44, head_sha=OTHER_HEAD_SHA)
        self._submit(bare)
        self._run_worker(bare.review_id)

        self.assertIsNotNone(
            self._attempts_by_number(donor.review_id)[2]["semantic_evidence"])
        self.assertIsNone(
            self._attempts_by_number(bare.review_id)[2]["semantic_evidence"],
            "evidence leaked across review ids")

    def test_absent_semantic_evidence_stays_absent(self):
        """NULL means no comparison ran. It must not become 'found nothing'."""
        outcome = self._begin(pull_number=45)
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        attempts = self._attempts_by_number(outcome.review_id)
        self.assertIsNone(attempts[1]["semantic_evidence"])
        self.assertIsNone(attempts[2]["semantic_evidence"])

    def test_repeated_recomputation_does_not_diverge(self):
        outcome = self._begin_with_semantic(CERTIFIED_SEMANTIC, pull_number=46)
        self._submit(outcome)
        self._run_worker(outcome.review_id)
        first = self._attempts_by_number(outcome.review_id)[2]["semantic_evidence"]

        from agent.metadata_evidence.recompute import recompute_review

        with self.pool.acquire() as store:
            repeat = recompute_review(
                store, organization_id=self.org, repository_id=self.repo,
                environment=self.env, review_id=outcome.review_id)
        self.assertEqual(repeat["status"], "already_recomputed")

        attempts = self._attempts_by_number(outcome.review_id)
        self.assertEqual(sorted(attempts), [1, 2])
        self.assertEqual(attempts[2]["semantic_evidence"], first)

    def test_api_current_attempt_projection_exposes_the_evidence(self):
        outcome = self._begin_with_semantic(CERTIFIED_SEMANTIC, pull_number=47)
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        body = self._get(f"/api/reviews/{outcome.review_id}/attempts").json()
        current = body["current_attempt"]
        self.assertEqual(current, 2)
        projected = next(a for a in body["attempts"] if a["attempt"] == current)
        evidence = projected["semantic_evidence"]
        self.assertIsNotNone(evidence, "current attempt projects no semantics")
        self.assertEqual(evidence["status"], "evaluated")
        self.assertEqual([c["kind"] for c in evidence["changes"]],
                         ["filter_changed"])

    def test_the_certified_phase_b_scenario(self):
        """attempt 1 filter_changed -> recompute -> attempt 2 WARN, same evidence."""
        outcome = self._begin_with_semantic(CERTIFIED_SEMANTIC, pull_number=48)
        self.assertIsNone(outcome.decision)
        self._submit(outcome, columns=[
            {"column_name": "order_id", "data_type": "bigint", "null_rate": 0.0},
            {"column_name": "discount_amount", "data_type": "numeric",
             "null_rate": 0.82}])
        _, result = self._run_worker(outcome.review_id)

        self.assertEqual(result["decision"], "WARN")
        self.assertIn("column.high_null_rate",
                      {f["code"] for f in result["findings"]})

        second = self._attempts_by_number(outcome.review_id)[2]
        self.assertEqual(second["decision"], "WARN")
        change = second["semantic_evidence"]["models"][0]["changes"][0]
        self.assertEqual(change["kind"], "filter_changed")
        self.assertEqual(change["model_unique_id"],
                         "model.relium_e2e_dbt.int_customer_orders")
        self.assertEqual(change["scope"], "where")

        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")
        self.assertEqual(review["health"], 100)

    # -- 22..27 metadata changes the decision ------------------------------

    def test_metadata_changes_the_final_decision(self):
        """Missing external column: waiting -> BLOCK once evidence arrives."""
        outcome = self._begin()
        self.assertIsNone(outcome.decision)
        self._submit(outcome, columns=[
            {"column_name": "order_id", "data_type": "bigint", "null_rate": 0.0}])
        _, result = self._run_worker(outcome.review_id)
        self.assertEqual(result["decision"], "BLOCK")
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("column.missing_in_production", codes)

    def test_high_null_rate_warns(self):
        outcome = self._begin()
        self._submit(outcome, columns=[
            {"column_name": "order_id", "data_type": "bigint", "null_rate": 0.0},
            {"column_name": "discount_amount", "data_type": "numeric",
             "null_rate": 0.82}])
        _, result = self._run_worker(outcome.review_id)
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("column.high_null_rate", codes)
        self.assertEqual(result["decision"], "WARN")

    def test_type_mismatch_blocks(self):
        outcome = self._begin()
        with self.pool.acquire() as store:
            review = store.get_review(self.org, self.repo, outcome.review_id)
            payload = dict(review["payload"])
            for target in payload["plan"]["targets"]:
                if target["relation_name"] == "raw.orders":
                    target["column_types"] = {"discount_amount": "numeric"}
            store.connection.execute(
                "UPDATE reviews SET payload=%s WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s",
                (store._Jsonb(payload), self.org, self.repo, outcome.review_id))
        self._submit(outcome, columns=[
            {"column_name": "order_id", "data_type": "bigint"},
            {"column_name": "discount_amount", "data_type": "varchar"}])
        _, result = self._run_worker(outcome.review_id)
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("column.type_mismatch", codes)
        self.assertEqual(result["decision"], "BLOCK")

    def test_stale_snapshot_is_not_treated_as_current(self):
        outcome = self._begin()
        self._submit(outcome,
                     observed_at=datetime.now(timezone.utc) - timedelta(hours=6),
                     ttl_seconds=900)
        _, result = self._run_worker(outcome.review_id)
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["coverage"], "INCOMPLETE")
        self.assertEqual(result["lifecycle_state"], "METADATA_STALE")
        self.assertEqual(result["health"], 100)

    def test_partial_snapshot_stays_incomplete(self):
        outcome = self._begin()
        self._submit(outcome, completeness="PARTIAL")
        _, result = self._run_worker(outcome.review_id)
        self.assertEqual(result["coverage"], "INCOMPLETE")
        self.assertNotEqual(result["decision"], "ALLOW")

    def test_required_missing_metadata_never_yields_allow(self):
        for mode, expected in (("shadow", "WARN"), ("enforce", "BLOCK")):
            with self.subTest(mode=mode):
                outcome = self._begin(head_sha=uuid.uuid4().hex.ljust(40, "0")[:40],
                                      mode=mode)
                self._submit(outcome, columns=[
                    {"column_name": "order_id", "data_type": "bigint"}])
                _, result = self._run_worker(outcome.review_id)
                self.assertNotEqual(result["decision"], "ALLOW")
                self.assertEqual(result["decision"], expected)
                self.assertEqual(result["health"], 100)

    # -- 31 privacy ---------------------------------------------------------

    def test_raw_rows_and_credentials_are_rejected_at_the_boundary(self):
        outcome = self._begin()
        for forbidden in ({"rows": [{"order_id": 1}]},
                          {"sql": "SELECT * FROM orders"},
                          {"password": "hunter2"},
                          {"connection_string": "postgresql://u:p@h/db"}):
            with self.subTest(field=next(iter(forbidden))):
                body = self._snapshot_body(outcome)
                body.update(forbidden)
                response = self._post("/api/metadata-snapshots", body,
                                      key=f"idem-{uuid.uuid4().hex[:12]}")
                self.assertEqual(response.status_code, 422, response.text)

    def test_unbounded_column_values_are_truncated(self):
        outcome = self._begin()
        self._submit(outcome, columns=[
            {"column_name": "order_id", "data_type": "bigint"},
            {"column_name": "discount_amount", "data_type": "numeric",
             "max_value": "z" * 9000}])
        with self.pool.acquire() as store:
            snapshot = store.latest_accepted_snapshot(self.org, self.repo,
                                                      outcome.review_id)
        values = [c["max_value"] for r in snapshot["relations"]
                  for c in r["columns"] if c["max_value"]]
        self.assertTrue(values)
        self.assertLessEqual(max(len(v) for v in values), 256)

    # -- collector control API ---------------------------------------------

    def test_collector_can_poll_and_acknowledge_its_request(self):
        outcome = self._begin()
        listing = self._get("/api/collection-requests", collector=True)
        self.assertEqual(listing.status_code, 200)
        ids = [r["request_id"] for r in listing.json()["requests"]]
        self.assertIn(outcome.request_id, ids)

        detail = self._get(f"/api/collection-requests/{outcome.request_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["request"]["targets"])

        ack = self._post(f"/api/collection-requests/{outcome.request_id}/acknowledge",
                         {"collector_id": "collector-1"})
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.json()["status"], "acknowledged")

    def test_collector_failure_is_recorded_sanitised(self):
        outcome = self._begin()
        response = self._post(
            f"/api/collection-requests/{outcome.request_id}/failure",
            {"reason": "warehouse unreachable"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request"]["state"], "FAILED")

    def test_another_tenant_cannot_see_the_request(self):
        from agent.api.auth import generate_token, hash_secret

        self._begin()
        other_org, other_repo = f"org-{uuid.uuid4().hex[:8]}", f"repo-{uuid.uuid4().hex[:8]}"
        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(other_org, other_repo, self.env)
            store.create_service_token(token_id, hash_secret(secret), other_org,
                                       other_repo, environment=self.env,
                                       description="other")
        response = self.client.get("/api/collection-requests",
                                   headers={"Authorization": f"Bearer {presented}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requests"], [])

    def test_collector_registration_and_revocation_blocks_submission(self):
        outcome = self._begin()
        registered = self._post("/api/collectors", {"collector_id": "collector-9",
                                                    "adapter_type": "postgres"})
        self.assertEqual(registered.status_code, 200)
        with self.pool.acquire() as store:
            store.revoke_collector(self.org, self.repo, "collector-9", reason="rotated")
        body = self._snapshot_body(outcome)
        body["collector_id"] = "collector-9"
        response = self._post("/api/metadata-snapshots", body,
                              key=f"idem-{uuid.uuid4().hex[:12]}")
        self.assertEqual(response.status_code, 409, response.text)

    # -- 30 dashboard -------------------------------------------------------

    def test_dashboard_exposes_the_complete_lifecycle(self):
        outcome = self._begin()
        self._submit(outcome)
        self._run_worker(outcome.review_id)

        coverage = self._get(f"/api/reviews/{outcome.review_id}/evidence-coverage")
        self.assertEqual(coverage.status_code, 200)
        payload = coverage.json()
        self.assertEqual(payload["decision"], "ALLOW")
        self.assertEqual(payload["evidence_coverage"], "COMPLETE")
        self.assertEqual(payload["lifecycle_state"], "DECISION_READY")
        self.assertEqual(payload["health"], 100)
        self.assertEqual(payload["enforcement_mode"], "enforce")
        sources = {e["source"] for e in payload["evidence"]}
        self.assertIn("production_metadata", sources)
        groups = {e["evidence_state_group"] for e in payload["evidence"]}
        self.assertEqual(groups, {"base_code", "head_code", "production"})

        review = self._get(f"/api/reviews/{outcome.review_id}")
        self.assertEqual(review.status_code, 200)

    def test_dashboard_does_not_leak_sql_or_credentials(self):
        outcome = self._begin()
        self._submit(outcome)
        self._run_worker(outcome.review_id)
        for path in (f"/api/reviews/{outcome.review_id}",
                     f"/api/reviews/{outcome.review_id}/evidence-coverage",
                     f"/api/collection-requests/{outcome.request_id}"):
            with self.subTest(path=path):
                text = self._get(path).text.lower()
                for forbidden in ("select ", "insert into", "postgresql://",
                                  "password", "secret", "private key"):
                    self.assertNotIn(forbidden, text)

    def test_snapshot_status_is_retrievable(self):
        outcome = self._begin()
        snapshot_id = self._submit(outcome).json()["snapshot_id"]
        response = self._get(f"/api/metadata-snapshots/{snapshot_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["snapshot"]["completeness"], "COMPLETE")
        self.assertEqual(payload["snapshot"]["freshness_state"], "CURRENT")
        self.assertEqual(payload["bindings"][0]["binding_state"], "ACCEPTED")

    def test_audit_history_is_preserved(self):
        outcome = self._begin()
        self._submit(outcome)
        self._run_worker(outcome.review_id)
        with self.pool.acquire() as store:
            events = [e["event_type"] for e in
                      store.audit_events(self.org, self.repo)]
            transitions = [t["to_state"] for t in
                           store.review_transitions(self.org, self.repo,
                                                    outcome.review_id)]
        self.assertIn("review.analysed", events)
        self.assertIn("snapshot.accepted", events)
        self.assertIn("review.recomputed", events)
        self.assertIn("WAITING_FOR_METADATA", transitions)
        self.assertIn("DECISION_READY", transitions)


if __name__ == "__main__":
    unittest.main()
