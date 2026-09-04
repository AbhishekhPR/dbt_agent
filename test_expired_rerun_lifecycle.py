"""Regression coverage for terminal metadata-refresh lifecycle reconciliation.

These tests use the real PostgreSQL store because the bug is a disagreement
between durable collection-request state and the authoritative review row. No
mock can prove the tenant-scoped SQL or the absence of a fabricated attempt.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from agent.metadata_evidence.recompute import recompute_review
from agent.metadata_evidence.review_lifecycle import validate_and_bind_snapshot
from agent.worker.lifecycle_worker import LifecycleWorker


DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "rerun-lifecycle-org"
REPO = "rerun-lifecycle-repo"
ENV = "production"
REVIEW = "review-expired-rerun"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _now():
    return datetime.now(timezone.utc)


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ExpiredRerunLifecycleTests(unittest.TestCase):
    def setUp(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        _reset_schema(DSN)
        self.store = PostgresLifecycleStore(DSN)
        self.addCleanup(self.store.close)
        self.store.ensure_tenant(ORG, REPO, ENV)

    def _review(self):
        return self.store.upsert_pr_review(
            ORG, REPO, ENV, review_id=REVIEW, pull_number=60,
            base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash="base-manifest", head_manifest_hash="head-manifest",
            enforcement_mode="enforce", policy_version="default-v1",
            policy_hash="policy-hash", metadata_required=True,
            payload={"plan": {"targets": []}},
        )

    def _decide(self, *, attempt=2, decision="WARN", coverage="COMPLETE"):
        self.store.transition_review(
            ORG, REPO, REVIEW, "METADATA_COMPLETE",
            reason="production metadata evaluated")
        self.store.record_review_decision(
            ORG, REPO, REVIEW, decision=decision,
            evidence_coverage=coverage, health=100, attempt=attempt,
            trigger="metadata_snapshot", snapshot_id=f"snapshot-{attempt}")
        self.store.transition_review(
            ORG, REPO, REVIEW, "DECISION_READY",
            reason="metadata-backed decision computed")

    def _request(self, request_id, *, expires_at, state=None):
        row = self.store.create_collection_request(
            ORG, REPO, ENV, request_id=request_id, review_id=REVIEW,
            reason="rerun", expires_at=expires_at,
            targets=[{"relation_name": "main.stg_orders",
                      "required_signals": ["row_count"],
                      "dependency_kind": "external"}],
            base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash="base-manifest",
            head_manifest_hash="head-manifest")
        if state is not None:
            row = self.store.close_collection_request(
                ORG, REPO, request_id, state=state)
        return row

    def _start_rerun(self, request_id="rerun-expired", *, expires_at=None,
                     state=None):
        row = self._request(
            request_id,
            expires_at=expires_at or (_now() - timedelta(minutes=1)),
            state=state)
        self.store.transition_review(
            ORG, REPO, REVIEW, "METADATA_REQUESTED",
            reason=f"re-run requested: {request_id}")
        return row

    def _sweep(self):
        worker = LifecycleWorker(lambda: self.store, identity="test-worker")
        worker.process_once(self.store)

    def _snapshot(self, request_id, snapshot_id="snapshot-3"):
        snapshot, _ = self.store.submit_metadata_snapshot(
            ORG, REPO, ENV, snapshot_id=snapshot_id,
            idempotency_key=f"idem-{snapshot_id}", payload_hash=f"payload-{snapshot_id}",
            evidence_hash=f"evidence-{snapshot_id}", observed_at=_now(),
            collected_at=_now(), review_id=REVIEW, request_id=request_id,
            base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest_hash="base-manifest", head_manifest_hash="head-manifest",
            completeness="COMPLETE", freshness_state="CURRENT", relations=[])
        return self.store.get_snapshot(
            ORG, REPO, snapshot["snapshot_id"], expand=False)

    def test_initial_review_returns_to_waiting_after_its_first_request_expires(self):
        self._review()
        self._start_rerun("initial-expired")

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        request = self.store.get_collection_request(ORG, REPO, "initial-expired")
        self.assertEqual(request["state"], "EXPIRED")
        self.assertEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertIsNone(review["decision"])
        self.assertEqual(self.store.review_attempts(ORG, REPO, REVIEW), [])
        transitions = self.store.review_transitions(ORG, REPO, REVIEW)
        self.assertEqual(transitions[-1]["to_state"], "WAITING_FOR_METADATA")
        audit = self.store.audit_events(ORG, REPO)
        self.assertIn(
            "review.metadata_request_reconciled",
            {row["event_type"] for row in audit},
        )

    def test_decided_review_with_live_rerun_remains_metadata_requested(self):
        self._review()
        self._decide()
        self._start_rerun(
            "rerun-live", expires_at=_now() + timedelta(minutes=30))

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        request = self.store.get_collection_request(ORG, REPO, "rerun-live")
        self.assertEqual(request["state"], "PENDING")
        self.assertEqual(review["lifecycle_state"], "METADATA_REQUESTED")
        self.assertEqual(review["decision"], "WARN")
        self.assertEqual(review["attempt"], 2)

    def test_successful_rerun_creates_a_new_authoritative_attempt(self):
        self._review()
        self._decide()
        self._start_rerun(
            "rerun-success", expires_at=_now() + timedelta(minutes=30))
        snapshot = self._snapshot("rerun-success")
        validate_and_bind_snapshot(
            self.store, organization_id=ORG, repository_id=REPO,
            environment=ENV, review_id=REVIEW, snapshot=snapshot,
            request_id="rerun-success", attempt=2)

        result = recompute_review(
            self.store, organization_id=ORG, repository_id=REPO,
            environment=ENV, review_id=REVIEW, now=_now())

        review = self.store.get_review(ORG, REPO, REVIEW)
        attempts = self.store.review_attempts(ORG, REPO, REVIEW)
        self.assertTrue(result["applied"])
        self.assertEqual([row["attempt"] for row in attempts], [2, 3])
        self.assertEqual(review["attempt"], 3)
        self.assertEqual(review["decision"], attempts[-1]["decision"])
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")

    def test_expired_rerun_restores_the_previous_authoritative_decision(self):
        self._review()
        self._decide()
        self._start_rerun()

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")
        self.assertEqual(review["decision"], "WARN")
        self.assertEqual(review["evidence_coverage"], "COMPLETE")
        self.assertEqual(review["attempt"], 2)

    def test_multiple_old_expired_requests_restore_once(self):
        self._review()
        self._decide()
        self._request(
            "rerun-expired-1", expires_at=_now() - timedelta(hours=2),
            state="EXPIRED")
        self._start_rerun(
            "rerun-expired-2", expires_at=_now() - timedelta(hours=1),
            state="EXPIRED")

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        transitions = self.store.review_transitions(ORG, REPO, REVIEW)
        restores = [row for row in transitions
                    if row["to_state"] == "DECISION_READY"
                    and row["reason"] == "metadata refresh ended without new evidence"]
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")
        self.assertEqual(len(restores), 1)

    def test_active_new_request_prevents_old_expiry_from_restoring_decision(self):
        self._review()
        self._decide()
        self._request(
            "rerun-expired-old", expires_at=_now() - timedelta(minutes=2),
            state="EXPIRED")
        self._start_rerun(
            "rerun-active-new", expires_at=_now() + timedelta(minutes=30))

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        self.assertEqual(review["lifecycle_state"], "METADATA_REQUESTED")
        self.assertEqual(
            self.store.get_collection_request(ORG, REPO, "rerun-active-new")["state"],
            "PENDING")

    def test_expiry_preserves_current_decision_and_publication_identity(self):
        self._review()
        self._decide()
        self.store.record_review_publication(
            ORG, REPO, REVIEW, comment_id=555, check_run_id=777)
        before_outbox = self.store.outbox_stats(ORG, REPO)
        self._start_rerun()

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        self.assertEqual(review["decision"], "WARN")
        self.assertEqual(review["github_comment_id"], "555")
        self.assertEqual(review["github_check_run_id"], "777")
        self.assertEqual(self.store.outbox_stats(ORG, REPO), before_outbox)

    def test_expiry_does_not_create_an_attempt(self):
        self._review()
        self._decide()
        before = self.store.review_attempts(ORG, REPO, REVIEW)
        self._start_rerun()

        self._sweep()

        after = self.store.review_attempts(ORG, REPO, REVIEW)
        self.assertEqual([row["attempt"] for row in after], [2])
        self.assertEqual(after, before)

    def test_failed_rerun_also_restores_the_previous_decision(self):
        self._review()
        self._decide()
        self._start_rerun(
            "rerun-failed", expires_at=_now() + timedelta(minutes=30),
            state="FAILED")

        self._sweep()

        review = self.store.get_review(ORG, REPO, REVIEW)
        self.assertEqual(review["lifecycle_state"], "DECISION_READY")
        self.assertEqual(review["decision"], "WARN")


if __name__ == "__main__":
    unittest.main()
