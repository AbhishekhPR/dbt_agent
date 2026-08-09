"""Republication of a recomputed review.

Before this, publication happened only in the synchronous webhook path, so a
review that became WARN or BLOCK from arriving production evidence kept the
neutral "waiting" check it was first published with, and Slack was never told.

The property under test is that a recomputation updates the SAME comment and
the SAME check run - never a second one - and that the Slack decision rules
are the adapter's own, unchanged.
"""
from __future__ import annotations

import unittest

from agent.metadata_evidence.publication_reconcile import (
    build_review_result,
    reconcile_publication,
)


class _RecordingPublisher:
    """Captures what would be published, at the outbound boundary."""

    def __init__(self, *, slack_publisher=None):
        self.comments = []
        self.checks = []
        self.slack_publisher = slack_publisher
        self.slack_calls = []

    def publish_comment(self, *, pull_number, body, comment_id=None):
        self.comments.append({"pull_number": pull_number, "body": body,
                              "comment_id": comment_id})
        # A real update returns the same id it was asked to edit.
        return {"id": comment_id or 5001}

    def publish_check(self, *, head_sha, payload, check_run_id=None):
        self.checks.append({"head_sha": head_sha, "payload": payload,
                            "check_run_id": check_run_id})
        return {"id": check_run_id or 9001}

    def publish_slack(self, *, publication_id, pull_number, result):
        self.slack_calls.append(result)
        if self.slack_publisher is None:
            return {"state": "disabled", "publication_id": publication_id}
        return self.slack_publisher.publish(
            publication_id=publication_id, repository="acme/analytics",
            pull_number=pull_number, result=result,
            pull_url="https://github.com/acme/analytics/pull/1")


class _FakeStore:
    """The three reads and three writes the reconciler actually uses."""

    def __init__(self, review, attempts):
        self._review = dict(review)
        self._attempts = [dict(a) for a in attempts]
        self.audit = []
        self.deliveries_recorded = []
        self.delivered = []

    def get_review(self, org, repo, review_id):
        return dict(self._review) if self._review["review_id"] == review_id else None

    def review_attempts(self, org, repo, review_id):
        return [dict(a) for a in self._attempts]

    def record_review_publication(self, org, repo, review_id, *,
                                  comment_id=None, check_run_id=None):
        if comment_id:
            self._review["github_comment_id"] = str(comment_id)
        if check_run_id:
            self._review["github_check_run_id"] = str(check_run_id)
        return dict(self._review)

    def record_delivery(self, org, repo, env, *, channel, event_key, payload):
        entry = {"journal_id": f"j-{channel}-{event_key}", "channel": channel,
                 "event_key": event_key, "payload": payload}
        self.deliveries_recorded.append(entry)
        return entry

    def mark_delivered(self, org, repo, journal_id, *, remote_id):
        self.delivered.append({"journal_id": journal_id, "remote_id": remote_id})

    def append_audit(self, org, repo, **kwargs):
        self.audit.append(kwargs)


def _review(**overrides):
    review = {
        "review_id": "gh-abc123", "environment": "production", "pull_number": 41,
        "head_sha": "b" * 40, "base_sha": "a" * 40,
        "enforcement_mode": "enforce", "decision": "WARN",
        "github_comment_id": "5001", "github_check_run_id": "9001",
        "payload": {"plan": {"changed_models": ["fct_orders"]}},
    }
    review.update(overrides)
    return review


def _attempt(attempt=2, decision="WARN", findings=(), **overrides):
    record = {
        "attempt": attempt, "decision": decision, "evidence_coverage": "COMPLETE",
        "health": 90, "lifecycle_state": "METADATA_COMPLETE",
        "enforcement_mode": "enforce", "trigger": "metadata_snapshot",
        "payload": {"findings": list(findings)},
    }
    record.update(overrides)
    return record


_NULL_FINDING = {
    "code": "column.high_null_rate", "severity": "warn", "category": "production",
    "message": "analytics.orders.customer_id is 33% NULL in production.",
    "relation": "analytics.orders", "column": "customer_id",
    "detail": {"null_rate": 0.33, "threshold": 0.2},
}
_MISSING_FINDING = {
    "code": "column.missing_in_production", "severity": "block",
    "category": "production",
    "message": "analytics.orders.customer_id does not exist in production.",
    "relation": "analytics.orders", "column": "customer_id", "detail": {},
}


class ResultShapeTests(unittest.TestCase):
    def test_measured_value_and_threshold_reach_the_published_body(self):
        result = build_review_result(_review(), _attempt(findings=[_NULL_FINDING]))
        self.assertEqual(result["decision"], "WARN")
        self.assertTrue(result["final"])
        finding = result["material_findings"][0]
        self.assertIn("null_rate=0.33", finding["recommended_fix"])
        self.assertIn("threshold=0.2", finding["recommended_fix"])
        self.assertIn("analytics.orders.customer_id", finding["title"])

    def test_info_findings_are_not_published_as_material(self):
        info = {"code": "dependency.head_derived_absent_ok", "severity": "info",
                "category": "production", "message": "expected", "detail": {}}
        result = build_review_result(_review(), _attempt(findings=[info]))
        self.assertEqual(result["material_findings"], [])

    def test_severity_follows_the_decision(self):
        for decision, severity in (("BLOCK", "HIGH"), ("WARN", "MEDIUM"),
                                   ("ALLOW", "LOW")):
            result = build_review_result(_review(decision=decision),
                                         _attempt(decision=decision))
            self.assertEqual(result["incident"]["severity"], severity)


class ReconciliationTests(unittest.TestCase):
    def _run(self, store, publisher, **kwargs):
        return reconcile_publication(
            store, organization_id="acme", repository_id="analytics",
            environment="production", review_id="gh-abc123",
            publisher=publisher, **kwargs)

    def test_existing_comment_and_check_are_updated_not_duplicated(self):
        store = _FakeStore(_review(), [_attempt(findings=[_NULL_FINDING])])
        publisher = _RecordingPublisher()

        outcome = self._run(store, publisher)

        self.assertTrue(outcome["published"])
        self.assertTrue(outcome["comment_reused"], "a second comment was created")
        self.assertTrue(outcome["check_reused"], "a second check run was created")
        self.assertEqual(publisher.comments[0]["comment_id"], "5001")
        self.assertEqual(publisher.checks[0]["check_run_id"], "9001")
        self.assertEqual(outcome["comment_id"], "5001")
        self.assertEqual(outcome["check_run_id"], "9001")

    def test_check_conclusion_tracks_the_recomputed_decision(self):
        cases = [("ALLOW", "enforce", "success"),
                 ("WARN", "enforce", "neutral"),
                 ("BLOCK", "enforce", "failure"),
                 ("BLOCK", "shadow", "neutral")]
        for decision, mode, expected in cases:
            store = _FakeStore(_review(enforcement_mode=mode),
                               [_attempt(decision=decision, enforcement_mode=mode)])
            outcome = self._run(store, _RecordingPublisher())
            self.assertEqual(outcome["check_conclusion"], expected,
                             f"{decision}/{mode}")

    def test_head_sha_is_not_sent_on_an_update(self):
        """GitHub rejects head_sha on a check-run update."""
        from agent.metadata_evidence.publishers import GitHubSlackPublisher

        class _Client:
            def __init__(self):
                self.updates = []

            def update_check_run(self, owner, repo, check_run_id, payload):
                self.updates.append(payload)
                return {"id": check_run_id}

        client = _Client()
        publisher = GitHubSlackPublisher(client, owner="acme", repository="analytics",
                                         expected_app_id=1)
        publisher.publish_check(head_sha="b" * 40,
                                payload={"name": "x", "head_sha": "b" * 40,
                                         "conclusion": "failure"},
                                check_run_id=9001)
        self.assertNotIn("head_sha", client.updates[0])
        self.assertEqual(client.updates[0]["conclusion"], "failure")

    def test_first_publication_creates_when_no_identity_is_known(self):
        store = _FakeStore(_review(github_comment_id=None, github_check_run_id=None),
                           [_attempt()])
        outcome = self._run(store, _RecordingPublisher())
        self.assertFalse(outcome["comment_reused"])
        self.assertFalse(outcome["check_reused"])
        self.assertEqual(outcome["comment_id"], "5001")

    def test_a_waiting_review_publishes_nothing(self):
        store = _FakeStore(_review(decision=None),
                           [_attempt(attempt=1, decision=None)])
        publisher = _RecordingPublisher()
        outcome = self._run(store, publisher)

        self.assertEqual(outcome["status"], "no_decision_yet")
        self.assertFalse(outcome["published"])
        self.assertEqual(publisher.comments, [])
        self.assertEqual(publisher.checks, [])

    def test_absent_publisher_is_recorded_as_unpublished(self):
        store = _FakeStore(_review(), [_attempt()])
        outcome = self._run(store, None)

        self.assertEqual(outcome["status"], "no_publisher")
        self.assertFalse(outcome["published"])
        self.assertEqual(store.audit[0]["event_type"], "review.publication_skipped")

    def test_delivery_journal_records_both_channels(self):
        store = _FakeStore(_review(), [_attempt()])
        self._run(store, _RecordingPublisher())
        channels = {d["channel"] for d in store.deliveries_recorded}
        self.assertEqual(channels, {"github", "slack"})
        for entry in store.deliveries_recorded:
            self.assertIn("attempt-2", entry["event_key"])

    def test_a_suppressed_slack_alert_is_not_journalled_as_delivered(self):
        """A policy-suppressed alert must not look like one that was sent.

        The Slack outcome carries a publication id even when nothing was sent,
        so marking delivery on the id alone made the dashboard report a
        PUBLISHED alert for every WARN and ALLOW - alerts nobody received.
        """
        sink, sent = SlackRuleTests()._sink(notify_warn=False)
        store = _FakeStore(_review(decision="ALLOW"),
                           [_attempt(decision="ALLOW")])
        outcome = self._run(store, _RecordingPublisher(slack_publisher=sink))

        self.assertEqual(outcome["slack"]["state"], "skipped")
        self.assertEqual(sent, [], "nothing should have been sent")

        slack_journal = [d for d in store.deliveries_recorded
                         if d["channel"] == "slack"]
        self.assertEqual(len(slack_journal), 1)
        self.assertEqual(slack_journal[0]["payload"]["state"], "skipped")
        self.assertNotIn("j-slack", [d["journal_id"] for d in store.delivered],
                         "a suppressed alert was marked delivered")

    def test_a_sent_slack_alert_is_journalled_as_delivered(self):
        sink, sent = SlackRuleTests()._sink(notify_warn=False)
        store = _FakeStore(_review(decision="BLOCK"),
                           [_attempt(decision="BLOCK",
                                     findings=[_MISSING_FINDING])])
        outcome = self._run(store, _RecordingPublisher(slack_publisher=sink))

        self.assertEqual(outcome["slack"]["state"], "complete")
        self.assertEqual(len(sent), 1)
        delivered_ids = [d["journal_id"] for d in store.delivered]
        self.assertTrue(any(i.startswith("j-slack") for i in delivered_ids),
                        f"slack delivery not journalled: {delivered_ids}")

    def test_a_named_attempt_can_be_republished(self):
        store = _FakeStore(_review(), [_attempt(attempt=2), _attempt(attempt=3,
                                                                     decision="BLOCK")])
        outcome = self._run(store, _RecordingPublisher(), attempt=2)
        self.assertEqual(outcome["attempt"], 2)
        self.assertEqual(outcome["decision"], "WARN")


class SlackRuleTests(unittest.TestCase):
    """The adapter's rules must be used exactly as they are."""

    def _sink(self, *, notify_warn):
        from agent.github_app.slack import SlackPublicationSink

        sent = []

        class _Response:
            status = 200

            def read(self):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            sent.append(request.data)
            return _Response()

        sink = SlackPublicationSink("https://hooks.example.invalid/T/B/X",
                                    notify_warn=notify_warn, opener=opener,
                                    sleep=lambda _s: None)
        return sink, sent

    def _publish(self, decision, *, notify_warn):
        sink, sent = self._sink(notify_warn=notify_warn)
        store = _FakeStore(_review(decision=decision),
                           [_attempt(decision=decision,
                                     findings=[_MISSING_FINDING]
                                     if decision == "BLOCK" else [_NULL_FINDING])])
        publisher = _RecordingPublisher(slack_publisher=sink)
        outcome = reconcile_publication(
            store, organization_id="acme", repository_id="analytics",
            environment="production", review_id="gh-abc123", publisher=publisher)
        return outcome["slack"], sent

    def test_block_always_alerts(self):
        slack, sent = self._publish("BLOCK", notify_warn=False)
        self.assertEqual(slack["state"], "complete")
        self.assertEqual(len(sent), 1)

    def test_warn_alerts_only_when_configured(self):
        quiet, quiet_sent = self._publish("WARN", notify_warn=False)
        self.assertEqual(quiet["state"], "skipped")
        self.assertEqual(quiet["reason"], "decision_not_configured_for_slack")
        self.assertEqual(quiet_sent, [])

        loud, loud_sent = self._publish("WARN", notify_warn=True)
        self.assertEqual(loud["state"], "complete")
        self.assertEqual(len(loud_sent), 1)

    def test_allow_never_alerts(self):
        slack, sent = self._publish("ALLOW", notify_warn=True)
        self.assertEqual(slack["state"], "skipped")
        self.assertEqual(slack["reason"], "decision_not_configured_for_slack")
        self.assertEqual(sent, [])

    def test_slack_payload_carries_no_finding_detail_beyond_the_adapter(self):
        _, sent = self._publish("BLOCK", notify_warn=False)
        body = sent[0].decode("utf-8").lower()
        for needle in ("postgresql://", "password", "select ", "-----begin"):
            self.assertNotIn(needle, body)


if __name__ == "__main__":
    unittest.main()
