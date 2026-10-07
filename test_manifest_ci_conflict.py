"""A CI manifest resubmission must be accepted or fail the review -- never hang.

What happened on relium-saas-demo PR #1: base commit 0f34be6 had manifest
evidence stored under dbt 1.12.4. The repository pins ``dbt-duckdb>=1.9,<2``,
so a later CI run compiled the same commit under dbt 1.12.5. Volatile stamps
aside, the two documents differed in ``metadata.dbt_version`` alone, the API
answered 409, the CI step died, and the review sat in WAITING_FOR_MANIFEST
with nothing left that could move it. Two other reviews on that base had been
stuck the same way since 18 Sep.

Two guarantees, both through the real served route on real PostgreSQL:

* a resubmission that means the same thing -- run stamps or the dbt version
  aside -- is accepted and wakes the review;
* a genuine conflict is still a 409, but every review waiting on the commit
  is parked in MANIFEST_CONFLICT and told what differs, and an agreeing
  resubmission later picks it back up.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest

from test_manifest_evidence_lifecycle_idempotency import (
    _StubQueue,
    _key,
    _manifest,
    _recompiled,
    _sha,
)

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "ci-conflict-org"
REPO = "ci-conflict-repo"
ENV = "production"

_HARNESS = {}


def setUpModule():
    import psycopg
    from starlette.testclient import TestClient

    from agent.api.pool import StorePool
    from agent.collector.provisioning import issue_ci_token
    from agent.github_app.http_app import create_http_app
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    if not DSN:
        return
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")

    pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
    app = create_http_app(
        webhook_secret="ci-conflict-secret", job_queue=_StubQueue(),
        max_body_bytes=8 * 1024 * 1024, shutdown_timeout_seconds=1.0,
        clock=lambda: 0.0, store_pool=pool)
    http = TestClient(app)
    http.__enter__()
    with pool.acquire() as store:
        store.ensure_tenant(ORG, REPO, ENV)
        _, token = issue_ci_token(store, organization_id=ORG,
                                  repository_id=REPO)
    _HARNESS.update(pool=pool, http=http, token=token)


def tearDownModule():
    if not _HARNESS:
        return
    _HARNESS["http"].__exit__(None, None, None)
    _HARNESS["pool"].close()


def _compiled_by(dbt_version, manifest=None, *, run="run-1"):
    """``manifest`` as compiled by one dbt version in one CI run."""
    document = json.loads(json.dumps(manifest or _manifest()))
    document["metadata"]["dbt_version"] = dbt_version
    document["metadata"]["invocation_id"] = run
    return document


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class _Base(unittest.TestCase):
    def setUp(self):
        self.pool = _HARNESS["pool"]
        self.http = _HARNESS["http"]
        self.base = _sha("c")
        self.head = _sha("d")

    def _submit(self, sha, manifest, *, key=None):
        return self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {_HARNESS['token']}",
                     "Idempotency-Key": key or _key(sha)},
            json={"commit_sha": sha, "manifest": manifest})

    def _open_pull_request(self, pull_number):
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        with self.pool.acquire() as store:
            return begin_manifest_wait(
                store, organization_id=ORG, repository_id=REPO,
                environment=ENV, pull_number=pull_number, base_sha=self.base,
                head_sha=self.head, base_manifest=None, head_manifest=None,
                changed_files=["models/fct_revenue.sql"],
                enforcement_mode="shadow",
                delivery_id=f"delivery-{pull_number}")

    def _review(self, review_id):
        with self.pool.acquire() as store:
            return store.get_review(ORG, REPO, review_id)

    def _jobs(self, review_id, event_type):
        with self.pool.acquire() as store:
            return store.connection.execute(
                "SELECT count(*) AS n FROM outbox_events WHERE "
                "organization_id=%s AND repository_id=%s AND subject_id=%s "
                "AND event_type=%s",
                (ORG, REPO, review_id, event_type),
            ).fetchone()["n"]

    def _resume(self, review_id):
        from agent.metadata_evidence.manifest_handoff import resume_manifest_review

        with self.pool.acquire() as store:
            return resume_manifest_review(
                store, organization_id=ORG, repository_id=REPO,
                environment=ENV, review_id=review_id, commit_sha=self.head)


class SameCommitResubmissionTests(_Base):
    """Semantically the same: accepted, and the waiting review moves on."""

    def test_a_dbt_patch_upgrade_with_new_run_stamps_is_accepted(self):
        stored = _compiled_by("1.12.4")
        self.assertEqual(self._submit(self.base, stored).status_code, 202)

        response = self._submit(
            self.base, _compiled_by("1.12.5", _recompiled(stored), run="run-2"))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["created"], False)
        with self.pool.acquire() as store:
            kept = store.get_manifest_evidence(ORG, REPO, self.base)
        # Reused, not replaced: the row earlier decisions read is untouched.
        self.assertEqual(kept["manifest"]["metadata"]["dbt_version"], "1.12.4")

    def test_a_pre_semantic_row_like_the_demo_base_is_accepted(self):
        """Production's row for 0f34be6: canonicalization_version 1, no hash."""
        stored = _compiled_by("1.12.4")
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (ORG, REPO, "manifest-legacy-" + self.base[:12], self.base,
                 "0" * 64, store._Jsonb(stored), _key(self.base), "1" * 64))

        response = self._submit(self.base, _compiled_by("1.12.5", run="run-2"))
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_row_hashed_under_the_previous_recipe_is_accepted(self):
        """A v2 row's stored hash includes dbt_version; it is re-derived."""
        from agent.metadata_evidence.manifest_identity import canonical_manifest

        stored = _compiled_by("1.12.4")
        v2_hash = hashlib.sha256(json.dumps(
            canonical_manifest(stored), sort_keys=True, separators=(",", ":"),
            default=str).encode()).hexdigest()
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 2)",
                (ORG, REPO, "manifest-v2-" + self.base[:12], self.base,
                 "0" * 64, store._Jsonb(stored), _key(self.base), "1" * 64,
                 v2_hash))

        response = self._submit(self.base, _compiled_by("1.12.5", run="run-2"))
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_waiting_review_resumes_after_a_dbt_patch_upgrade(self):
        """The demo's path end to end: base known from an earlier PR, a new PR
        opens, CI recompiles both commits under a newer dbt patch."""
        self.assertEqual(
            self._submit(self.base, _compiled_by("1.12.4")).status_code, 202)
        review = self._open_pull_request(601)

        self.assertEqual(self._submit(
            self.base, _compiled_by("1.12.5", run="run-2")).status_code, 200)
        self.assertEqual(self._submit(
            self.head, _compiled_by("1.12.5", run="run-2")).status_code, 202)

        self.assertEqual(self._review(review.review_id)["lifecycle_state"],
                         "WAITING_FOR_MANIFEST")
        self.assertEqual(
            self._jobs(review.review_id, "review.manifest_resume_requested"), 1)
        self.assertEqual(self._resume(review.review_id)["status"], "resumed")


class GenuineConflictTests(_Base):
    """Really different: still a 409, but the review is told, not stranded."""

    PUBLISH = "review.manifest_conflict_publish_requested"

    def _stored_then_conflicting(self):
        self.assertEqual(self._submit(
            self.base, _manifest(sql="select 1 as revenue")).status_code, 202)
        return self._submit(self.base, _manifest(sql="select 2 as revenue"))

    def test_the_409_names_what_differs_and_what_to_do(self):
        response = self._stored_then_conflicting()

        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertTrue(detail.startswith(
            "commit SHA already has different manifest evidence"))
        self.assertIn("differs at: nodes.model.relium.fct_revenue.compiled_code",
                      detail)
        self.assertIn("pin dbt and package versions", detail)
        # A CI log can be public: paths, never the SQL itself.
        self.assertNotIn("select 2", detail)

    def test_the_waiting_review_is_parked_not_left_waiting(self):
        review = self._open_pull_request(602)

        response = self._stored_then_conflicting()

        self.assertEqual(response.status_code, 409)
        self.assertIn("1 waiting review(s) marked MANIFEST_CONFLICT",
                      response.json()["detail"])
        parked = self._review(review.review_id)
        self.assertEqual(parked["lifecycle_state"], "MANIFEST_CONFLICT")
        self.assertIsNone(parked["decision"])
        conflicts = parked["payload"]["manifest_wait"]["evidence_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["side"], "base")
        self.assertEqual(conflicts[0]["source"], "ci")
        self.assertEqual(conflicts[0]["commit_sha"], self.base)
        self.assertIn("nodes.model.relium.fct_revenue.raw_code",
                      conflicts[0]["differences"])
        with self.pool.acquire() as store:
            states = [row["to_state"] for row in store.review_transitions(
                ORG, REPO, review.review_id)]
        self.assertEqual(states[-1], "MANIFEST_CONFLICT")
        self.assertEqual(self._jobs(review.review_id, self.PUBLISH), 1)
        self.assertEqual(
            self._jobs(review.review_id, "review.manifest_resume_requested"), 0)

    def test_a_head_side_conflict_is_parked_on_the_head(self):
        review = self._open_pull_request(603)
        self.assertEqual(self._submit(
            self.head, _manifest(sql="select 1 as revenue")).status_code, 202)

        response = self._submit(self.head, _manifest(sql="select 3 as revenue"))

        self.assertEqual(response.status_code, 409)
        conflicts = self._review(review.review_id)[
            "payload"]["manifest_wait"]["evidence_conflicts"]
        self.assertEqual([c["side"] for c in conflicts], ["head"])

    def test_a_repeated_rejection_publishes_once(self):
        review = self._open_pull_request(604)
        self._stored_then_conflicting()

        again = self._submit(self.base, _manifest(sql="select 2 as revenue"))

        self.assertEqual(again.status_code, 409)
        self.assertEqual(self._jobs(review.review_id, self.PUBLISH), 1)
        self.assertEqual(self._review(review.review_id)["lifecycle_state"],
                         "MANIFEST_CONFLICT")

    def test_an_agreeing_resubmission_releases_and_resumes_the_review(self):
        """Not a dead end: CI pinned back, re-run, review finishes."""
        review = self._open_pull_request(605)
        self._stored_then_conflicting()

        agreeing = self._submit(
            self.base, _recompiled(_manifest(sql="select 1 as revenue")))
        self.assertEqual(agreeing.status_code, 200, agreeing.text)
        self.assertEqual(self._review(review.review_id)["lifecycle_state"],
                         "WAITING_FOR_MANIFEST")
        self.assertEqual(self._review(review.review_id)[
            "payload"]["manifest_wait"]["evidence_conflicts"], [])

        self.assertEqual(self._submit(self.head, _manifest()).status_code, 202)
        self.assertEqual(
            self._jobs(review.review_id, "review.manifest_resume_requested"), 1)
        outcome = self._resume(review.review_id)
        self.assertEqual(outcome["status"], "resumed")
        self.assertNotEqual(outcome["lifecycle_state"], "WAITING_FOR_MANIFEST")

    def test_a_rejection_before_the_webhook_opens_the_review_in_conflict(self):
        """CI can finish before the webhook is processed. The review must not
        then wait for a submission that was already refused."""
        from agent.metadata_evidence.waiting_publication import (
            render_manifest_conflict_result,
        )

        self.assertEqual(self._stored_then_conflicting().status_code, 409)

        outcome = self._open_pull_request(606)

        self.assertEqual(outcome.lifecycle_state, "MANIFEST_CONFLICT")
        self.assertFalse(outcome.waiting)
        self.assertEqual(outcome.evidence["base_manifest"], "CONFLICT")
        markdown = render_manifest_conflict_result(
            outcome, base_sha=self.base, head_sha=self.head,
        )["rendered"]["markdown"]
        self.assertIn("Where they differ", markdown)
        self.assertIn("`nodes.model.relium.fct_revenue.raw_code`", markdown)
        self.assertIn("pin your CI's dbt and package versions", markdown)

    def test_a_cleared_rejection_does_not_park_a_later_review(self):
        self._stored_then_conflicting()
        self.assertEqual(self._submit(
            self.base, _recompiled(_manifest(sql="select 1 as revenue"))
        ).status_code, 200)

        outcome = self._open_pull_request(607)

        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_MANIFEST")

    def test_a_reused_key_does_not_park_reviews(self):
        """A key conflict is a client defect about the key, not this commit's
        content; it must not strand reviews on the commit it was sent for."""
        review = self._open_pull_request(608)
        other = _sha("e")
        self.assertEqual(self._submit(other, _manifest()).status_code, 202)

        response = self._submit(self.base, _manifest(), key=_key(other))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"],
                         "idempotency key already used for a different commit SHA")
        self.assertEqual(self._review(review.review_id)["lifecycle_state"],
                         "WAITING_FOR_MANIFEST")
        self.assertEqual(self._jobs(review.review_id, self.PUBLISH), 0)


if __name__ == "__main__":
    unittest.main()
