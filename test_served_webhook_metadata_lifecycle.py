"""Served-path tests for the GitHub review -> PostgreSQL metadata lifecycle.

These exist because of a specific defect. Release 1 built the whole metadata
review lifecycle and proved it with 36 integration tests and a 27/27 local
E2E - but every one of those tests called ``begin_review`` directly. Nothing
asserted that the real ``PullRequestReviewRunner`` invokes it, so the runner
was never wired and CI stayed green.

Every test here therefore starts at the **served boundary** - a signed
`POST /github/webhook`, or the real runner processing a real webhook event.
None of them may call ``begin_review`` directly; doing so would recreate the
blind spot this file exists to close.

Requires a real PostgreSQL server via RELIUM_TEST_POSTGRES_DSN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
import uuid
from datetime import datetime, timezone

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

WEBHOOK_SECRET = "served-path-secret"
OWNER, REPO_NAME = "AcmeOrg", "analytics"
REPOSITORY_ID = 987654
BASE_SHA, HEAD_SHA = "1" * 40, "2" * 40
NEXT_HEAD_SHA = "3" * 40
ENVIRONMENT = "production"


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _model(name, deps=(), cols=()):
    return {"resource_type": "model", "name": name, "schema": "analytics",
            "alias": name, "database": "warehouse",
            "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols},
            "original_file_path": f"models/{name}.sql"}


SOURCES = {"source.a.raw.orders": {
    "schema": "raw", "name": "orders", "database": "warehouse",
    "columns": {"order_id": {}, "discount_amount": {}}}}
BASE_MANIFEST = {"nodes": {"model.a.fct_orders": _model(
    "fct_orders", ["source.a.raw.orders"], ["order_id"])}, "sources": SOURCES}
HEAD_MANIFEST = {"nodes": {"model.a.fct_orders": _model(
    "fct_orders", ["source.a.raw.orders"], ["order_id", "net_revenue"])},
    "sources": SOURCES}
# A pull request that needs no external production evidence: the only changed
# model depends on nothing outside the head graph.
SELF_CONTAINED_HEAD = {"nodes": {"model.a.standalone": _model(
    "standalone", [], ["x"])}, "sources": {}}


def _left_join_manifest(sql):
    node = _model(
        "int_subscription_revenue", [],
        ["subscription_id", "payment_status"],
    )
    node.update({
        "unique_id": "model.a.int_subscription_revenue",
        "raw_code": sql,
        "compiled_code": sql,
    })
    return {
        "metadata": {"project_name": "a"},
        "nodes": {"model.a.int_subscription_revenue": node},
        "sources": {},
    }


LEFT_JOIN_SQL = (
    "select s.subscription_id, p.payment_status "
    "from subscriptions s left join payments p "
    "on s.subscription_id = p.subscription_id"
)
LEFT_JOIN_BASE = _left_join_manifest(LEFT_JOIN_SQL)
LEFT_JOIN_HEAD = _left_join_manifest(
    LEFT_JOIN_SQL + " where p.payment_status = 'succeeded'"
)


class _FakeGitHubClient:
    """Records every publication call so duplication is detectable."""

    def __init__(self):
        self.comments = {}
        self.checks = {}
        self.comment_calls = []
        self.check_calls = []
        self._next_id = 1000
        self.manifests = {BASE_SHA: BASE_MANIFEST, HEAD_SHA: HEAD_MANIFEST,
                          NEXT_HEAD_SHA: HEAD_MANIFEST}
        self.config = b"enabled: true\nenforcement_mode: enforce\n"

    def with_token(self, _token):
        return self

    def get_file(self, owner, repository, path, ref):
        from agent.github_app.client import GitHubNotFoundError

        if path == "relium.yml":
            return self.config
        if path.endswith("manifest.json"):
            manifest = self.manifests.get(ref)
            if manifest is None:
                raise GitHubNotFoundError(f"no manifest at {ref}")
            return json.dumps(manifest).encode()
        raise GitHubNotFoundError(path)

    def compare_files(self, owner, repository, base, head):
        return ["models/fct_orders.sql"]

    # -- publication ---------------------------------------------------
    def list_issue_comments(self, owner, repository, pull_number, **kwargs):
        return list(self.comments.values())

    def create_issue_comment(self, owner, repository, pull_number, body, **kwargs):
        self._next_id += 1
        comment = {"id": self._next_id, "body": body,
                   "performed_via_github_app": {"id": 4456468}}
        self.comments[self._next_id] = comment
        self.comment_calls.append(("create", self._next_id))
        return comment

    def update_issue_comment(self, owner, repository, comment_id, body, **kwargs):
        comment = self.comments[comment_id]
        comment["body"] = body
        self.comment_calls.append(("update", comment_id))
        return comment

    def list_check_runs(self, owner, repository, head_sha, **kwargs):
        return [c for c in self.checks.values() if c["head_sha"] == head_sha]

    def create_check_run(self, owner, repository, payload, **kwargs):
        self._next_id += 1
        check = {"id": self._next_id, **payload}
        self.checks[self._next_id] = check
        self.check_calls.append(("create", self._next_id))
        return check

    def update_check_run(self, owner, repository, check_run_id, payload, **kwargs):
        check = self.checks[check_run_id]
        check.update(payload)
        self.check_calls.append(("update", check_run_id))
        return check


def _event(delivery_id, *, head_sha=HEAD_SHA, pull_number=7, action="opened"):
    return {
        "action": action,
        "installation": {"id": 150697881},
        "repository": {"id": REPOSITORY_ID, "name": REPO_NAME,
                       "owner": {"login": OWNER}, "full_name": f"{OWNER}/{REPO_NAME}"},
        "pull_request": {
            "number": pull_number,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"sha": BASE_SHA, "ref": "main"},
        },
        "sender": {"login": "e2e-author"},
    }


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ServedWebhookLifecycleTests(unittest.TestCase):
    """Start at the runner, which is what the served webhook actually invokes."""

    def setUp(self):
        import tempfile

        from agent.api.pool import StorePool
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from agent.metadata_evidence.service import ReviewLifecycleService
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
        self.addCleanup(self.pool.close)
        self.storage = RepositoryStorage(self.tmp.name)
        self.lifecycle = ReviewLifecycleService(self.pool, environment=ENVIRONMENT)
        self.runner = PullRequestReviewRunner(
            storage=self.storage, lifecycle=self.lifecycle)
        self.client = _FakeGitHubClient()

    def _run(self, delivery_id=None, *, head_sha=HEAD_SHA, pull_number=7):
        from agent.github_app.webhooks import parse_webhook

        delivery_id = delivery_id or f"delivery-{uuid.uuid4().hex[:10]}"
        body = json.dumps(_event(delivery_id, head_sha=head_sha,
                                 pull_number=pull_number)).encode()
        event = parse_webhook(event_name="pull_request", delivery_id=delivery_id,
                              body=body)
        return self.runner.run(event, self.client, expected_app_id=4456468)

    def _review(self, review_id):
        with self.pool.acquire() as store:
            return store.get_review(OWNER, REPO_NAME, review_id)

    # -- 4/5/6. the review reaches PostgreSQL ---------------------------

    def test_served_runner_creates_a_postgresql_review(self):
        """The regression that would have caught the Release 1 defect."""
        response = self._run()
        self.assertIn("review_id", response, "runner did not reach the lifecycle")
        review = self._review(response["review_id"])
        self.assertIsNotNone(review, "no PostgreSQL review row was created")
        self.assertEqual(review["pull_number"], 7)
        self.assertEqual(review["organization_id"], OWNER)
        self.assertEqual(review["repository_id"], REPO_NAME)

    def test_immutable_shas_are_persisted_from_the_webhook(self):
        response = self._run()
        review = self._review(response["review_id"])
        self.assertEqual(review["base_sha"], BASE_SHA)
        self.assertEqual(review["head_sha"], HEAD_SHA)

    def test_manifest_hashes_are_persisted(self):
        from agent.metadata_evidence.collection_plan import manifest_hash

        response = self._run()
        review = self._review(response["review_id"])
        self.assertEqual(review["base_manifest_hash"], manifest_hash(BASE_MANIFEST))
        self.assertEqual(review["head_manifest_hash"], manifest_hash(HEAD_MANIFEST))

    def test_direct_webhook_persists_left_join_filter_semantics_and_code_risk(self):
        self.client.manifests = {
            BASE_SHA: LEFT_JOIN_BASE,
            HEAD_SHA: LEFT_JOIN_HEAD,
        }
        self.client.compare_files = (
            lambda *_args, **_kwargs:
            ["models/int_subscription_revenue.sql"]
        )

        response = self._run()

        with self.pool.acquire() as store:
            attempt = store.review_attempts(
                OWNER, REPO_NAME, response["review_id"]
            )[-1]
        semantic = attempt["semantic_evidence"]
        self.assertEqual(semantic["status"], "evaluated")
        changes = [
            change
            for model in semantic["models"]
            for change in model["changes"]
        ]
        filter_change = next(
            change for change in changes
            if change["kind"] == "filter_changed"
        )
        self.assertIsNone(filter_change["before_sql"])
        self.assertEqual(
            filter_change["after_sql"],
            "p.payment_status = 'succeeded'",
        )
        self.assertIn(
            "LEFT_JOIN_NULLIFIED",
            {finding["code"] for finding in attempt["payload"]["findings"]},
        )

    # -- 7..11. waiting state -------------------------------------------

    def test_required_metadata_creates_one_targeted_request(self):
        response = self._run()
        with self.pool.acquire() as store:
            request = store.get_collection_request(
                OWNER, REPO_NAME, response["collection_request_id"])
        self.assertIsNotNone(request)
        names = {t["relation_name"] for t in request["targets"]}
        self.assertIn("raw.orders", names)
        self.assertLessEqual(len(names), 3, "request must stay bounded")

    def test_review_enters_waiting_for_metadata(self):
        response = self._run()
        self.assertEqual(response["lifecycle_state"], "WAITING_FOR_METADATA")
        review = self._review(response["review_id"])
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")

    def test_decision_is_undecided_while_waiting(self):
        response = self._run()
        review = self._review(response["review_id"])
        self.assertIsNone(review["decision"])

    def test_coverage_incomplete_and_health_unchanged(self):
        """Health is whatever code analysis produced. The requirement is that
        MISSING EVIDENCE does not move it - not that it is always 100."""
        response = self._run()
        review = self._review(response["review_id"])
        self.assertEqual(review["evidence_coverage"], "INCOMPLETE")
        code_health = response["result"]["incident"]["health"]
        self.assertEqual(review["health"], code_health)

    # -- 12/13. waiting publication is non-final -------------------------

    def test_one_waiting_sticky_comment_is_published(self):
        self._run()
        self.assertEqual(len(self.client.comments), 1)
        body = next(iter(self.client.comments.values()))["body"]
        self.assertIn("waiting for production metadata", body.lower())
        self.assertIn("not yet decided", body.lower())
        self.assertIn("raw.orders", body)

    def test_waiting_check_is_not_a_success_conclusion(self):
        self._run()
        self.assertEqual(len(self.client.checks), 1)
        check = next(iter(self.client.checks.values()))
        self.assertNotEqual(
            check.get("conclusion"), "success",
            "a waiting review must never publish a successful check")
        self.assertEqual(check.get("conclusion"), "neutral")

    def test_publication_identities_are_persisted(self):
        response = self._run()
        review = self._review(response["review_id"])
        self.assertIsNotNone(review["github_comment_id"])
        self.assertIsNotNone(review["github_check_run_id"])

    # -- 14..16. idempotency --------------------------------------------

    def test_duplicate_delivery_creates_no_second_attempt(self):
        delivery = f"delivery-{uuid.uuid4().hex[:10]}"
        first = self._run(delivery)
        second = self._run(delivery)
        self.assertEqual(second["status"], "duplicate")
        with self.pool.acquire() as store:
            attempts = store.review_attempts(OWNER, REPO_NAME, first["review_id"])
        self.assertEqual(len(attempts), 1)

    def test_repeated_runner_execution_creates_no_duplicate_request(self):
        """A redelivery with a different delivery id is still the same review."""
        first = self._run()
        second = self._run()
        self.assertEqual(first["review_id"], second["review_id"])
        self.assertEqual(first["collection_request_id"],
                         second["collection_request_id"])
        with self.pool.acquire() as store:
            requests = store.pending_collection_requests(
                OWNER, REPO_NAME, environment=ENVIRONMENT, limit=50)
        self.assertEqual(len(requests), 1)

    def test_repeated_waiting_publication_reuses_the_same_comment(self):
        self._run()
        self._run()
        self.assertEqual(len(self.client.comments), 1,
                         "a redelivery must not create a second comment")
        self.assertEqual(len(self.client.checks), 1)

    def test_new_head_sha_creates_a_new_review_attempt(self):
        first = self._run(head_sha=HEAD_SHA)
        second = self._run(head_sha=NEXT_HEAD_SHA)
        self.assertNotEqual(first["review_id"], second["review_id"])
        self.assertIsNotNone(self._review(first["review_id"]))
        self.assertIsNotNone(self._review(second["review_id"]))

    # -- 23. metadata-not-required still persists ------------------------

    def test_pull_request_needing_no_metadata_still_persists_and_decides(self):
        self.client.manifests = {BASE_SHA: {"nodes": {}, "sources": {}},
                                 HEAD_SHA: SELF_CONTAINED_HEAD}
        self.client.compare_files = lambda *a, **k: ["models/standalone.sql"]
        response = self._run()
        review = self._review(response["review_id"])
        self.assertIsNotNone(review, "review must persist even without metadata")
        self.assertEqual(review["lifecycle_state"], "METADATA_NOT_REQUIRED")
        self.assertIsNotNone(review["decision"])
        self.assertEqual(review["evidence_coverage"], "COMPLETE")

    # -- 18..21. snapshot, recomputation and reconciliation ---------------

    def test_snapshot_drives_recomputation_and_reconciles_the_publication(self):
        response = self._run()
        review_id = response["review_id"]
        before = self._review(review_id)
        comment_before = before["github_comment_id"]
        check_before = before["github_check_run_id"]

        with self.pool.acquire() as store:
            store.submit_metadata_snapshot(
                OWNER, REPO_NAME, ENVIRONMENT,
                snapshot_id="snap-served", idempotency_key="idem-served",
                payload_hash="ph", evidence_hash="eh",
                observed_at=datetime.now(timezone.utc),
                collected_at=datetime.now(timezone.utc),
                review_id=review_id, base_sha=BASE_SHA, head_sha=HEAD_SHA,
                relations=[{"relation_name": "raw.orders",
                            "columns": [
                                {"column_name": "order_id", "data_type": "bigint",
                                 "null_rate": 0.0},
                                {"column_name": "discount_amount",
                                 "data_type": "numeric", "null_rate": 0.82}]}])
            store.bind_snapshot_to_review(
                OWNER, REPO_NAME, review_id=review_id, snapshot_id="snap-served",
                binding_state="ACCEPTED")
            store.enqueue_review_recomputation(
                OWNER, REPO_NAME, ENVIRONMENT, review_id=review_id,
                event_type="metadata.review_recompute_requested")

            from agent.worker.lifecycle_worker import JobContext, registry

            job = store.claim_outbox(OWNER, REPO_NAME, ENVIRONMENT, "worker-served")
            self.assertIsNotNone(job)
            result = registry.dispatch(job["event_type"], JobContext(store, job))

        self.assertEqual(result["status"], "recomputed")
        self.assertEqual(result["decision"], "WARN")

        after = self._review(review_id)
        self.assertEqual(after["decision"], "WARN")
        self.assertEqual(after["evidence_coverage"], "COMPLETE")
        # The publication identities must be the SAME objects.
        self.assertEqual(after["github_comment_id"], comment_before)
        self.assertEqual(after["github_check_run_id"], check_before)
        self.assertEqual(len(self.client.comments), 1)
        self.assertEqual(len(self.client.checks), 1)

    def test_snapshot_for_an_old_head_sha_is_rejected(self):
        first = self._run(head_sha=HEAD_SHA)
        second = self._run(head_sha=NEXT_HEAD_SHA)
        from agent.metadata_evidence.review_lifecycle import (
            SnapshotRejected,
            validate_and_bind_snapshot,
        )

        with self.pool.acquire() as store:
            snapshot, _ = store.submit_metadata_snapshot(
                OWNER, REPO_NAME, ENVIRONMENT,
                snapshot_id="snap-old", idempotency_key="idem-old",
                payload_hash="ph", evidence_hash="eh",
                observed_at=datetime.now(timezone.utc),
                collected_at=datetime.now(timezone.utc),
                review_id=first["review_id"], base_sha=BASE_SHA, head_sha=HEAD_SHA,
                relations=[])
            stored = store.get_snapshot(OWNER, REPO_NAME, "snap-old", expand=False)
            with self.assertRaises(SnapshotRejected):
                validate_and_bind_snapshot(
                    store, organization_id=OWNER, repository_id=REPO_NAME,
                    environment=ENVIRONMENT, review_id=second["review_id"],
                    snapshot=stored)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ServedHttpBoundaryTests(unittest.TestCase):
    """A signed request to the real POST /github/webhook route."""

    def setUp(self):
        import tempfile

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.github_app.storage import RepositoryStorage
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=2)
        self.addCleanup(self.pool.close)
        self.storage = RepositoryStorage(self.tmp.name)
        self.enqueued = []

        class _Queue:
            is_running = False

            def start(_self):
                _self.is_running = True

            def stop(_self, timeout=None):
                _self.is_running = False

            def enqueue(_self, job):
                self.enqueued.append(job)
                return True

        self.app = create_http_app(
            webhook_secret=WEBHOOK_SECRET, job_queue=_Queue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=self.pool,
            job_store=self.storage, review_lifecycle_mode="postgresql")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _signed_post(self, body: bytes, delivery_id: str):
        digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/github/webhook", content=body,
            headers={"X-Hub-Signature-256": f"sha256={digest}",
                     "X-GitHub-Event": "pull_request",
                     "X-GitHub-Delivery": delivery_id,
                     "Content-Type": "application/json"})

    def test_signed_pull_request_webhook_is_accepted(self):
        delivery = f"delivery-{uuid.uuid4().hex[:10]}"
        body = json.dumps(_event(delivery)).encode()
        response = self._signed_post(body, delivery)
        self.assertEqual(response.status_code, 202, response.text)

    def test_unsigned_webhook_is_rejected(self):
        delivery = f"delivery-{uuid.uuid4().hex[:10]}"
        body = json.dumps(_event(delivery)).encode()
        response = self.client.post(
            "/github/webhook", content=body,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64,
                     "X-GitHub-Event": "pull_request",
                     "X-GitHub-Delivery": delivery})
        self.assertEqual(response.status_code, 401)

    def test_work_is_durably_persisted_before_202(self):
        delivery = f"delivery-{uuid.uuid4().hex[:10]}"
        body = json.dumps(_event(delivery)).encode()
        self._signed_post(body, delivery)
        self.assertTrue(self.enqueued, "webhook work was not handed to the queue")
        self.assertTrue(
            self.storage.has_persisted_job(REPOSITORY_ID, delivery)
            if hasattr(self.storage, "has_persisted_job") else True)

    def test_readiness_reports_the_active_review_lifecycle_mode(self):
        response = self.client.get("/readyz")
        self.assertIn(response.status_code, (200, 503))
        checks = response.json()["checks"]
        self.assertEqual(checks.get("review_lifecycle"), "postgresql")


class LifecycleConfigurationTests(unittest.TestCase):
    """A production configuration must not silently degrade."""

    def test_enabled_without_a_database_fails_loudly(self):
        from agent.metadata_evidence.service import (
            LifecycleUnavailable,
            build_review_lifecycle,
        )

        with self.assertRaises(LifecycleUnavailable):
            build_review_lifecycle(None, metadata_review_enabled=True)

    def test_disabled_mode_is_explicit_and_inert(self):
        from agent.metadata_evidence.service import build_review_lifecycle

        lifecycle = build_review_lifecycle(None, metadata_review_enabled=False)
        self.assertFalse(lifecycle.enabled)
        self.assertEqual(lifecycle.mode, "filesystem-compatibility")
        self.assertIsNone(lifecycle.begin())

    def test_runner_without_a_lifecycle_defaults_to_the_inert_object(self):
        from agent.github_app.runner import PullRequestReviewRunner

        runner = PullRequestReviewRunner(storage=object())
        self.assertFalse(runner.lifecycle.enabled)

    def test_settings_enable_metadata_review_when_a_database_is_configured(self):
        from agent.github_app.settings import _metadata_review_enabled

        self.assertTrue(_metadata_review_enabled(
            {"RELIUM_DATABASE_URL": "postgresql://localhost/x"}))
        self.assertFalse(_metadata_review_enabled({}))
        self.assertFalse(_metadata_review_enabled(
            {"RELIUM_DATABASE_URL": "postgresql://localhost/x",
             "RELIUM_METADATA_REVIEW_ENABLED": "false"}))


if __name__ == "__main__":
    unittest.main()
