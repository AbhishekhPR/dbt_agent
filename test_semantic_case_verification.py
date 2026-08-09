"""The review-verification stage whose absence produced an empty export.

Run 31330824658 created both fixture PRs and exported a database containing
only `schema_migrations`. The driver never waited for a review and never
called its own assertion functions, so a run that proved nothing reached
cleanup and looked like a pass.

These drive `verify_case` against a fake GitHub and a fake lifecycle store.
Nothing here touches GitHub or PostgreSQL.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "e2e"))

from live_flow import StageFailure  # noqa: E402
from test_semantic_diff_e2e_harness import HarnessTestCase  # noqa: E402

HEAD = "h" * 40
BASE = "b" * 40

BLOCK_CHANGES = [
    {"kind": "projection_expression_changed", "model_name": "fct_orders",
     "output_name": "net_order_amount",
     "before_sql": ("COALESCE(items.gross_order_amount, 0.0) - "
                    "COALESCE(refunds.refund_amount, 0.0)"),
     "after_sql": "COALESCE(items.gross_order_amount, 0.0)"},
    {"kind": "join_removed", "model_name": "fct_orders",
     "relation": "int_order_refunds", "before_join_type": "LEFT",
     "before_condition_sql": "orders.order_id = refunds.order_id"},
]
ALLOW_CHANGES = [
    {"kind": "filter_changed", "model_name": "int_customer_orders",
     "scope": "where", "before_sql": None, "after_sql": "NOT status IS NULL"},
]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class FakeStore:
    """Stands in for the lifecycle store verify_flow opens."""

    def __init__(self, reviews=(), attempts=()):
        self.reviews = [dict(r) for r in reviews]
        self.attempts = [dict(a) for a in attempts]
        self.connection = self

    def execute(self, sql, params):
        if "FROM reviews" in sql and "head_sha=%s" in sql:
            return _Rows([r for r in self.reviews if r.get("head_sha") == params[2]])
        if "FROM reviews" in sql:
            return _Rows(self.reviews)
        if "FROM review_attempts" in sql:
            return _Rows([a for a in self.attempts
                          if a.get("review_id") == params[2]
                          and a.get("attempt") == params[3]])
        return _Rows([])

    def close(self):
        pass


def fast_poll(fn, *, timeout=180, interval=3, description="condition"):
    """Two attempts, no sleeping. A sleep is never proof of anything."""
    for _ in range(2):
        value = fn()
        if value:
            return value
    raise StageFailure(f"timed out waiting for {description}")


def attempt_row(review_id, attempt, decision, health, changes, material):
    return {"review_id": review_id, "attempt": attempt, "decision": decision,
            "health": health, "lifecycle_state": "DECISION_READY",
            "semantic_evidence": {"status": "evaluated", "changes": changes},
            "payload": {"metadata": {"manifest_comparison": {
                "material_sql_changes": material}}}}


def review_row(review_id="rev-1", attempt=1, head=HEAD, base=BASE):
    return {"review_id": review_id, "attempt": attempt, "pull_number": 40,
            "base_sha": base, "head_sha": head, "base_manifest_hash": "bm",
            "head_manifest_hash": "hm", "policy_version": "1",
            "enforcement_mode": "enforce", "lifecycle_state": "DECISION_READY",
            "decision": "BLOCK"}


class CaseVerification(HarnessTestCase):
    def wire(self, reviews, attempts, *, delivery_pr=40, delivery_head=HEAD,
             delivery=None):
        d = self.driver
        d.OWNER, d.REPO_NAME = "AbhishekhPR", "relium-e2e-dbt"
        store = FakeStore(reviews, attempts)
        d.vf._store = lambda dsn: store
        d.lf.poll = fast_poll
        d.vf.poll = fast_poll
        d.vf.verify_genuine_webhook = (
            delivery or (lambda gh, jwt, since, pr: {
                "id": 900, "status_code": 202, "event": "pull_request"}))
        original = self.gh._route

        def route(method, path, body):
            if path.startswith("/app/hook/deliveries/"):
                return 200, {"request": {"payload": {"pull_request": {
                    "number": delivery_pr, "head": {"sha": delivery_head}}}}}
            return original(method, path, body)

        self.gh._route = route
        return store

    def run_case(self, case="block", pr=40):
        return self.driver.verify_case("dsn", case, pr, BASE, HEAD,
                                       "2026-01-01T00:00:00+00:00")

    # -- the paths that must succeed --------------------------------------

    def test_the_correct_block_path_succeeds(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "BLOCK", 65, BLOCK_CHANGES, [{"m": 1}])])
        record = self.run_case()
        self.assertTrue(record["assertion"]["passed"], record["assertion"]["failures"])
        self.assertEqual(record["review_id"], "rev-1")
        self.assertEqual(record["attempt"], 1)
        self.assertEqual((record["decision"], record["health"]), ("BLOCK", 65))
        self.assertEqual(record["semantic_evidence_status"], "evaluated")

    def test_the_correct_allow_path_succeeds(self):
        self.wire([review_row("rev-2")],
                  [attempt_row("rev-2", 1, "ALLOW", 100, ALLOW_CHANGES, [])])
        record = self.run_case("allow")
        self.assertTrue(record["assertion"]["passed"], record["assertion"]["failures"])
        self.assertEqual(record["material_sql_changes"], 0)

    def test_the_verified_record_carries_persisted_provenance(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "BLOCK", 65, BLOCK_CHANGES, [{"m": 1}])])
        record = self.run_case()
        for field in ("case", "pull_number", "base_sha", "head_sha", "review_id",
                      "attempt", "decision", "health", "semantic_evidence_status",
                      "semantic_changes", "genuine_delivery"):
            self.assertIn(field, record)
        self.assertTrue((self.evidence / "semantic-diff-block-verified.json").exists())

    # -- delivery correlation ---------------------------------------------

    def test_a_delivery_for_another_pull_request_does_not_satisfy_the_case(self):
        self.wire([review_row()], [], delivery_pr=41)
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("not PR #40", str(caught.exception))

    def test_a_delivery_for_another_head_sha_does_not_satisfy_the_case(self):
        self.wire([review_row()], [], delivery_head="z" * 40)
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_no_delivery_at_all_fails(self):
        def none(gh, jwt, since, pr):
            raise StageFailure("no accepted pull_request delivery")

        self.wire([review_row()], [], delivery=none)
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_a_delivery_without_an_id_cannot_be_correlated(self):
        self.wire([review_row()], [],
                  delivery=lambda gh, jwt, since, pr: {"status_code": 202})
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("no id to correlate", str(caught.exception))

    # -- review and attempt readiness --------------------------------------

    def test_a_genuine_delivery_without_a_review_fails_with_diagnostics(self):
        self.wire([], [])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        message = str(caught.exception)
        self.assertIn("no persisted review", message)
        self.assertIn("Diagnostics", message)
        self.assertTrue(
            (self.evidence / "semantic-diff-block-review-timeout.json").exists())

    def test_a_review_without_a_decided_attempt_fails(self):
        self.wire([review_row()],
                  [{"review_id": "rev-1", "attempt": 1, "decision": None,
                    "health": None, "semantic_evidence": None, "payload": {}}])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("never reached a decision", str(caught.exception))
        self.assertTrue(
            (self.evidence / "semantic-diff-block-attempt-timeout.json").exists())

    def test_a_partially_written_attempt_is_never_read_as_final(self):
        """decision present, health still NULL: not a durable verdict."""
        self.wire([review_row()],
                  [{"review_id": "rev-1", "attempt": 1, "decision": "BLOCK",
                    "health": None, "semantic_evidence": None, "payload": {}}])
        with self.assertRaises(StageFailure):
            self.run_case()

    # -- assertions actually run -------------------------------------------

    def test_a_block_review_with_the_wrong_decision_fails(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "ALLOW", 100, BLOCK_CHANGES, [])])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("assertions failed against persisted state", str(caught.exception))

    def test_a_block_review_missing_required_evidence_fails(self):
        partial = [c for c in BLOCK_CHANGES if c["kind"] != "join_removed"]
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "BLOCK", 65, partial, [{"m": 1}])])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("join_removed", str(caught.exception))

    def test_a_block_review_at_the_wrong_health_fails(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "BLOCK", 80, BLOCK_CHANGES, [{"m": 1}])])
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_an_allow_review_with_a_material_finding_fails(self):
        self.wire([review_row("rev-3")],
                  [attempt_row("rev-3", 1, "ALLOW", 100, ALLOW_CHANGES, [{"m": 1}])])
        with self.assertRaises(StageFailure):
            self.run_case("allow")

    def test_a_null_semantic_evidence_column_is_never_read_as_no_change(self):
        row = attempt_row("rev-1", 1, "BLOCK", 65, [], [{"m": 1}])
        row["semantic_evidence"] = None
        self.wire([review_row()], [row])
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_json_encoded_columns_are_decoded(self):
        """psycopg can return JSON text rather than parsed objects."""
        row = attempt_row("rev-1", 1, "BLOCK", 65, BLOCK_CHANGES, [{"m": 1}])
        row["semantic_evidence"] = json.dumps(row["semantic_evidence"])
        row["payload"] = json.dumps(row["payload"])
        self.wire([review_row()], [row])
        self.assertTrue(self.run_case()["assertion"]["passed"])

    # -- evidence and cleanup ----------------------------------------------

    def test_a_failed_case_writes_a_failure_record_not_a_verified_one(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, "ALLOW", 100, BLOCK_CHANGES, [])])
        with self.assertRaises(StageFailure):
            self.run_case()
        self.assertTrue((self.evidence / "semantic-diff-block-failed.json").exists())
        self.assertFalse((self.evidence / "semantic-diff-block-verified.json").exists())

    def test_the_run_evidence_is_not_written_merely_because_a_pr_exists(self):
        self.drive("block_pr")
        self.assertFalse((self.evidence / "semantic-diff-run.json").exists())

    def test_cleanup_still_runs_after_a_verification_failure(self):
        self.drive("block_pr")
        self.wire([], [])
        with self.assertRaises(StageFailure):
            self.run_case()
        result = self.driver.cleanup("after-verification-failure")
        self.assertTrue(result["cleanup_passed"], result["failures"])
        self.assertEqual([r for r in self.gh.refs if r.startswith("e2e/")], [])
        self.assertEqual(
            [n for n, p in self.gh.pulls.items() if p["state"] != "closed"], [])


if __name__ == "__main__":
    unittest.main()
