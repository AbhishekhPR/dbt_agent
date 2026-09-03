"""A Free review must still publish its GitHub check and comment.

Reaching a decision is only half the fix. The runner publishes a NEUTRAL
"waiting" check whenever the lifecycle reports ``waiting=True``, so a Free
review that never left ``WAITING_FOR_METADATA`` published a permanent
"waiting for production metadata" comment and a neutral check that would never
be updated. These tests start at the real ``PullRequestReviewRunner``, drive a
real webhook event through it, and assert the published artifacts.

Nothing here calls ``begin_review`` directly: the point is the wiring from the
served path through the lifecycle to GitHub.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from agent.billing.plans import PLAN_FREE, PLAN_PRO
from agent.github_app.runner import PullRequestReviewRunner
from agent.github_app.storage import RepositoryStorage
from agent.github_app.webhooks import parse_webhook
from agent.metadata_evidence.service import ReviewLifecycleService
from lifecycle_store_test_support import InMemoryLifecycleStore, active_billing_row

OWNER, REPO_NAME, REPOSITORY_ID = "AcmeOrg", "analytics", 987654
BASE_SHA, HEAD_SHA = "1" * 40, "2" * 40
APP_ID = 4456468

POLAR = {
    "POLAR_ACCESS_TOKEN": "polar_oat_test",
    "POLAR_WEBHOOK_SECRET": "whsec-test",
    "POLAR_STARTER_PRODUCT_ID": "prod-starter",
    "POLAR_PRO_PRODUCT_ID": "prod-pro",
}


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


class _FakeGitHubClient:
    def __init__(self):
        self.comments = {}
        self.checks = {}
        self._next_id = 1000
        self.manifests = {BASE_SHA: BASE_MANIFEST, HEAD_SHA: HEAD_MANIFEST}
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

    def list_issue_comments(self, owner, repository, pull_number, **kwargs):
        return list(self.comments.values())

    def create_issue_comment(self, owner, repository, pull_number, body, **kwargs):
        self._next_id += 1
        comment = {"id": self._next_id, "body": body,
                   "performed_via_github_app": {"id": APP_ID}}
        self.comments[self._next_id] = comment
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
        return check

    def update_check_run(self, owner, repository, check_run_id, payload, **kwargs):
        self.checks[check_run_id].update(payload)
        return self.checks[check_run_id]


class _Pool:
    def __init__(self, store):
        self._store = store

    @contextlib.contextmanager
    def acquire(self):
        yield self._store


def _event(delivery_id, pull_number=7):
    return {
        "action": "opened",
        "installation": {"id": 150697881},
        "repository": {"id": REPOSITORY_ID, "name": REPO_NAME,
                       "owner": {"login": OWNER},
                       "full_name": f"{OWNER}/{REPO_NAME}"},
        "pull_request": {"number": pull_number,
                         "head": {"sha": HEAD_SHA, "ref": "feature"},
                         "base": {"sha": BASE_SHA, "ref": "main"}},
        "sender": {"login": "e2e-author"},
    }


class _RunnerCase(unittest.TestCase):
    plan = PLAN_FREE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = InMemoryLifecycleStore(
            tenants={(OWNER, REPO_NAME): "tenant-1"},
            billing={"tenant-1": active_billing_row(self.plan)})
        self.runner = PullRequestReviewRunner(
            storage=RepositoryStorage(self.tmp.name),
            lifecycle=ReviewLifecycleService(_Pool(self.store),
                                             environment="production"))
        self.client = _FakeGitHubClient()
        with mock.patch.dict(os.environ, POLAR, clear=True):
            body = json.dumps(_event("delivery-1")).encode()
            event = parse_webhook(event_name="pull_request",
                                  delivery_id="delivery-1", body=body)
            self.response = self.runner.run(event, self.client,
                                            expected_app_id=APP_ID)

    @property
    def comment(self):
        return next(iter(self.client.comments.values()))

    @property
    def check(self):
        return next(iter(self.client.checks.values()))


class FreePublishesARealDecision(_RunnerCase):
    plan = PLAN_FREE

    def test_the_review_reached_the_lifecycle(self):
        self.assertIn("review_id", self.response)

    def test_the_published_status_is_a_review_not_a_wait(self):
        self.assertEqual(self.response["status"], "reviewed")
        self.assertNotEqual(self.response["lifecycle_state"],
                            "WAITING_FOR_METADATA")

    def test_a_comment_is_published(self):
        self.assertEqual(len(self.client.comments), 1)
        self.assertIn("Relium", self.comment["body"])

    def test_the_comment_does_not_claim_the_review_is_still_waiting(self):
        body = self.comment["body"]
        self.assertNotIn("waiting for production metadata", body.lower())
        self.assertNotIn("has not reached a decision yet", body.lower())

    def test_a_check_run_is_published_and_completed(self):
        self.assertEqual(len(self.client.checks), 1)
        self.assertEqual(self.check["status"], "completed")
        self.assertIsNotNone(self.check.get("conclusion"))

    def test_no_collection_request_is_advertised(self):
        self.assertIsNone(self.response["collection_request_id"])

    def test_the_publication_identity_is_recorded_for_reconciliation(self):
        review = self.store.get_review(OWNER, REPO_NAME, self.response["review_id"])
        self.assertEqual(review["comment_id"], self.comment["id"])
        self.assertEqual(review["check_run_id"], self.check["id"])


class PaidPlansStillPublishTheWaitingState(_RunnerCase):
    """The paid lifecycle is untouched: it still publishes a neutral wait."""

    plan = PLAN_PRO

    def test_the_published_status_is_the_metadata_wait(self):
        self.assertEqual(self.response["status"], "waiting_for_metadata")
        self.assertEqual(self.response["lifecycle_state"], "WAITING_FOR_METADATA")

    def test_the_comment_says_the_review_is_unfinished(self):
        self.assertIn("has not reached a decision yet",
                      self.comment["body"].lower())

    def test_a_collection_request_is_advertised(self):
        self.assertIsNotNone(self.response["collection_request_id"])


if __name__ == "__main__":
    unittest.main()
