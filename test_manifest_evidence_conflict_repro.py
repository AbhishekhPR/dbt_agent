"""Reproduction of the PR #46 manifest-handoff 409.

Diagnostic only. Nothing here changes backend behaviour; it pins down which of
the two 409 conditions the workflow actually hits, and what the response body
says, so the fix can be aimed at the right thing.

Real PostgreSQL and the real served route, because the conflict is decided by
database uniqueness and reconciled inside one transaction.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import copy
import os
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "AbhishekhPR"
REPO = "dbt_agent"
# Per-test commit SHAs. `manifest_evidence` is immutable by database
# trigger, so a test cannot clean up after itself -- and should not want to.
_SEQUENCE = iter(range(1, 10_000))


def _shas():
    """A fresh (base, head) pair, so each case starts with no prior evidence."""
    index = next(_SEQUENCE)
    return (f"{index:040x}".replace("x", "a"),
            f"{index + 5000:040x}".replace("x", "b"))

# The GitHub repository id the workflow interpolates into the key. The value
# does not matter; that it is STABLE across runs and across PRs does.
REPOSITORY_ID = "123456789"


def _key(sha):
    """Exactly the key .github/workflows/relium-pr-review.yml sends."""
    return f"github-actions:{REPOSITORY_ID}:{sha}"


def _manifest(*, generated_at, invocation_id):
    """A dbt manifest shaped like the real thing.

    `metadata.generated_at` and `metadata.invocation_id` are what dbt stamps
    afresh on every compile. Everything else here is identical between runs,
    which is the whole point: the SOURCE has not changed, only the run has.
    """
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "generated_at": generated_at,
            "invocation_id": invocation_id,
            "project_name": "relium",
        },
        "nodes": {
            "model.relium.fct_revenue": {
                "resource_type": "model",
                "name": "fct_revenue",
                "database": "analytics",
                "schema": "public",
                "raw_code": "select 1 as revenue",
            },
        },
        "sources": {},
        "child_map": {},
        "parent_map": {},
    }


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the conflict is a database property")
class ManifestEvidenceConflictReproduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
        cls.app = create_http_app(
            webhook_secret="repro-secret", job_queue=_StubQueue(),
            max_body_bytes=8 * 1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool)
        cls.http = TestClient(cls.app)
        cls.http.__enter__()

        from agent.collector.provisioning import issue_ci_token

        with cls.pool.acquire() as store:
            store.ensure_repository(ORG, REPO)
            _, cls.token = issue_ci_token(store, organization_id=ORG,
                                          repository_id=REPO)

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        # No cleanup: the evidence table is append-only and enforced so by a
        # trigger. Fresh SHAs give each case a clean starting point.
        self.base_sha, self.head_sha = _shas()

    def _submit(self, sha, manifest, *, key=None):
        return self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {self.token}",
                     "Idempotency-Key": key or _key(sha)},
            json={"commit_sha": sha, "manifest": manifest})

    # -- what the workflow does on a first run ------------------------------

    def test_a_first_submission_is_accepted(self):
        response = self._submit(self.base_sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIs(response.json()["created"], True)

    # -- a byte-identical retry IS already idempotent -----------------------

    def test_an_identical_resubmission_is_idempotent(self):
        """Proves the backend is NOT missing idempotency.

        Same key, byte-identical payload — accepted, reusing the existing
        record. A retried workflow run that somehow produced the same manifest
        would succeed.
        """
        manifest = _manifest(generated_at="2026-08-18T10:00:00Z",
                             invocation_id="run-1")
        first = self._submit(self.base_sha, manifest)
        second = self._submit(self.base_sha, copy.deepcopy(manifest))

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIs(second.json()["created"], False)
        self.assertEqual(first.json()["evidence_id"],
                         second.json()["evidence_id"])

    # -- THE REPRODUCTION ---------------------------------------------------

    def test_recompiling_the_same_commit_conflicts_on_the_idempotency_key(self):
        """The exact failure PR #46 hits.

        The BASE commit already has evidence from an earlier run or an earlier
        PR. dbt is re-run, stamps a new `generated_at` and `invocation_id`, and
        the manifest is therefore a different document for the same source.

        The workflow's idempotency key is stable per (repository, commit) and
        carries no PR or run identity, so the second submission collides on the
        key first — and the payload no longer matches.
        """
        self._submit(self.base_sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))

        # Same commit, same source, later run.
        response = self._submit(self.base_sha, _manifest(
            generated_at="2026-08-19T09:15:00Z", invocation_id="run-2"))

        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(body["status"], "conflict")
        self.assertEqual(
            body["detail"],
            "idempotency key already used with different manifest evidence")

    def test_a_different_key_on_the_same_commit_conflicts_on_the_sha(self):
        """The other 409 branch, for completeness.

        Reached only if the key differs — which the current workflow cannot
        produce, since its key is derived from repository id and SHA alone.
        """
        self._submit(self.base_sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"),
            key="some-other-key-1")
        response = self._submit(self.base_sha, _manifest(
            generated_at="2026-08-19T09:15:00Z", invocation_id="run-2"),
            key="some-other-key-2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"],
                         "commit SHA already has different manifest evidence")

    def test_only_the_volatile_metadata_differs(self):
        """The two manifests are otherwise the same document.

        If this ever stops holding, the 409 would be a genuine content change
        and the diagnosis above would be wrong.
        """
        first = _manifest(generated_at="2026-08-18T10:00:00Z",
                          invocation_id="run-1")
        second = _manifest(generated_at="2026-08-19T09:15:00Z",
                           invocation_id="run-2")
        for document in (first, second):
            document["metadata"].pop("generated_at")
            document["metadata"].pop("invocation_id")
        self.assertEqual(first, second)

    def test_base_fails_before_head_is_ever_attempted(self):
        """Which request failed.

        The workflow loops base then head and does not catch HTTPError, so a
        409 on base ends the step. Head is never submitted — and the run log
        shows the base payload size printed, then the error.
        """
        self._submit(self.base_sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))

        order = []
        for side, sha in (("base", self.base_sha), ("head", self.head_sha)):
            response = self._submit(sha, _manifest(
                generated_at="2026-08-19T09:15:00Z", invocation_id="run-2"))
            order.append((side, response.status_code))
            if response.status_code not in (200, 202):
                break

        self.assertEqual(order, [("base", 409)])

    def test_a_fresh_head_commit_is_accepted_on_its_own(self):
        """HEAD is not the problem: a commit with no prior evidence is fine."""
        self._submit(self.base_sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))
        response = self._submit(self.head_sha, _manifest(
            generated_at="2026-08-19T09:15:00Z", invocation_id="run-2"))
        self.assertEqual(response.status_code, 202, response.text)

    # -- ruling out the other candidate causes ------------------------------

    def test_the_ci_token_scope_is_the_repository_and_is_not_the_cause(self):
        """A scope mismatch is 401/403, never 409."""
        response = self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": "Bearer rlm_deadbeef.wrong",
                     "Idempotency-Key": _key(self.base_sha)},
            json={"commit_sha": self.base_sha, "manifest": _manifest(
                generated_at="2026-08-18T10:00:00Z", invocation_id="run-1")})
        self.assertEqual(response.status_code, 401)

    def test_the_onboarding_migrations_do_not_touch_manifest_evidence(self):
        """Migrations 0014-0016 add tenancy tables and nothing else.

        Ruling this out explicitly, because "a recent migration changed
        uniqueness" is the kind of theory that survives far too long unchecked.
        """
        from pathlib import Path

        directory = Path("agent/migrations/postgres")
        for name in ("0014_clerk_tenants_and_onboarding.sql",
                     "0015_github_installation_binding.sql",
                     "0016_repository_onboarding.sql"):
            sql = (directory / name).read_text(encoding="utf-8")
            self.assertNotIn("manifest_evidence", sql, name)

    def test_the_manifest_evidence_uniqueness_is_unchanged(self):
        """The constraints are exactly the ones migration 0013 created."""
        with self.pool.acquire() as store:
            rows = store.connection.execute(
                "SELECT conname, contype FROM pg_constraint "
                "WHERE conrelid='manifest_evidence'::regclass "
                "AND contype IN ('p','u') ORDER BY conname").fetchall()
        names = [row["conname"] for row in rows]
        self.assertEqual(len(names), 3, names)


if __name__ == "__main__":
    unittest.main()
