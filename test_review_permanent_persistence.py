"""A review outlives the pull request it reviewed.

The defect this file exists to close had two halves, and both of them made a
persisted review disappear from the product while its row sat untouched in
PostgreSQL:

1. `pull_request` deliveries with action `closed` were dropped on the floor by
   the webhook parser, so nothing in Relium ever learned that a pull request
   had been merged or closed. That is not a deletion bug -- it is worse than
   one to diagnose, because the reviews were all still there and simply
   described a world that had moved on.

2. `GET /api/reviews` bounded the list by the plan's `history_retention_days`.
   Free is seven days. The rows were never deleted, but a Free workspace
   watching its own analyses vanish from the Changes page after a week cannot
   tell the difference, and the History page is supposed to be the permanent
   audit record.

Every test here therefore asserts a PERSISTENCE property, from the served
boundary where one exists: the runner that a real webhook invokes, and the API
route that the dashboard calls. Counting rows before and after is deliberate --
an assertion that a review is still readable can be satisfied by a cache, but
an assertion that the table is the same size cannot.

Requires a real PostgreSQL server via RELIUM_TEST_POSTGRES_DSN.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

MIGRATIONS = Path(__file__).parent / "agent" / "migrations" / "postgres"
PR_STATE_MIGRATION = MIGRATIONS / "0030_review_pr_state.sql"

OWNER, REPO_NAME = "AcmeOrg", "analytics"
REPOSITORY_ID = 987654
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
NEXT_HEAD_SHA = "3" * 40
ENVIRONMENT = "production"

#: Every table that holds a piece of a review. A PR lifecycle event may not
#: shrink ANY of them. Listed literally rather than discovered, so that a
#: release which adds review state and a delete path alongside it fails here.
REVIEW_TABLES = (
    "reviews",
    "review_attempts",
    "review_lifecycle_transitions",
    "review_evidence_coverage",
    "review_change_requests",
    "review_exceptions",
    "snapshot_review_bindings",
    "collection_requests",
    "audit_events",
)


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
LATER_MANIFEST = {"nodes": {"model.a.fct_orders": _model(
    "fct_orders", ["source.a.raw.orders"],
    ["order_id", "net_revenue", "refund_amount"])}, "sources": SOURCES}


class _FakeGitHubClient:
    """The scripted GitHub the served runner talks to."""

    def __init__(self):
        self.comments = {}
        self.checks = {}
        self._next_id = 1000
        self.manifests = {BASE_SHA: BASE_MANIFEST, HEAD_SHA: HEAD_MANIFEST,
                          NEXT_HEAD_SHA: LATER_MANIFEST}
        self.config = b"enabled: true\nenforcement_mode: enforce\n"
        self.calls = []

    def with_token(self, _token):
        return self

    def get_file(self, owner, repository, path, ref):
        from agent.github_app.client import GitHubNotFoundError

        self.calls.append(("get_file", path, ref))
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

    def list_issue_comments(self, owner, repository, pull_number, **kwargs):
        return list(self.comments.values())

    def create_issue_comment(self, owner, repository, pull_number, body, **kwargs):
        self._next_id += 1
        comment = {"id": self._next_id, "body": body,
                   "performed_via_github_app": {"id": 4456468}}
        self.comments[self._next_id] = comment
        self.calls.append(("create_issue_comment", pull_number))
        return comment

    def update_issue_comment(self, owner, repository, comment_id, body, **kwargs):
        self.comments[comment_id]["body"] = body
        return self.comments[comment_id]

    def list_check_runs(self, owner, repository, head_sha, **kwargs):
        return [c for c in self.checks.values() if c["head_sha"] == head_sha]

    def create_check_run(self, owner, repository, payload, **kwargs):
        self._next_id += 1
        check = {"id": self._next_id, **payload}
        self.checks[self._next_id] = check
        self.calls.append(("create_check_run", payload.get("head_sha")))
        return check

    def update_check_run(self, owner, repository, check_run_id, payload, **kwargs):
        self.checks[check_run_id].update(payload)
        return self.checks[check_run_id]


def _payload(*, action="opened", head_sha=HEAD_SHA, pull_number=7, merged=None):
    body = {
        "action": action,
        "installation": {"id": 150697881},
        "repository": {"id": REPOSITORY_ID, "name": REPO_NAME,
                       "owner": {"login": OWNER},
                       "full_name": f"{OWNER}/{REPO_NAME}"},
        "pull_request": {
            "number": pull_number,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"sha": BASE_SHA, "ref": "main"},
        },
        "sender": {"login": "e2e-author"},
    }
    if merged is not None:
        body["pull_request"]["merged"] = merged
    return body


# =====================================================================
# The served GitHub path: merge and close preserve everything.
# =====================================================================

@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; persistence is a database property")
class PullRequestLifecyclePreservesReviewsTests(unittest.TestCase):
    """Merge it, close it, re-analyse it. Nothing may be removed."""

    def setUp(self):
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
        self.lifecycle = ReviewLifecycleService(self.pool, environment=ENVIRONMENT)
        self.runner = PullRequestReviewRunner(
            storage=RepositoryStorage(self.tmp.name), lifecycle=self.lifecycle)
        self.github = _FakeGitHubClient()

    # -- helpers -------------------------------------------------------

    def _deliver(self, **kwargs):
        """Drive one delivery through the parser and the real runner."""
        from agent.github_app.webhooks import parse_webhook

        delivery_id = f"delivery-{uuid.uuid4().hex[:10]}"
        event = parse_webhook(
            event_name="pull_request", delivery_id=delivery_id,
            body=json.dumps(_payload(**kwargs)).encode())
        self.assertIsNotNone(
            event, f"delivery was ignored by the parser: {kwargs}")
        return self.runner.run(event, self.github, expected_app_id=4456468)

    def _review(self, review_id):
        with self.pool.acquire() as store:
            return store.get_review(OWNER, REPO_NAME, review_id)

    def _counts(self):
        with self.pool.acquire() as store:
            return {t: store.connection.execute(
                f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in REVIEW_TABLES}

    def _attempts(self, review_id):
        with self.pool.acquire() as store:
            return store.review_attempts(OWNER, REPO_NAME, review_id)

    # -- an analysed review survives a merge ---------------------------

    def test_an_analysed_review_is_still_queryable_after_its_pr_is_merged(self):
        analysed = self._deliver()
        review_id = analysed["review_id"]
        self.assertIsNotNone(self._review(review_id))

        closed = self._deliver(action="closed", merged=True)
        self.assertEqual(closed["status"], "pr_state_recorded")

        review = self._review(review_id)
        self.assertIsNotNone(review, "the review vanished when the PR merged")
        self.assertEqual(review["pr_state"], "MERGED")

    def test_a_merge_preserves_the_decision_and_the_evidence_binding(self):
        """The row is not merely present -- it still says what it said."""
        analysed = self._deliver()
        review_id = analysed["review_id"]
        before = self._review(review_id)

        self._deliver(action="closed", merged=True)
        after = self._review(review_id)

        for field in ("decision", "lifecycle_state", "attempt", "health",
                      "evidence_coverage", "base_sha", "head_sha",
                      "base_manifest_hash", "head_manifest_hash",
                      "policy_version", "policy_hash", "enforcement_mode",
                      "payload", "created_at"):
            self.assertEqual(after[field], before[field], field)

    def test_every_attempt_survives_the_merge(self):
        analysed = self._deliver()
        review_id = analysed["review_id"]
        before = self._attempts(review_id)
        self.assertTrue(before, "the analysis recorded no attempt to preserve")

        self._deliver(action="closed", merged=True)

        self.assertEqual(self._attempts(review_id), before)

    # -- an analysed review survives a close ---------------------------

    def test_an_analysed_review_is_still_queryable_after_its_pr_is_closed(self):
        analysed = self._deliver()
        review_id = analysed["review_id"]

        closed = self._deliver(action="closed", merged=False)
        self.assertEqual(closed["status"], "pr_state_recorded")

        review = self._review(review_id)
        self.assertIsNotNone(review, "the review vanished when the PR closed")
        self.assertEqual(review["pr_state"], "CLOSED")

    def test_a_close_without_a_merged_flag_is_recorded_as_closed(self):
        """GitHub distinguishes the two ONLY by `pull_request.merged`. A
        payload missing it must never be read as a merge."""
        analysed = self._deliver()
        self._deliver(action="closed")
        self.assertEqual(self._review(analysed["review_id"])["pr_state"], "CLOSED")

    # -- the state itself ----------------------------------------------

    def test_a_freshly_analysed_review_is_open(self):
        analysed = self._deliver()
        self.assertEqual(self._review(analysed["review_id"])["pr_state"], "OPEN")

    def test_closing_marks_every_review_of_that_pull_request(self):
        """One PR, two head SHAs, two reviews. Both described that PR."""
        first = self._deliver(head_sha=HEAD_SHA)["review_id"]
        second = self._deliver(action="synchronize",
                               head_sha=NEXT_HEAD_SHA)["review_id"]
        self.assertNotEqual(first, second)

        recorded = self._deliver(action="closed", merged=True)

        self.assertEqual(set(recorded["reviews"]), {first, second})
        self.assertEqual(self._review(first)["pr_state"], "MERGED")
        self.assertEqual(self._review(second)["pr_state"], "MERGED")

    def test_a_merge_does_not_touch_another_pull_requests_reviews(self):
        mine = self._deliver(pull_number=7)["review_id"]
        theirs = self._deliver(pull_number=8, head_sha=NEXT_HEAD_SHA)["review_id"]

        self._deliver(action="closed", pull_number=7, merged=True)

        self.assertEqual(self._review(mine)["pr_state"], "MERGED")
        self.assertEqual(self._review(theirs)["pr_state"], "OPEN")

    def test_the_merge_timestamp_is_recorded_separately_from_the_review(self):
        analysed = self._deliver()
        self._deliver(action="closed", merged=True)
        review = self._review(analysed["review_id"])
        self.assertIsNotNone(review["pr_state_updated_at"])

    def test_recording_a_pr_state_is_idempotent(self):
        """GitHub redelivers. A second `closed` changes nothing and removes
        nothing."""
        analysed = self._deliver()
        self._deliver(action="closed", merged=True)
        before = self._counts()
        first = self._review(analysed["review_id"])

        self._deliver(action="closed", merged=True)

        self.assertEqual(self._counts(), before)
        self.assertEqual(self._review(analysed["review_id"]), first)

    # -- a newer analysis is additive ----------------------------------

    def test_a_newer_analysis_does_not_delete_an_older_analysis(self):
        first = self._deliver(head_sha=HEAD_SHA)["review_id"]
        first_before = self._review(first)
        first_attempts = self._attempts(first)

        second = self._deliver(action="synchronize",
                               head_sha=NEXT_HEAD_SHA)["review_id"]

        self.assertNotEqual(first, second)
        self.assertIsNotNone(self._review(second))
        self.assertEqual(self._review(first), first_before,
                         "the earlier analysis was rewritten by the newer one")
        self.assertEqual(self._attempts(first), first_attempts)

    def test_both_analyses_are_listed_for_the_pull_request(self):
        first = self._deliver(head_sha=HEAD_SHA)["review_id"]
        second = self._deliver(action="synchronize",
                               head_sha=NEXT_HEAD_SHA)["review_id"]
        with self.pool.acquire() as store:
            page = store.list_reviews(OWNER, REPO_NAME, environment=ENVIRONMENT,
                                      limit=50)
        self.assertEqual({r["review_id"] for r in page["items"]}, {first, second})

    # -- the guarantee, stated as row counts ---------------------------

    def test_no_review_rows_are_deleted_by_pr_lifecycle_processing(self):
        """The whole point, asserted the only way that cannot be faked."""
        self._deliver(head_sha=HEAD_SHA)
        self._deliver(action="synchronize", head_sha=NEXT_HEAD_SHA)
        self._deliver(pull_number=8, head_sha=HEAD_SHA)
        before = self._counts()
        self.assertGreater(before["reviews"], 0)
        self.assertGreater(before["review_attempts"], 0)

        self._deliver(action="closed", pull_number=7, merged=True)
        self._deliver(action="closed", pull_number=8, merged=False)

        after = self._counts()
        for table in REVIEW_TABLES:
            self.assertGreaterEqual(
                after[table], before[table],
                f"{table} lost rows during PR lifecycle processing")
        self.assertEqual(after["reviews"], before["reviews"])
        self.assertEqual(after["review_attempts"], before["review_attempts"])

    def test_closing_records_an_audit_event_rather_than_removing_one(self):
        analysed = self._deliver()
        self._deliver(action="closed", merged=True)
        with self.pool.acquire() as store:
            events = store.audit_events(OWNER, REPO_NAME)
        recorded = [e for e in events
                    if e["event_type"] == "review.pr_state_recorded"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["payload"]["pr_state"], "MERGED")
        self.assertIn(analysed["review_id"], recorded[0]["payload"]["review_ids"])
        self.assertIn("review.analysed", {e["event_type"] for e in events})

    # -- a close is not an analysis ------------------------------------

    def test_a_close_neither_analyses_nor_publishes(self):
        """It reads no configuration and calls no GitHub API, so a repository
        that has since removed relium.yml still gets its PR's fate recorded."""
        self._deliver()
        self.github.calls.clear()

        recorded = self._deliver(action="closed", merged=True)

        self.assertEqual(recorded["status"], "pr_state_recorded")
        self.assertEqual(self.github.calls, [])

    def test_a_close_is_recorded_even_after_the_repository_disables_relium(self):
        analysed = self._deliver()
        self.github.config = b"enabled: false\n"

        self._deliver(action="closed", merged=True)

        self.assertEqual(self._review(analysed["review_id"])["pr_state"], "MERGED")


# =====================================================================
# The dashboard read: nothing is hidden by a retention window.
# =====================================================================

@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the API reads a real store")
class ReviewHistoryIsNotWindowedTests(unittest.TestCase):
    """A Free workspace sees every review it ever had.

    `billing_settings` is non-None here, which is what a metered deployment
    looks like, and the scope has no reconciled tenant -- so entitlements
    resolve to Free, the plan whose seven-day window used to hide these rows.
    """

    class Settings:
        past_due_grace = timedelta(0)

    @classmethod
    def setUpClass(cls):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.api.routes import create_api_routes
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
        cls.app = Starlette(routes=create_api_routes(
            store_pool=cls.pool, billing_settings=cls.Settings()))
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def setUp(self):
        from agent.api.auth import generate_token, hash_secret

        self.org, self.repo, self.env = "org-history", "repo-history", "prod"
        token_id, secret, presented = generate_token()
        with self.pool.acquire() as store:
            store.ensure_tenant(self.org, self.repo, self.env)
            store.create_service_token(
                token_id, hash_secret(secret), self.org, self.repo,
                environment=self.env, description="test", scope="operator_read")
        self.auth = {"Authorization": f"Bearer {presented}"}

    def _persist_review(self, review_id, *, age_days, pr_state="OPEN"):
        """A review that was analysed `age_days` ago and never removed.

        The schema is reset once for the class, so every review here needs its
        own pull request and head SHA: `uq_reviews_pr_head` is the constraint
        that makes one analysis per (PR, head SHA), and colliding on it would
        be a test artefact rather than the property under test.
        """
        created = datetime.now(timezone.utc) - timedelta(days=age_days)
        digest = int(hashlib.sha256(review_id.encode()).hexdigest()[:8], 16) % 100000
        with self.pool.acquire() as store:
            store.upsert_pr_review(
                self.org, self.repo, self.env, review_id=review_id,
                pull_number=digest, base_sha=BASE_SHA,
                head_sha=f"{digest:040d}",
                enforcement_mode="shadow", pr_state=pr_state)
            store.connection.execute(
                "UPDATE reviews SET created_at=%s WHERE review_id=%s",
                (created, review_id))
        return review_id

    def _list(self):
        response = self.client.get("/api/reviews", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_review_older_than_the_free_seven_day_window_is_returned(self):
        old = self._persist_review("gh-older-than-free", age_days=30)
        body = self._list()
        self.assertIn(old, {item["review_id"] for item in body["items"]})

    def test_a_review_far_older_than_any_plan_window_is_returned(self):
        old = self._persist_review("gh-older-than-pro", age_days=400)
        self.assertIn(old, {item["review_id"] for item in self._list()["items"]})

    def test_the_total_counts_every_persisted_review(self):
        """A windowed COUNT was the second half of the disappearance: the list
        could be complete and the total still claim otherwise."""
        self._persist_review("gh-total-recent", age_days=1)
        self._persist_review("gh-total-old", age_days=90)
        body = self._list()
        self.assertGreaterEqual(body["total"], 2)
        self.assertEqual(body["total"], len(body["items"]))

    def test_no_window_is_advertised_because_none_is_applied(self):
        """Reporting `7` while returning everything would be a worse lie than
        the bug it replaced."""
        self._persist_review("gh-window-report", age_days=1)
        self.assertIsNone(self._list()["history_window_days"])

    def test_the_merged_state_crosses_the_api(self):
        merged = self._persist_review("gh-api-merged", age_days=20,
                                      pr_state="MERGED")
        item = next(i for i in self._list()["items"]
                    if i["review_id"] == merged)
        self.assertEqual(item["pr_state"], "MERGED")

    def test_a_merged_review_is_readable_by_id(self):
        merged = self._persist_review("gh-api-detail", age_days=20,
                                      pr_state="MERGED")
        response = self.client.get(f"/api/reviews/{merged}", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pr_state"], "MERGED")


# =====================================================================
# The migration, over a database that already holds reviews.
# =====================================================================

class PrStateMigrationContractTests(unittest.TestCase):
    """0030 is additive. Read as text, so the property holds without a server."""

    def setUp(self):
        self.sql = PR_STATE_MIGRATION.read_text(encoding="utf-8")

    def test_it_creates_no_table_and_drops_nothing(self):
        upper = self.sql.upper()
        self.assertNotIn("CREATE TABLE", upper)
        self.assertNotIn("DROP TABLE", upper)
        self.assertNotIn("DROP COLUMN", upper)
        self.assertNotIn("DELETE FROM", upper)
        self.assertNotIn("TRUNCATE", upper)

    def test_it_rewrites_no_existing_review_data(self):
        """The only permitted write is the column DEFAULT filling new columns."""
        self.assertNotIn("UPDATE REVIEWS", self.sql.upper())

    def test_it_defines_exactly_the_four_states(self):
        import re

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        constraint = self.sql.split("reviews_pr_state_check")[-1].split(";")[0]
        self.assertEqual(set(re.findall(r"'([A-Z_]+)'", constraint)),
                         set(PostgresLifecycleStore.REVIEW_PR_STATES))
        self.assertEqual(set(PostgresLifecycleStore.REVIEW_PR_STATES),
                         {"OPEN", "MERGED", "CLOSED", "UNKNOWN"})

    def test_legacy_rows_default_to_unknown(self):
        self.assertIn("DEFAULT 'UNKNOWN'", self.sql)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; a migration is a database property")
class LegacyReviewsSurviveTheMigrationTests(unittest.TestCase):
    """A review written by the PREVIOUS release, then upgraded.

    The database is migrated only as far as 0029, a review is written exactly
    the way the release before this one wrote it, and only then is 0030
    applied. Anything less would test the migration against rows this release
    created, which is the one case that cannot fail.
    """

    ORG, REPO, ENV = "legacy-org", "legacy-repo", "prod"
    REVIEW_ID = "gh-legacy-review-0001"

    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        from agent import postgres_migrate

        self.Jsonb = Jsonb
        _reset_schema(DSN)

        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, staging, True)
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "0030_":
                shutil.copy(path, Path(staging) / path.name)
        staged = {p.name for p in Path(staging).glob("*.sql")}
        self.assertNotIn(PR_STATE_MIGRATION.name, staged)

        self.connection = psycopg.connect(DSN, autocommit=True,
                                          row_factory=dict_row)
        self.addCleanup(self.connection.close)

        original = postgres_migrate.MIGRATIONS_DIR
        postgres_migrate.MIGRATIONS_DIR = Path(staging)
        try:
            postgres_migrate.apply_migrations(self.connection)
        finally:
            postgres_migrate.MIGRATIONS_DIR = original

        self.connection.execute(
            "INSERT INTO organizations (organization_id) VALUES (%s)", (self.ORG,))
        self.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "VALUES (%s, %s)", (self.ORG, self.REPO))
        self.connection.execute(
            "INSERT INTO environments (organization_id, repository_id, "
            "environment, connected) VALUES (%s, %s, %s, TRUE)",
            (self.ORG, self.REPO, self.ENV))

    def _write_legacy_review(self):
        """Exactly the INSERT the previous release issued, and no more."""
        self.connection.execute(
            "INSERT INTO reviews (review_id, organization_id, repository_id, "
            "environment, pull_number, commit_sha, decision, enforcement_mode, "
            "evidence_coverage, lifecycle_state, base_sha, head_sha, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (self.REVIEW_ID, self.ORG, self.REPO, self.ENV, 11, HEAD_SHA,
             "WARN", "shadow", "COMPLETE", "PUBLISHED", BASE_SHA, HEAD_SHA,
             self.Jsonb({"plan": {"metadata_required": False}})))

    def _apply_0030(self):
        from agent import postgres_migrate

        return postgres_migrate.apply_migrations(self.connection)

    def _row(self):
        return self.connection.execute(
            "SELECT * FROM reviews WHERE review_id=%s", (self.REVIEW_ID,)
        ).fetchone()

    def test_the_previous_release_schema_has_no_pr_state(self):
        columns = {row["column_name"] for row in self.connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='reviews'").fetchall()}
        self.assertNotIn("pr_state", columns)

    def test_the_migration_applies_over_existing_reviews(self):
        self._write_legacy_review()
        self.assertIn(30, self._apply_0030())

    def test_a_legacy_review_survives_the_migration_field_for_field(self):
        self._write_legacy_review()
        self._apply_0030()
        row = self._row()
        self.assertEqual(row["review_id"], self.REVIEW_ID)
        self.assertEqual(row["decision"], "WARN")
        self.assertEqual(row["lifecycle_state"], "PUBLISHED")
        self.assertEqual(row["evidence_coverage"], "COMPLETE")
        self.assertEqual(row["base_sha"], BASE_SHA)
        self.assertEqual(row["head_sha"], HEAD_SHA)
        self.assertEqual(row["payload"], {"plan": {"metadata_required": False}})

    def test_a_legacy_review_reads_as_unknown_rather_than_open(self):
        """It is not known to be open. Nothing ever observed its pull
        request, and inventing OPEN would be asserting a fact we do not have."""
        self._write_legacy_review()
        self._apply_0030()
        self.assertEqual(self._row()["pr_state"], "UNKNOWN")
        self.assertIsNone(self._row()["pr_state_updated_at"])

    def test_a_legacy_review_is_readable_through_the_store(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self._write_legacy_review()
        self._apply_0030()
        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.close)
        self.assertIsNotNone(store.get_review(self.ORG, self.REPO, self.REVIEW_ID))
        page = store.list_reviews(self.ORG, self.REPO, environment=self.ENV)
        self.assertEqual([r["review_id"] for r in page["items"]], [self.REVIEW_ID])

    def test_a_legacy_review_can_still_be_marked_merged(self):
        """The upgrade does not strand old rows outside the new lifecycle."""
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self._write_legacy_review()
        self._apply_0030()
        store = PostgresLifecycleStore(DSN)
        self.addCleanup(store.close)
        moved = store.record_pr_state(self.ORG, self.REPO, 11, pr_state="MERGED")
        self.assertEqual(moved, [self.REVIEW_ID])
        self.assertEqual(self._row()["pr_state"], "MERGED")

    def test_applying_the_migration_twice_is_a_no_op(self):
        self._write_legacy_review()
        self._apply_0030()
        self.assertEqual(self._apply_0030(), [])


# =====================================================================
# Guards on the implementation itself.
# =====================================================================

class NoDeletionPathExistsTests(unittest.TestCase):
    """Source-level guards. These fail when someone adds the delete back."""

    def test_recording_a_pr_state_contains_no_delete(self):
        import inspect

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        source = inspect.getsource(PostgresLifecycleStore.record_pr_state)
        self.assertNotRegex(source.upper(), r"\bDELETE\s+FROM\b")
        self.assertIn("UPDATE reviews SET pr_state", source)

    def test_the_closed_branch_of_the_runner_contains_no_delete(self):
        import inspect

        from agent.github_app.runner import PullRequestReviewRunner

        source = inspect.getsource(PullRequestReviewRunner._record_pr_closed)
        self.assertNotIn("delete", source.lower())

    def test_no_module_deletes_a_review_row(self):
        """A review row is removed by exactly one thing -- a customer deleting
        their workspace -- and that path names its tables in a list rather than
        issuing a literal `DELETE FROM reviews`."""
        import re

        root = Path(__file__).parent / "agent"
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(
                    r"DELETE\s+FROM\s+(reviews|review_attempts)\b", text):
                offenders.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(offenders, [], "a direct review delete was introduced")

    def test_the_closed_action_is_supported_but_is_not_an_analysis_trigger(self):
        from agent.github_app.webhooks import (
            ANALYSIS_PULL_REQUEST_ACTIONS, SUPPORTED_PULL_REQUEST_ACTIONS,
        )

        self.assertIn("closed", SUPPORTED_PULL_REQUEST_ACTIONS)
        self.assertNotIn("closed", ANALYSIS_PULL_REQUEST_ACTIONS)
        self.assertTrue(
            ANALYSIS_PULL_REQUEST_ACTIONS < SUPPORTED_PULL_REQUEST_ACTIONS)

    def test_the_history_window_is_off(self):
        from agent.api.routes import REVIEW_HISTORY_WINDOW_ENFORCED

        self.assertFalse(REVIEW_HISTORY_WINDOW_ENFORCED)

    def test_the_retention_entitlement_is_untouched(self):
        """Turning the window off must not have edited what the plan SELLS."""
        from agent.billing.entitlements import FREE, PRO, STARTER

        self.assertEqual(FREE.history_retention_days, 7)
        self.assertEqual(STARTER.history_retention_days, 90)
        self.assertIsNone(PRO.history_retention_days)


class WebhookParsingTests(unittest.TestCase):
    """The merged/closed distinction, which exists only in the payload."""

    def _parse(self, **kwargs):
        from agent.github_app.webhooks import parse_webhook

        return parse_webhook(
            event_name="pull_request", delivery_id="d-1",
            body=json.dumps(_payload(**kwargs)).encode())

    def test_a_merged_close_reads_as_merged(self):
        self.assertEqual(self._parse(action="closed", merged=True).pr_state,
                         "MERGED")

    def test_an_unmerged_close_reads_as_closed(self):
        self.assertEqual(self._parse(action="closed", merged=False).pr_state,
                         "CLOSED")

    def test_a_close_missing_the_flag_reads_as_closed(self):
        self.assertEqual(self._parse(action="closed").pr_state, "CLOSED")

    def test_a_non_boolean_merged_flag_is_not_a_merge(self):
        from agent.github_app.webhooks import parse_webhook

        body = _payload(action="closed")
        body["pull_request"]["merged"] = "true"
        event = parse_webhook(event_name="pull_request", delivery_id="d-1",
                              body=json.dumps(body).encode())
        self.assertEqual(event.pr_state, "CLOSED")

    def test_an_opened_delivery_reads_as_open(self):
        self.assertEqual(self._parse(action="opened").pr_state, "OPEN")

    def test_an_unrelated_action_is_still_ignored(self):
        self.assertIsNone(self._parse(action="labeled"))


if __name__ == "__main__":
    unittest.main()
