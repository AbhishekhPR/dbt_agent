"""Manifest evidence must be reusable for the life of a commit.

The regression these guard is a product-lifecycle failure, not a hash bug: a
customer opening a second pull request from a base commit Relium had already
analysed got HTTP 409 on the base manifest, the CI step died before head was
ever submitted, and the review sat in WAITING_FOR_MANIFEST forever. The commit
SHA was permanently unusable, because the idempotency key is a pure function
of repository and SHA and `manifest_evidence` is immutable by trigger.

Real PostgreSQL and the real served route, because the outcome is decided by
database uniqueness and reconciled inside one transaction.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "lifecycle-org"
REPO = "lifecycle-repo"
ENV = "production"
OTHER_ORG = "lifecycle-other-org"
WAITING_ORG = "lifecycle-waiting-org"

# The repository id the workflow interpolates into the key. The value does not
# matter; that it is STABLE across runs and across pull requests does.
REPOSITORY_ID = "1355704784"

FIXTURES = Path("tests/fixtures/manifests")

_SEQUENCE = iter(range(1, 10_000))


def _sha(marker="a"):
    """A fresh SHA. `manifest_evidence` is immutable by database trigger, so a
    test cannot clean up after itself -- and should not want to."""
    return f"{next(_SEQUENCE):040x}".replace("x", marker)


def _key(sha):
    """Exactly the key agent/ci_workflow/relium-pr-review.yml sends."""
    return f"github-actions:{REPOSITORY_ID}:{sha}"


def _manifest(*, generated_at="2026-09-01T10:00:00Z", invocation_id="run-1",
              created_at=1756720000.0, sql="select 1 as revenue"):
    """A dbt manifest shaped like the real thing.

    `generated_at`, `invocation_id` and the per-entry `created_at` are what dbt
    stamps afresh on every compile. Everything else is identical between runs,
    which is the whole point: the SOURCE has not changed, only the run has.
    """
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "project_name": "relium",
            "generated_at": generated_at,
            "invocation_id": invocation_id,
            "user_id": "8b1d-anonymous",
        },
        "nodes": {
            "model.relium.fct_revenue": {
                "unique_id": "model.relium.fct_revenue",
                "resource_type": "model",
                "name": "fct_revenue",
                "database": "analytics",
                "schema": "public",
                "alias": "fct_revenue",
                "path": "models/fct_revenue.sql",
                "original_file_path": "models/fct_revenue.sql",
                "raw_code": sql,
                "compiled_code": sql,
                "depends_on": {"nodes": []},
                "columns": {"revenue": {"name": "revenue"}},
                "config": {"materialized": "table"},
                "created_at": created_at,
            },
        },
        "macros": {
            "macro.relium.cents_to_dollars": {
                "unique_id": "macro.relium.cents_to_dollars",
                "name": "cents_to_dollars",
                "macro_sql": "cents / 100",
                "created_at": created_at,
            },
        },
        "sources": {},
        "exposures": {},
        "child_map": {},
        "parent_map": {},
    }


def _recompiled(manifest):
    """The same project, compiled again. Only run stamps move."""
    return _manifest(generated_at="2026-09-04T18:42:11Z",
                     invocation_id="run-2",
                     created_at=1757010131.0,
                     sql=manifest["nodes"]["model.relium.fct_revenue"]["raw_code"])


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


#: One schema reset, one pool and one served application for the whole module.
#: Each class works in its own organisation instead of dropping the schema:
#: two live pools plus a DROP SCHEMA is a deadlock, and evidence rows are
#: append-only anyway, so isolation comes from fresh SHAs and fresh tenants.
_HARNESS = {}


def setUpModule():
    import psycopg
    from starlette.testclient import TestClient

    from agent.api.pool import StorePool
    from agent.github_app.http_app import create_http_app
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    if not DSN:
        return
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")

    pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
    app = create_http_app(
        webhook_secret="lifecycle-secret", job_queue=_StubQueue(),
        max_body_bytes=8 * 1024 * 1024, shutdown_timeout_seconds=1.0,
        clock=lambda: 0.0, store_pool=pool)
    http = TestClient(app)
    http.__enter__()
    _HARNESS.update(pool=pool, http=http)


def tearDownModule():
    if not _HARNESS:
        return
    _HARNESS["http"].__exit__(None, None, None)
    _HARNESS["pool"].close()


def _tenant(name):
    """A fresh organisation, so one class cannot see another's evidence."""
    from agent.collector.provisioning import issue_ci_token

    with _HARNESS["pool"].acquire() as store:
        store.ensure_tenant(name, REPO, ENV)
        _, token = issue_ci_token(store, organization_id=name,
                                  repository_id=REPO)
    return token


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; idempotency is a database property")
class ManifestEvidenceIdempotencyTests(unittest.TestCase):
    """The /api/manifest-evidence contract, over the served route."""

    @classmethod
    def setUpClass(cls):
        cls.pool = _HARNESS["pool"]
        cls.http = _HARNESS["http"]
        cls.token = _tenant(ORG)
        cls.other_token = _tenant(OTHER_ORG)

    def setUp(self):
        self.sha = _sha()

    def _submit(self, sha, manifest, *, key=None, token=None):
        return self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {token or self.token}",
                     "Idempotency-Key": key or _key(sha)},
            json={"commit_sha": sha, "manifest": manifest})

    # -- 1. a first submission ---------------------------------------------

    def test_a_first_submission_is_created(self):
        response = self._submit(self.sha, _manifest())
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertIs(body["created"], True)
        self.assertEqual(len(body["semantic_manifest_hash"]), 64)

    # -- 2. an exact retry --------------------------------------------------

    def test_a_byte_identical_retry_reuses_the_evidence(self):
        manifest = _manifest()
        first = self._submit(self.sha, manifest)
        second = self._submit(self.sha, copy.deepcopy(manifest))

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIs(second.json()["created"], False)
        self.assertEqual(first.json()["evidence_id"],
                         second.json()["evidence_id"])

    # -- 3. the same project, compiled again --------------------------------

    def test_a_recompile_of_the_same_commit_reuses_the_evidence(self):
        """THE REGRESSION. Same commit, same source, later dbt run.

        Before the fix this was HTTP 409 'idempotency key already used with
        different manifest evidence', and no key the workflow could produce
        would ever avoid it.
        """
        first = self._submit(self.sha, _manifest())
        second = self._submit(self.sha, _recompiled(_manifest()))

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIs(second.json()["created"], False)
        self.assertEqual(first.json()["evidence_id"],
                         second.json()["evidence_id"])
        self.assertEqual(first.json()["semantic_manifest_hash"],
                         second.json()["semantic_manifest_hash"])

    def test_two_real_compiles_of_one_commit_reuse_the_evidence(self):
        """The same claim against two manifests dbt actually produced."""
        a = json.loads((FIXTURES / "base-compile-a.json").read_text(encoding="utf-8"))
        b = json.loads((FIXTURES / "base-compile-b.json").read_text(encoding="utf-8"))
        self.assertNotEqual(a, b)

        first = self._submit(self.sha, a)
        second = self._submit(self.sha, b)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["evidence_id"],
                         second.json()["evidence_id"])

    def test_the_stored_manifest_is_not_overwritten_by_a_reuse(self):
        """Reuse returns the existing row. It does not adopt the new document.

        `manifest_evidence` is the record earlier decisions were computed
        from; a reuse must be a read, never a correction.
        """
        original = _manifest()
        self._submit(self.sha, original)
        self._submit(self.sha, _recompiled(original))

        with self.pool.acquire() as store:
            stored = store.get_manifest_evidence(ORG, REPO, self.sha)
        self.assertEqual(stored["manifest"], original)

    # -- 4. evidence left behind by an older deploy -------------------------

    def test_a_legacy_row_does_not_permanently_block_its_commit(self):
        """A row written before canonicalisation existed.

        No semantic hash, recipe version 1, and an un-normalised manifest
        carrying every per-run stamp -- under the very key the workflow will
        send again. This is the production state that made the SHA unusable.
        """
        legacy = _manifest()
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (ORG, REPO, "manifest-legacy-" + self.sha[:8], self.sha,
                 "0" * 64, store._Jsonb(legacy), _key(self.sha), "1" * 64),
            )

        response = self._submit(self.sha, _recompiled(legacy))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["created"], False)
        self.assertEqual(response.json()["evidence_id"],
                         "manifest-legacy-" + self.sha[:8])

    def test_a_legacy_row_is_re_derived_rather_than_rewritten(self):
        """The un-poisoning must not mutate an append-only table."""
        legacy = _manifest()
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (ORG, REPO, "manifest-frozen-" + self.sha[:8], self.sha,
                 "0" * 64, store._Jsonb(legacy), _key(self.sha), "1" * 64),
            )

        self._submit(self.sha, _recompiled(legacy))

        with self.pool.acquire() as store:
            row = store.connection.execute(
                "SELECT semantic_manifest_hash, canonicalization_version, "
                "manifest_hash FROM manifest_evidence WHERE organization_id=%s "
                "AND repository_id=%s AND commit_sha=%s",
                (ORG, REPO, self.sha),
            ).fetchone()
        self.assertIsNone(row["semantic_manifest_hash"])
        self.assertEqual(row["canonicalization_version"], 1)
        self.assertEqual(row["manifest_hash"], "0" * 64)

    def test_a_legacy_row_with_different_content_is_still_a_conflict(self):
        """Re-derivation is not amnesty. A legacy row whose manifest really
        says something else still refuses the submission."""
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (ORG, REPO, "manifest-other-" + self.sha[:8], self.sha,
                 "0" * 64, store._Jsonb(_manifest(sql="select 1 as other")),
                 _key(self.sha), "1" * 64),
            )

        response = self._submit(self.sha, _manifest(sql="select 1 as revenue"))

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "commit SHA already has different manifest evidence")

    # -- 5. a genuine difference -------------------------------------------

    def test_a_semantically_different_manifest_is_still_rejected(self):
        self._submit(self.sha, _manifest(sql="select 1 as revenue"))
        response = self._submit(self.sha, _manifest(sql="select 2 as revenue"))

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["status"], "conflict")
        self.assertEqual(response.json()["detail"],
                         "commit SHA already has different manifest evidence")

    def test_an_added_model_is_still_rejected(self):
        self._submit(self.sha, _manifest())
        widened = _manifest()
        widened["nodes"]["model.relium.dim_customer"] = {
            "unique_id": "model.relium.dim_customer", "resource_type": "model",
            "name": "dim_customer", "raw_code": "select 1 as id"}
        response = self._submit(self.sha, widened)

        self.assertEqual(response.status_code, 409, response.text)

    def test_one_key_cannot_describe_two_commits(self):
        """The check the idempotency key does own, and the only one it owns."""
        other = _sha("b")
        self._submit(self.sha, _manifest(), key="shared-key-1")
        response = self._submit(other, _manifest(), key="shared-key-1")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "idempotency key already used for a different commit SHA")

    def test_one_key_cannot_describe_two_commits_even_with_equal_content(self):
        """Matching content must NOT soften the key rule.

        The second commit carries a byte-identical manifest, so every content
        comparison in the system says "same". The key still cannot be allowed
        to mean two commits: accepting it would leave one key standing for two
        subjects and make every later retry on that key ambiguous.
        """
        other = _sha("b")
        manifest = _manifest()
        self._submit(self.sha, manifest, key="shared-key-equal")
        response = self._submit(other, copy.deepcopy(manifest),
                                key="shared-key-equal")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "idempotency key already used for a different commit SHA")

    def test_one_key_cannot_describe_two_commits_when_the_second_is_known(self):
        """The ordering case.

        The second commit ALREADY has its own evidence, semantically equal to
        what is being submitted -- so the by-SHA path on its own would happily
        reuse it and return 200. The key is checked first, and refuses.
        """
        other = _sha("b")
        self._submit(self.sha, _manifest(), key="key-for-first")
        self._submit(other, _manifest(), key="key-for-second")

        response = self._submit(other, _recompiled(_manifest()),
                                key="key-for-first")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "idempotency key already used for a different commit SHA")

    def test_one_key_cannot_describe_two_commits_against_a_legacy_row(self):
        """Re-derivation of a legacy row must not smuggle the key rule out.

        The legacy row holds the key; a different commit tries to borrow it.
        """
        other = _sha("b")
        legacy = _manifest()
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (ORG, REPO, "manifest-legacy-key-" + self.sha[:8], self.sha,
                 "0" * 64, store._Jsonb(legacy), "legacy-shared-key", "1" * 64),
            )

        response = self._submit(other, _recompiled(legacy),
                                key="legacy-shared-key")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         "idempotency key already used for a different commit SHA")

    def test_the_key_rule_does_not_block_the_same_commit(self):
        """The rule is about two commits, not about reuse.

        Same key, same commit, volatile-only difference -- the case the whole
        fix exists for. It must still reuse.
        """
        self._submit(self.sha, _manifest(), key="key-same-commit")
        response = self._submit(self.sha, _recompiled(_manifest()),
                                key="key-same-commit")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["created"], False)

    # -- 6. a second pull request from a known base ------------------------

    def test_a_new_key_on_a_known_commit_reuses_the_evidence(self):
        """A second pull request from an already-analysed base.

        Its key differs -- the webhook path derives one per repository and
        commit, and an older deploy derived one per review. Either way the
        commit is the same commit, so the evidence is reused.
        """
        self._submit(self.sha, _manifest(), key="pr-101-base")
        response = self._submit(self.sha, _recompiled(_manifest()),
                                key="pr-207-base")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["created"], False)

    def test_the_whole_base_and_head_handoff_succeeds_on_a_second_pull_request(self):
        """End to end, in the order the workflow submits.

        Before the fix the base 409 killed the step and head was never sent.
        """
        base, first_head, second_head = self.sha, _sha("b"), _sha("c")
        for sha in (base, first_head):
            self.assertIn(self._submit(sha, _manifest()).status_code,
                          (200, 202))

        results = []
        for side, sha in (("base", base), ("head", second_head)):
            response = self._submit(sha, _recompiled(_manifest()))
            results.append((side, response.status_code))
            if response.status_code not in (200, 202):
                break

        self.assertEqual(results, [("base", 200), ("head", 202)])

    # -- ownership ---------------------------------------------------------

    def test_another_tenant_does_not_reuse_or_see_this_tenant_evidence(self):
        """Reconciliation is scoped to the tenant, as the conflict checks were.

        The same SHA in a different organisation is a different subject: it
        gets its own row, and never the first organisation's identifier.
        """
        mine = self._submit(self.sha, _manifest())
        theirs = self._submit(self.sha, _manifest(sql="select 9 as revenue"),
                              token=self.other_token)

        self.assertEqual(mine.status_code, 202, mine.text)
        self.assertEqual(theirs.status_code, 202, theirs.text)
        self.assertNotEqual(mine.json()["evidence_id"],
                            theirs.json()["evidence_id"])

    def test_an_unknown_token_is_rejected_before_any_conflict_check(self):
        """A scope mismatch is 401/403, never 409."""
        response = self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": "Bearer rlm_deadbeef.wrong",
                     "Idempotency-Key": _key(self.sha)},
            json={"commit_sha": self.sha, "manifest": _manifest()})
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the lifecycle is a database property")
class WaitingReviewIsUnblockedTests(unittest.TestCase):
    """The product outcome: a review must leave WAITING_FOR_MANIFEST.

    Reusing evidence is only half the fix. A reused submission must still wake
    the review that is waiting on that commit, or the customer trades a 409 for
    a silent stall.
    """

    @classmethod
    def setUpClass(cls):
        cls.pool = _HARNESS["pool"]
        cls.http = _HARNESS["http"]
        cls.token = _tenant(WAITING_ORG)

    def setUp(self):
        self.base = _sha()

    def _submit(self, sha, manifest):
        return self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {self.token}",
                     "Idempotency-Key": _key(sha)},
            json={"commit_sha": sha, "manifest": manifest})

    def _open_pull_request(self, pull_number, head_sha):
        """The webhook half: a review waiting for manifests that CI will send."""
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        with self.pool.acquire() as store:
            return begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=pull_number, base_sha=self.base,
                head_sha=head_sha, base_manifest=None, head_manifest=None,
                changed_files=["models/fct_revenue.sql"],
                enforcement_mode="shadow", delivery_id=f"delivery-{pull_number}")

    def _resume(self, review_id, head_sha):
        from agent.metadata_evidence.manifest_handoff import resume_manifest_review

        with self.pool.acquire() as store:
            return resume_manifest_review(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, review_id=review_id, commit_sha=head_sha)

    def _resume_jobs(self, review_id):
        with self.pool.acquire() as store:
            return store.connection.execute(
                "SELECT count(*) AS n FROM outbox_events WHERE organization_id=%s "
                "AND repository_id=%s AND subject_id=%s "
                "AND event_type='review.manifest_resume_requested'",
                (WAITING_ORG, REPO, review_id),
            ).fetchone()["n"]

    def test_a_second_pull_request_from_a_known_base_reaches_analysis(self):
        """The customer-visible bug, end to end.

        Pull request 101 is analysed. Pull request 207 opens from the same base
        commit; its CI recompiles that base, so the base manifest differs in
        every per-run stamp. It must still be analysed.
        """
        first_head, second_head = _sha("b"), _sha("c")

        first = self._open_pull_request(101, first_head)
        self.assertEqual(self._submit(self.base, _manifest()).status_code, 202)
        self.assertEqual(self._submit(first_head, _manifest()).status_code, 202)
        self._resume(first.review_id, first_head)

        second = self._open_pull_request(207, second_head)
        self.assertEqual(second.lifecycle_state, "WAITING_FOR_MANIFEST")

        # The recompiled base, then the new head -- the workflow's own order.
        base_again = self._submit(self.base, _recompiled(_manifest()))
        self.assertEqual(base_again.status_code, 200, base_again.text)
        self.assertEqual(
            self._submit(second_head, _recompiled(_manifest())).status_code, 202)

        outcome = self._resume(second.review_id, second_head)
        self.assertEqual(outcome["status"], "resumed")
        self.assertTrue(outcome["applied"])
        self.assertNotEqual(outcome["lifecycle_state"], "WAITING_FOR_MANIFEST")

    def test_a_reused_base_submission_still_enqueues_the_resume(self):
        """A 200 must wake the waiting review, not just avoid the 409.

        Here the base evidence already exists when the review opens, so the
        base submission is a reuse -- and that reuse is the only thing that can
        complete the pair for this review.
        """
        head = _sha("b")
        self.assertEqual(self._submit(self.base, _manifest()).status_code, 202)

        review = self._open_pull_request(311, head)
        self.assertEqual(self._resume_jobs(review.review_id), 0)

        self.assertEqual(self._submit(head, _manifest()).status_code, 202)
        reuse = self._submit(self.base, _recompiled(_manifest()))

        self.assertEqual(reuse.status_code, 200, reuse.text)
        self.assertEqual(self._resume_jobs(review.review_id), 1)

    def _conflicted_review(self, pull_number=404):
        """A delivery whose base manifest disagrees with stored evidence."""
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        head = _sha("b")
        self.assertEqual(
            self._submit(self.base, _manifest(sql="select 1 as revenue")).status_code,
            202)
        with self.pool.acquire() as store:
            outcome = begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=pull_number, base_sha=self.base,
                head_sha=head,
                base_manifest=_manifest(sql="select 2 as revenue"),
                head_manifest=None,
                # A real changed file: a retried conflict goes on to analyse,
                # and analysis needs something to analyse.
                changed_files=["models/fct_revenue.sql"],
                enforcement_mode="shadow",
                delivery_id=f"delivery-{pull_number}")
        return outcome, head

    def test_a_conflicting_base_persists_the_review_in_an_explicit_state(self):
        """A genuine difference is recorded, visibly, and is not a wait.

        The webhook writes evidence BEFORE the review row exists, so a raised
        conflict used to abort the delivery with nothing persisted and every
        redelivery repeated it. The review now exists and says what is wrong.
        """
        outcome, _ = self._conflicted_review()

        with self.pool.acquire() as store:
            review = store.get_review(WAITING_ORG, REPO, outcome.review_id)
            events = [row["event_type"]
                      for row in store.audit_events(WAITING_ORG, REPO)]

        self.assertEqual(outcome.lifecycle_state, "MANIFEST_CONFLICT")
        self.assertFalse(outcome.waiting)
        self.assertEqual(review["lifecycle_state"], "MANIFEST_CONFLICT")
        self.assertIn("review.manifest_evidence_conflict", events)
        conflicts = review["payload"]["manifest_wait"]["evidence_conflicts"]
        self.assertEqual([c["side"] for c in conflicts], ["base"])
        self.assertEqual(conflicts[0]["commit_sha"], self.base)
        self.assertEqual(outcome.evidence["base_manifest"], "CONFLICT")

    def test_a_conflicted_review_is_not_published_as_waiting(self):
        """The pull request must not be told to sit tight."""
        from agent.metadata_evidence.waiting_publication import (
            render_manifest_conflict_result,
        )

        outcome, head = self._conflicted_review(pull_number=405)
        rendered = render_manifest_conflict_result(
            outcome, base_sha=self.base, head_sha=head)

        self.assertEqual(rendered["decision"], "MANIFEST_CONFLICT")
        self.assertFalse(rendered["final"])
        markdown = rendered["rendered"]["markdown"]
        self.assertIn("Action required", markdown)
        self.assertIn(self.base, markdown)
        self.assertNotIn("waiting for the CI-generated dbt manifests", markdown)

    def test_a_conflicted_review_does_not_proceed_to_analysis(self):
        """THE GUARANTEE. Analysis must not run from the stored evidence."""
        from agent.metadata_evidence.manifest_handoff import resume_manifest_review

        outcome, head = self._conflicted_review(pull_number=406)
        self.assertEqual(self._submit(head, _manifest()).status_code, 202)

        with self.pool.acquire() as store:
            result = resume_manifest_review(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, review_id=outcome.review_id, commit_sha=head)
            review = store.get_review(WAITING_ORG, REPO, outcome.review_id)
            attempts = store.review_attempts(WAITING_ORG, REPO, outcome.review_id)

        self.assertEqual(result["status"], "manifest_conflict")
        self.assertFalse(result["applied"])
        self.assertEqual([c["side"] for c in result["conflicts"]], ["base"])
        # Still conflicted, still undecided, and no attempt was recorded.
        self.assertEqual(review["lifecycle_state"], "MANIFEST_CONFLICT")
        self.assertIsNone(review["decision"])
        self.assertEqual(attempts, [])

    def test_a_conflicted_review_is_not_woken_by_later_evidence(self):
        """No resume work is queued for it, either."""
        outcome, head = self._conflicted_review(pull_number=407)

        self.assertEqual(self._submit(head, _manifest()).status_code, 202)
        self.assertEqual(self._submit(self.base, _recompiled(
            _manifest(sql="select 1 as revenue"))).status_code, 200)

        self.assertEqual(self._resume_jobs(outcome.review_id), 0)

    def test_a_conflict_is_retryable_once_the_disagreement_is_gone(self):
        """Not a dead end.

        The author re-compiles from a clean checkout, so the next delivery
        carries a manifest that agrees with the stored evidence. The review
        returns to WAITING_FOR_MANIFEST and analysis proceeds.
        """
        from agent.metadata_evidence.manifest_handoff import (
            begin_manifest_wait, resume_manifest_review,
        )

        outcome, head = self._conflicted_review(pull_number=408)

        with self.pool.acquire() as store:
            retried = begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=408, base_sha=self.base,
                head_sha=head,
                # The agreeing manifest this time, volatile fields aside.
                base_manifest=_recompiled(_manifest(sql="select 1 as revenue")),
                head_manifest=None, changed_files=[],
                enforcement_mode="shadow", delivery_id="delivery-408-retry")

        self.assertEqual(retried.review_id, outcome.review_id)
        self.assertEqual(retried.lifecycle_state, "WAITING_FOR_MANIFEST")
        self.assertTrue(retried.waiting)

        self.assertEqual(self._submit(head, _manifest()).status_code, 202)
        with self.pool.acquire() as store:
            result = resume_manifest_review(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, review_id=outcome.review_id, commit_sha=head)
            states = [row["to_state"] for row in store.review_transitions(
                WAITING_ORG, REPO, outcome.review_id)]

        self.assertEqual(result["status"], "resumed")
        self.assertTrue(result["applied"])
        self.assertIn("WAITING_FOR_MANIFEST", states)

    def test_a_volatile_only_difference_never_conflicts_the_review(self):
        """The other half of the policy: reuse and continue, as before.

        Same source, recompiled, delivered through the webhook path. This must
        stay a plain wait -- the conflict state is for real disagreement only.
        """
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        head = _sha("b")
        self.assertEqual(self._submit(self.base, _manifest()).status_code, 202)

        with self.pool.acquire() as store:
            outcome = begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=409, base_sha=self.base,
                head_sha=head, base_manifest=_recompiled(_manifest()),
                head_manifest=None, changed_files=[],
                enforcement_mode="shadow", delivery_id="delivery-409")
            review = store.get_review(WAITING_ORG, REPO, outcome.review_id)

        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_MANIFEST")
        self.assertTrue(outcome.waiting)
        self.assertEqual(review["payload"]["manifest_wait"]["evidence_conflicts"],
                         [])

    def test_a_legacy_representation_never_conflicts_the_review(self):
        """Same, for a row written before canonicalisation existed."""
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        head = _sha("b")
        legacy = _manifest()
        with self.pool.acquire() as store:
            store.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, "
                "idempotency_key, payload_hash, semantic_manifest_hash, "
                "canonicalization_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 1)",
                (WAITING_ORG, REPO, "manifest-legacy-wait-" + self.base[:8],
                 self.base, "0" * 64, store._Jsonb(legacy),
                 _key(self.base), "1" * 64),
            )
            outcome = begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=410, base_sha=self.base,
                head_sha=head, base_manifest=_recompiled(legacy),
                head_manifest=None, changed_files=[],
                enforcement_mode="shadow", delivery_id="delivery-410")

        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_MANIFEST")
        self.assertTrue(outcome.waiting)

    def test_the_webhook_and_ci_paths_agree_on_one_commit(self):
        """The webhook writing a committed manifest, then CI writing its own
        recompile of the same commit. Two writers, two keys, one row."""
        head = _sha("b")
        from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

        with self.pool.acquire() as store:
            begin_manifest_wait(
                store, organization_id=WAITING_ORG, repository_id=REPO,
                environment=ENV, pull_number=512, base_sha=self.base,
                head_sha=head, base_manifest=_manifest(), head_manifest=None,
                changed_files=[], enforcement_mode="shadow")

        response = self._submit(self.base, _recompiled(_manifest()))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["created"], False)

        with self.pool.acquire() as store:
            rows = store.connection.execute(
                "SELECT count(*) AS n FROM manifest_evidence "
                "WHERE organization_id=%s AND repository_id=%s AND commit_sha=%s",
                (WAITING_ORG, REPO, self.base),
            ).fetchone()["n"]
        self.assertEqual(rows, 1)


if __name__ == "__main__":
    unittest.main()
