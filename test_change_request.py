"""Submitting a reviewer's request-changes to GitHub.

The property under test is that the record only ever claims what actually
happened: PENDING until GitHub accepts, PUBLISHED with the remote id once it
does, FAILED with a reason when it does not — and never a success that GitHub
never saw.
"""
from __future__ import annotations

import unittest

from agent.metadata_evidence.change_request import (
    ChangeRequestError,
    _body_for,
    submit_change_request,
)


class _Publisher:
    """Records the GitHub call. Optionally fails, like GitHub sometimes does."""

    def __init__(self, *, fail=None, review_id=55501):
        self.calls = []
        self.fail = fail
        self.review_id = review_id

    def submit_request_changes(self, *, pull_number, body):
        self.calls.append({"pull_number": pull_number, "body": body})
        if self.fail:
            raise self.fail
        return {"id": self.review_id, "state": "CHANGES_REQUESTED"}


class _Store:
    def __init__(self, record, review=None, attempts=()):
        self._record = dict(record) if record else None
        self._review = review or {"review_id": "gh-abc", "decision": "BLOCK"}
        self._attempts = [dict(a) for a in attempts]
        self.audit = []

    def get_change_request(self, org, repo, change_request_id):
        return dict(self._record) if self._record else None

    def get_review(self, org, repo, review_id):
        return dict(self._review)

    def review_attempts(self, org, repo, review_id):
        return [dict(a) for a in self._attempts]

    def complete_change_request(self, org, repo, change_request_id, *,
                                remote_review_id=None, failure_reason=None):
        self._record["state"] = "FAILED" if failure_reason else "PUBLISHED"
        self._record["remote_review_id"] = remote_review_id
        self._record["failure_reason"] = failure_reason
        return dict(self._record)

    def append_audit(self, org, repo, **kwargs):
        self.audit.append(kwargs)


BLOCK_FINDING = {
    "code": "column.missing_in_production", "severity": "block",
    "category": "production", "relation": "analytics.orders",
    "column": "customer_id", "message": "does not exist in production",
    "detail": {"dependency_kind": "external"},
}

RECORD = {
    "change_request_id": "cr-1", "review_id": "gh-abc", "attempt": 3,
    "pull_number": 101, "actor": "sarah", "message": "Restore the refund join.",
    "state": "PENDING", "remote_review_id": None,
}

ATTEMPT = {"attempt": 3, "decision": "BLOCK",
           "payload": {"findings": [BLOCK_FINDING]}}


def _run(store, publisher):
    return submit_change_request(
        store, organization_id="acme", repository_id="analytics",
        environment="production", change_request_id="cr-1", publisher=publisher)


class BodyTests(unittest.TestCase):
    def test_body_leads_with_the_relium_decision_and_attempt(self):
        body = _body_for({"decision": "BLOCK"}, ATTEMPT, "Restore the join.")
        self.assertIn("Relium decision: `BLOCK`", body)
        self.assertIn("attempt 3", body)
        self.assertIn("Restore the join.", body)

    def test_body_carries_the_findings_behind_the_request(self):
        body = _body_for({}, ATTEMPT, "Please fix.")
        self.assertIn("column.missing_in_production", body)
        self.assertIn("analytics.orders.customer_id", body)

    def test_body_omits_informational_findings(self):
        info = {"code": "relation.not_collected", "severity": "info",
                "message": "not evaluated", "detail": {}}
        body = _body_for({}, {"attempt": 1, "payload": {"findings": [info]}}, "Fix.")
        self.assertNotIn("relation.not_collected", body)


class SubmissionTests(unittest.TestCase):
    def test_a_successful_submission_records_the_remote_review_id(self):
        store = _Store(RECORD, attempts=[ATTEMPT])
        publisher = _Publisher()
        result = _run(store, publisher)

        self.assertTrue(result["published"])
        self.assertEqual(result["remote_review_id"], "55501")
        self.assertEqual(publisher.calls[0]["pull_number"], 101)
        self.assertEqual(store._record["state"], "PUBLISHED")
        self.assertEqual(store._record["remote_review_id"], "55501")
        self.assertEqual(store.audit[0]["event_type"],
                         "review.change_request_published")

    def test_a_digit_string_review_id_is_normalized_and_published(self):
        store = _Store(RECORD, attempts=[ATTEMPT])

        result = _run(store, _Publisher(review_id="0055501"))

        self.assertTrue(result["published"])
        self.assertEqual(result["remote_review_id"], "0055501")
        self.assertEqual(store._record["remote_review_id"], "0055501")

    def test_malformed_success_is_failed_and_raises_for_retry_semantics(self):
        invalid_ids = (
            None, "", "   ", True, False, 0, -1, {}, [], "review-7", "１２３"
        )
        expected = (
            "publication identity missing; publication success cannot be verified"
        )

        for invalid_id in invalid_ids:
            with self.subTest(remote_review_id=invalid_id):
                store = _Store(RECORD, attempts=[ATTEMPT])

                with self.assertRaisesRegex(ChangeRequestError, f"^{expected}$"):
                    _run(store, _Publisher(review_id=invalid_id))

                self.assertEqual(store._record["state"], "FAILED")
                self.assertIsNone(store._record["remote_review_id"])
                self.assertEqual(store._record["failure_reason"], expected)
                self.assertEqual(store.audit[0]["event_type"],
                                 "review.change_request_failed")

    def test_a_github_failure_is_recorded_and_not_reported_as_success(self):
        store = _Store(RECORD, attempts=[ATTEMPT])
        publisher = _Publisher(fail=RuntimeError("403 Resource not accessible"))

        with self.assertRaises(ChangeRequestError):
            _run(store, publisher)

        self.assertEqual(store._record["state"], "FAILED")
        self.assertIn("403", store._record["failure_reason"])
        self.assertIsNone(store._record["remote_review_id"])
        self.assertEqual(store.audit[0]["event_type"],
                         "review.change_request_failed")

    def test_an_already_published_request_is_never_submitted_twice(self):
        published = {**RECORD, "state": "PUBLISHED", "remote_review_id": 900}
        store = _Store(published, attempts=[ATTEMPT])
        publisher = _Publisher()

        result = _run(store, publisher)

        self.assertEqual(result["status"], "already_published")
        self.assertFalse(result["published"])
        self.assertEqual(publisher.calls, [],
                         "a retry submitted a second GitHub review")

    def test_absent_credentials_leave_the_request_pending(self):
        """Nothing was attempted, so nothing failed."""
        store = _Store(RECORD, attempts=[ATTEMPT])
        result = _run(store, None)

        self.assertEqual(result["status"], "no_publisher")
        self.assertFalse(result["published"])
        self.assertEqual(store._record["state"], "PENDING")

    def test_an_unknown_request_is_reported_rather_than_raising(self):
        result = _run(_Store(None), _Publisher())
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["published"])


class PublisherContractTests(unittest.TestCase):
    def test_the_real_publisher_submits_a_request_changes_review(self):
        from agent.metadata_evidence.publishers import GitHubSlackPublisher

        class _Client:
            def __init__(self):
                self.calls = []

            def create_pull_request_review(self, owner, repo, pull_number, *,
                                           body, event):
                self.calls.append((owner, repo, pull_number, event))
                return {"id": 7}

        client = _Client()
        publisher = GitHubSlackPublisher(
            client, owner="acme", repository="analytics", expected_app_id=1)
        publisher.submit_request_changes(pull_number=12, body="please fix")

        self.assertEqual(client.calls,
                         [("acme", "analytics", 12, "REQUEST_CHANGES")])

    def test_the_null_publisher_refuses_rather_than_pretending(self):
        from agent.metadata_evidence.publishers import NullPublisher

        with self.assertRaises(RuntimeError):
            NullPublisher().submit_request_changes(pull_number=1, body="x")

    def test_the_client_rejects_an_unsupported_review_event(self):
        from agent.github_app.client import GitHubClient

        with self.assertRaises(ValueError):
            GitHubClient("token").create_pull_request_review(
                "acme", "analytics", 1, body="x", event="MERGE")


if __name__ == "__main__":
    unittest.main()
