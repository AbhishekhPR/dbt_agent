"""PostgreSQL release gates for the hosted CI manifest handoff.

Every persistence assertion in this module uses RELIUM_TEST_POSTGRES_DSN.
GitHub is represented by the same in-process fake used by the served webhook
tests; no network or second database is contacted.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from starlette.testclient import TestClient

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
ORG, REPO, ENV = "manifest-org", "manifest-repo", "production"
HEAD_SHA = "2" * 40
BASE_SHA = "1" * 40
OTHER_SHA = "3" * 40
MANIFEST = {
    "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
    "nodes": {"model.a.orders": {"resource_type": "model", "name": "orders"}},
    "sources": {},
}
CONFLICTING_MANIFEST = {
    "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
    "nodes": {"model.a.customers": {"resource_type": "model", "name": "customers"}},
    "sources": {},
}


def _reset_schema():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)


def _apply_through(connection, last_version):
    import hashlib as _hashlib
    from agent import postgres_migrate

    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for path in postgres_migrate._migration_files():
        version = postgres_migrate._version_of(path)
        if version > last_version:
            continue
        sql = path.read_text(encoding="utf-8")
        with connection.transaction():
            connection.execute(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, _hashlib.sha256(sql.encode()).hexdigest()),
            )


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ManifestMigration0013Tests(unittest.TestCase):
    def setUp(self):
        _reset_schema()

    def test_0013_applies_from_an_empty_database(self):
        from agent.postgres_migrate import apply_migrations, applied_versions

        with _connect() as connection:
            self.assertIn(13, apply_migrations(connection))
            self.assertEqual(applied_versions(connection)[-1], 13)
            table = connection.execute(
                "SELECT to_regclass('public.manifest_evidence') AS name"
            ).fetchone()["name"]
            self.assertEqual(table, "manifest_evidence")

    def test_schema_version_12_upgrades_to_13_without_losing_state(self):
        from agent.postgres_migrate import apply_migrations, applied_versions

        with _connect() as connection:
            _apply_through(connection, 12)
            connection.execute("INSERT INTO organizations (organization_id) VALUES ('kept-org')")
            self.assertEqual(apply_migrations(connection), [13])
            self.assertEqual(applied_versions(connection)[-2:], [12, 13])
            kept = connection.execute(
                "SELECT organization_id FROM organizations WHERE organization_id='kept-org'"
            ).fetchone()
            self.assertIsNotNone(kept)


class _Queue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ManifestEvidencePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema()
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=4)
        cls.app = create_http_app(
            webhook_secret="manifest-test-secret", job_queue=_Queue(),
            max_body_bytes=1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool,
        )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.org = f"{ORG}-{self.suffix}"
        self.repo = f"{REPO}-{self.suffix}"
        with self.pool.acquire() as store:
            store.ensure_tenant(self.org, self.repo, ENV)

    def _token(self, scope):
        from agent.api.auth import generate_token, hash_secret

        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.create_service_token(
                token_id, hash_secret(secret), self.org, self.repo,
                environment=ENV, scope=scope,
            )
        return {"Authorization": f"Bearer {presented}",
                "Idempotency-Key": f"idem-{uuid.uuid4().hex}"}

    def _submit(self, store, manifest=MANIFEST, *, sha=HEAD_SHA, key="idem-1"):
        return store.submit_manifest_evidence(
            self.org, self.repo, commit_sha=sha, manifest=manifest,
            manifest_hash=_hash(manifest),
            payload_hash=_hash({"commit_sha": sha, "manifest": manifest}),
            idempotency_key=key,
        )

    def _waiting_review(self, store, review_id=None):
        review_id = review_id or f"review-{self.suffix}"
        store.upsert_pr_review(
            self.org, self.repo, ENV, review_id=review_id, pull_number=7,
            base_sha="1" * 40, head_sha=HEAD_SHA,
            enforcement_mode="enforce", lifecycle_state="WAITING_FOR_MANIFEST",
        )
        return review_id

    def test_ci_service_token_is_the_only_manifest_ingest_scope(self):
        body = {"commit_sha": HEAD_SHA, "manifest": MANIFEST}
        accepted = self.client.post("/api/manifest-evidence", json=body,
                                    headers=self._token("ci"))
        self.assertEqual(accepted.status_code, 202, accepted.text)
        for scope in ("collector", "operator_read"):
            response = self.client.post(
                "/api/manifest-evidence",
                json={"commit_sha": OTHER_SHA, "manifest": MANIFEST},
                headers=self._token(scope),
            )
            self.assertEqual(response.status_code, 403, (scope, response.text))

    def test_ci_token_can_be_issued_once_scoped_and_revoked(self):
        from agent.api.auth import (
            AuthenticationError,
            ServiceTokenAuthenticator,
            hash_secret,
        )
        from agent.collector.provisioning import (
            issue_ci_token,
            revoke_collector_token,
        )

        with self.pool.acquire() as store:
            token_id, presented = issue_ci_token(
                store, organization_id=self.org, repository_id=self.repo,
                description="customer Actions handoff",
            )
            scope = ServiceTokenAuthenticator(store).authenticate(presented)
            self.assertEqual(scope.scope, "ci")
            self.assertEqual(scope.organization_id, self.org)
            self.assertEqual(scope.repository_id, self.repo)
            self.assertIsNone(scope.environment)

            secret = presented.split(".", 1)[1]
            stored = store.get_service_token(token_id)
            self.assertEqual(stored["secret_hash"], hash_secret(secret))
            self.assertNotIn(secret, json.dumps(stored, default=str))
            listed = json.dumps(store.list_service_tokens(
                organization_id=self.org, repository_id=self.repo), default=str)
            self.assertNotIn(secret, listed)
            self.assertNotIn(presented, listed)

            self.assertTrue(revoke_collector_token(store, token_id))
            with self.assertRaises(AuthenticationError):
                ServiceTokenAuthenticator(store).authenticate(presented)

    def test_manifest_evidence_is_inserted(self):
        with self.pool.acquire() as store:
            row, created = self._submit(store)
            self.assertTrue(created)
            self.assertEqual(row["commit_sha"], HEAD_SHA)
            self.assertEqual(row["manifest"], MANIFEST)

    def test_identical_replay_is_idempotent(self):
        with self.pool.acquire() as store:
            first, first_created = self._submit(store)
            second, second_created = self._submit(store)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first["evidence_id"], second["evidence_id"])

    def test_conflicting_replay_is_rejected(self):
        from agent.postgres_lifecycle_store import ManifestEvidenceConflict

        with self.pool.acquire() as store:
            self._submit(store)
            with self.assertRaises(ManifestEvidenceConflict):
                self._submit(store, CONFLICTING_MANIFEST)
            stored = store.get_manifest_evidence(self.org, self.repo, HEAD_SHA)
            self.assertEqual(stored["manifest"], MANIFEST)

    def test_manifest_lookup_is_tenant_isolated(self):
        other_org, other_repo = f"other-{self.org}", f"other-{self.repo}"
        with self.pool.acquire() as store:
            self._submit(store)
            store.ensure_tenant(other_org, other_repo, ENV)
            self.assertIsNone(store.get_manifest_evidence(other_org, other_repo, HEAD_SHA))
            store.submit_manifest_evidence(
                other_org, other_repo, commit_sha=HEAD_SHA,
                manifest=CONFLICTING_MANIFEST,
                manifest_hash=_hash(CONFLICTING_MANIFEST),
                payload_hash=_hash({"commit_sha": HEAD_SHA,
                                    "manifest": CONFLICTING_MANIFEST}),
                idempotency_key="same-key-other-tenant",
            )
            self.assertEqual(
                store.get_manifest_evidence(other_org, other_repo, HEAD_SHA)["manifest"],
                CONFLICTING_MANIFEST,
            )

    def test_tenant_deletion_removes_manifest_evidence(self):
        with self.pool.acquire() as store:
            self._submit(store)
            store.delete_tenant(self.org)
            self.assertIsNone(
                store.get_manifest_evidence(self.org, self.repo, HEAD_SHA))
            repository = store.connection.execute(
                "SELECT 1 FROM repositories WHERE organization_id=%s "
                "AND repository_id=%s", (self.org, self.repo),
            ).fetchone()
            self.assertIsNone(repository)

    def test_lookup_requires_the_exact_sha(self):
        with self.pool.acquire() as store:
            self._submit(store)
            self.assertIsNotNone(store.get_manifest_evidence(self.org, self.repo, HEAD_SHA))
            self.assertIsNone(store.get_manifest_evidence(self.org, self.repo, HEAD_SHA[:-1]))
            self.assertIsNone(store.get_manifest_evidence(self.org, self.repo, OTHER_SHA))

    def test_waiting_for_manifest_is_a_valid_lifecycle_state(self):
        with self.pool.acquire() as store:
            review_id = self._waiting_review(store)
            review = store.get_review(self.org, self.repo, review_id)
            self.assertEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
            self.assertIsNone(review["decision"])

    def test_evidence_arrival_enqueues_resume(self):
        with self.pool.acquire() as store:
            review_id = self._waiting_review(store)
            self._submit(store, sha=BASE_SHA, key="base-before-head")
            self._submit(store)
            jobs = store.connection.execute(
                "SELECT * FROM outbox_events WHERE organization_id=%s "
                "AND repository_id=%s AND event_type='review.manifest_resume_requested'",
                (self.org, self.repo),
            ).fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["subject_id"], review_id)
            self.assertEqual(jobs[0]["payload"]["commit_sha"], HEAD_SHA)

    def test_identical_replay_does_not_duplicate_outbox_work(self):
        with self.pool.acquire() as store:
            self._waiting_review(store)
            self._submit(store, sha=BASE_SHA, key="base-before-replay")
            self._submit(store)
            self._submit(store)
            count = store.connection.execute(
                "SELECT count(*) AS n FROM outbox_events WHERE organization_id=%s "
                "AND repository_id=%s AND event_type='review.manifest_resume_requested'",
                (self.org, self.repo),
            ).fetchone()["n"]
            self.assertEqual(count, 1)

    def test_concurrent_identical_submissions_collapse_to_one_row(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def submit():
            from agent.postgres_lifecycle_store import PostgresLifecycleStore
            store = PostgresLifecycleStore(DSN)
            try:
                barrier.wait()
                results.append(self._submit(store))
            except Exception as exc:  # retained for an assertion in the caller
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        self.assertEqual(sorted(created for _, created in results), [False, True])
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT count(*) AS n FROM manifest_evidence WHERE organization_id=%s "
                "AND repository_id=%s AND commit_sha=%s",
                (self.org, self.repo, HEAD_SHA),
            ).fetchone()["n"]
            self.assertEqual(count, 1)

    def test_concurrent_conflicting_submissions_accept_exactly_one(self):
        from agent.postgres_lifecycle_store import (
            ManifestEvidenceConflict,
            PostgresLifecycleStore,
        )

        barrier = threading.Barrier(2)
        accepted, conflicts, errors = [], [], []

        def submit(manifest, key):
            store = PostgresLifecycleStore(DSN)
            try:
                barrier.wait()
                accepted.append(self._submit(store, manifest, key=key))
            except ManifestEvidenceConflict as exc:
                conflicts.append(exc)
            except Exception as exc:
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=submit, args=(MANIFEST, "concurrent-a")),
            threading.Thread(target=submit,
                             args=(CONFLICTING_MANIFEST, "concurrent-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(conflicts), 1)

    def test_concurrent_base_and_head_arrivals_enqueue_one_resume(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with self.pool.acquire() as store:
            self._waiting_review(store)
        barrier = threading.Barrier(2)
        errors = []

        def submit(sha, key):
            store = PostgresLifecycleStore(DSN)
            try:
                barrier.wait()
                self._submit(store, sha=sha, key=key)
            except Exception as exc:
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=submit, args=(BASE_SHA, "pair-base")),
            threading.Thread(target=submit, args=(HEAD_SHA, "pair-head")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        with self.pool.acquire() as store:
            count = store.connection.execute(
                "SELECT count(*) AS n FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s "
                "AND event_type='review.manifest_resume_requested'",
                (self.org, self.repo),
            ).fetchone()["n"]
        self.assertEqual(count, 1)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class ManifestWebhookResumePostgresTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from agent.api.pool import StorePool
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage
        from agent.metadata_evidence.service import ReviewLifecycleService
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from test_served_webhook_metadata_lifecycle import _FakeGitHubClient

        _reset_schema()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
        self.addCleanup(self.pool.close)
        self.lifecycle = ReviewLifecycleService(self.pool, environment="production")
        self.runner = PullRequestReviewRunner(
            storage=RepositoryStorage(self.tmp.name), lifecycle=self.lifecycle)
        self.github = _FakeGitHubClient()
        self.github.manifests.pop("2" * 40)

    def _run(self):
        from agent.github_app.webhooks import parse_webhook
        from test_served_webhook_metadata_lifecycle import _event

        delivery = f"manifest-delivery-{uuid.uuid4().hex[:8]}"
        event = parse_webhook(
            event_name="pull_request", delivery_id=delivery,
            body=json.dumps(_event(delivery)).encode(),
        )
        return self.runner.run(event, self.github, expected_app_id=4456468)

    def _submit(self, sha, manifest, key):
        from agent.metadata_evidence.collection_plan import manifest_hash

        with self.pool.acquire() as store:
            return store.submit_manifest_evidence(
                "AcmeOrg", "analytics", commit_sha=sha,
                manifest=manifest, manifest_hash=manifest_hash(manifest),
                payload_hash=_hash({"commit_sha": sha, "manifest": manifest}),
                idempotency_key=key,
            )

    def _resume_job_count(self):
        with self.pool.acquire() as store:
            return store.connection.execute(
                "SELECT count(*) AS n FROM outbox_events "
                "WHERE organization_id='AcmeOrg' AND repository_id='analytics' "
                "AND event_type='review.manifest_resume_requested'"
            ).fetchone()["n"]

    def _publisher(self):
        github = self.github

        class Publisher:
            def publish_comment(self, *, pull_number, body, comment_id=None):
                if comment_id:
                    return github.update_issue_comment(
                        "AcmeOrg", "analytics", int(comment_id), body)
                return github.create_issue_comment(
                    "AcmeOrg", "analytics", pull_number, body)

            def publish_check(self, *, head_sha, payload, check_run_id=None):
                if check_run_id:
                    return github.update_check_run(
                        "AcmeOrg", "analytics", int(check_run_id), payload)
                return github.create_check_run("AcmeOrg", "analytics", payload)

            def publish_slack(self, **kwargs):
                return {"state": "disabled",
                        "publication_id": kwargs["publication_id"]}

        return Publisher()

    def _drain_resume(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from agent.worker.lifecycle_worker import LifecycleWorker, configure_publisher

        configure_publisher(lambda **scope: self._publisher())
        self.addCleanup(configure_publisher, None)
        worker = LifecycleWorker(lambda: PostgresLifecycleStore(DSN),
                                 identity="manifest-ordering-test")
        store = worker.store_factory()
        self.addCleanup(store.close)
        self.assertEqual(worker.process_once(store), 1)
        self.assertEqual(worker.process_once(store), 1)
        self.assertEqual(worker.process_once(store), 0)

    def test_webhook_before_ci_persists_and_publishes_waiting(self):
        response = self._run()
        self.assertEqual(response["status"], "waiting_for_manifest")
        self.assertEqual(response["lifecycle_state"], "WAITING_FOR_MANIFEST")
        with self.pool.acquire() as store:
            review = store.get_review("AcmeOrg", "analytics", response["review_id"])
            base_evidence = store.get_manifest_evidence(
                "AcmeOrg", "analytics", "1" * 40)
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
        self.assertIsNone(review["decision"])
        self.assertNotIn("base_manifest", review["payload"]["manifest_wait"])
        self.assertIsNotNone(base_evidence)
        self.assertEqual([kind for kind, _ in self.github.comment_calls], ["create"])
        self.assertEqual([kind for kind, _ in self.github.check_calls], ["create"])

    def test_committed_head_does_not_bypass_a_missing_base(self):
        from test_served_webhook_metadata_lifecycle import HEAD_MANIFEST

        self.github.manifests[HEAD_SHA] = HEAD_MANIFEST
        self.github.manifests.pop(BASE_SHA)
        response = self._run()

        self.assertEqual(response["lifecycle_state"], "WAITING_FOR_MANIFEST")
        with self.pool.acquire() as store:
            review = store.get_review("AcmeOrg", "analytics", response["review_id"])
            committed_head = store.get_manifest_evidence(
                "AcmeOrg", "analytics", HEAD_SHA)
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
        self.assertIsNone(review["decision"])
        self.assertEqual(committed_head["manifest"], HEAD_MANIFEST)
        self.assertEqual(self._resume_job_count(), 0)

    def test_resume_updates_one_existing_github_publication(self):
        from agent.metadata_evidence.collection_plan import manifest_hash
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        from agent.worker.lifecycle_worker import (
            LifecycleWorker,
            configure_publisher,
        )
        from test_served_webhook_metadata_lifecycle import HEAD_MANIFEST

        waiting = self._run()

        class Publisher:
            def __init__(publisher_self, github):
                publisher_self.github = github

            def publish_comment(publisher_self, *, pull_number, body, comment_id=None):
                if comment_id:
                    return publisher_self.github.update_issue_comment(
                        "AcmeOrg", "analytics", int(comment_id), body)
                return publisher_self.github.create_issue_comment(
                    "AcmeOrg", "analytics", pull_number, body)

            def publish_check(publisher_self, *, head_sha, payload, check_run_id=None):
                if check_run_id:
                    return publisher_self.github.update_check_run(
                        "AcmeOrg", "analytics", int(check_run_id), payload)
                return publisher_self.github.create_check_run(
                    "AcmeOrg", "analytics", payload)

            def publish_slack(publisher_self, **kwargs):
                return {"state": "disabled", "publication_id": kwargs["publication_id"]}

        with self.pool.acquire() as store:
            store.submit_manifest_evidence(
                "AcmeOrg", "analytics", commit_sha="2" * 40,
                manifest=HEAD_MANIFEST, manifest_hash=manifest_hash(HEAD_MANIFEST),
                payload_hash=_hash({"commit_sha": "2" * 40,
                                    "manifest": HEAD_MANIFEST}),
                idempotency_key="ci-resume-once",
            )

        configure_publisher(lambda **scope: Publisher(self.github))
        self.addCleanup(configure_publisher, None)
        worker = LifecycleWorker(lambda: PostgresLifecycleStore(DSN),
                                 identity="manifest-resume-test")
        store = worker.store_factory()
        self.addCleanup(store.close)
        self.assertEqual(worker.process_once(store), 1)
        self.assertEqual(worker.process_once(store), 1)
        self.assertEqual(worker.process_once(store), 0)

        self.assertEqual([kind for kind, _ in self.github.comment_calls],
                         ["create", "update"])
        self.assertEqual([kind for kind, _ in self.github.check_calls],
                         ["create", "update"])
        with self.pool.acquire() as read_store:
            review = read_store.get_review(
                "AcmeOrg", "analytics", waiting["review_id"])
            jobs = read_store.connection.execute(
                "SELECT event_type, state FROM outbox_events "
                "WHERE organization_id='AcmeOrg' AND repository_id='analytics' "
                "ORDER BY created_at"
            ).fetchall()
        self.assertNotEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
        self.assertEqual([job["state"] for job in jobs], ["COMPLETED", "COMPLETED"])

    def test_head_first_waits_for_base_then_resumes_once_with_exact_pair(self):
        from agent.deployment_review_service import review_manifest_change
        from test_served_webhook_metadata_lifecycle import (
            BASE_MANIFEST,
            HEAD_MANIFEST,
        )

        self.github.manifests.pop(BASE_SHA)
        waiting = self._run()

        self._submit(HEAD_SHA, HEAD_MANIFEST, "head-first")
        self._submit(HEAD_SHA, HEAD_MANIFEST, "head-first")
        with self.pool.acquire() as store:
            review = store.get_review("AcmeOrg", "analytics", waiting["review_id"])
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
        self.assertIsNone(review["decision"])
        self.assertEqual(self._resume_job_count(), 0)
        self.assertEqual([kind for kind, _ in self.github.comment_calls], ["create"])
        self.assertEqual([kind for kind, _ in self.github.check_calls], ["create"])

        self._submit(BASE_SHA, BASE_MANIFEST, "base-second")
        self._submit(BASE_SHA, BASE_MANIFEST, "base-second")
        self.assertEqual(self._resume_job_count(), 1)
        with patch(
            "agent.metadata_evidence.manifest_handoff.review_manifest_change",
            wraps=review_manifest_change,
        ) as reviewer:
            self._drain_resume()
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(reviewer.call_args.kwargs["previous_manifest"], BASE_MANIFEST)
        self.assertEqual(reviewer.call_args.kwargs["manifest"], HEAD_MANIFEST)
        self.assertEqual([kind for kind, _ in self.github.comment_calls],
                         ["create", "update"])
        self.assertEqual([kind for kind, _ in self.github.check_calls],
                         ["create", "update"])

    def test_base_first_waits_for_head_then_resumes_once_with_exact_pair(self):
        from agent.deployment_review_service import review_manifest_change
        from test_served_webhook_metadata_lifecycle import (
            BASE_MANIFEST,
            HEAD_MANIFEST,
        )

        self.github.manifests.pop(BASE_SHA)
        waiting = self._run()

        self._submit(BASE_SHA, BASE_MANIFEST, "base-first")
        self._submit(BASE_SHA, BASE_MANIFEST, "base-first")
        with self.pool.acquire() as store:
            review = store.get_review("AcmeOrg", "analytics", waiting["review_id"])
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_MANIFEST")
        self.assertIsNone(review["decision"])
        self.assertEqual(self._resume_job_count(), 0)

        self._submit(HEAD_SHA, HEAD_MANIFEST, "head-second")
        self._submit(HEAD_SHA, HEAD_MANIFEST, "head-second")
        self.assertEqual(self._resume_job_count(), 1)
        with patch(
            "agent.metadata_evidence.manifest_handoff.review_manifest_change",
            wraps=review_manifest_change,
        ) as reviewer:
            self._drain_resume()
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(reviewer.call_args.kwargs["previous_manifest"], BASE_MANIFEST)
        self.assertEqual(reviewer.call_args.kwargs["manifest"], HEAD_MANIFEST)
        self.assertEqual([kind for kind, _ in self.github.comment_calls],
                         ["create", "update"])
        self.assertEqual([kind for kind, _ in self.github.check_calls],
                         ["create", "update"])


if __name__ == "__main__":
    unittest.main()
