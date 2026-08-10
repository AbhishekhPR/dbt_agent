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


def attempt_row(review_id, attempt, changes, *, state="WAITING_FOR_METADATA",
                status="evaluated", decision=None, health=65, evidence=...):
    """The real shape: metadata-waiting, decision NULL, evidence nested.

    Observed on a genuine persisted attempt -- payload carries findings and
    plan only, and semantic_evidence groups changes under models[].
    """
    row = {"review_id": review_id, "attempt": attempt, "decision": decision,
           "health": health, "lifecycle_state": state,
           "payload": {"findings": [{"code": "metadata.pending"}],
                       "plan": {"changed_models": ["fct_orders"]}}}
    row["semantic_evidence"] = (
        {"status": status, "change_count": len(changes),
         "models": [{"model_name": (changes[0].get("model_name") if changes
                                    else "fct_orders"),
                     "changes": changes}]} if evidence is ... else evidence)
    return row


def review_row(review_id="rev-1", attempt=1, head=HEAD, base=BASE):
    return {"review_id": review_id, "attempt": attempt, "pull_number": 40,
            "base_sha": base, "head_sha": head, "base_manifest_hash": "bm",
            "head_manifest_hash": "hm", "policy_version": "1",
            "enforcement_mode": "enforce", "lifecycle_state": "DECISION_READY",
            "decision": "BLOCK"}


class CaseVerification(HarnessTestCase):
    def wire(self, reviews, attempts, *, delivery_pr=40, delivery_head=HEAD,
             delivery=None, guid="guid-900", list_status=200, list_body=...,
             disposition="accepted", extra_deliveries=()):
        """Serve a deliveries list plus per-delivery details.

        The driver selects by payload, not by position, so the list may hold
        another case's delivery first -- run 31360954740 failed exactly that
        way -- and the right one must still be found.
        """
        d = self.driver
        d.OWNER, d.REPO_NAME = "AbhishekhPR", "relium-e2e-dbt"
        d.DELIVERY_TIMEOUT = 1
        store = FakeStore(reviews, attempts)
        d.vf._store = lambda dsn: store
        d.lf.poll = fast_poll
        d.vf.poll = fast_poll
        if delivery is not None:
            d.vf.verify_genuine_webhook = delivery

        details = {}
        listed = []
        for number, head, item_guid in list(extra_deliveries) + [
                (delivery_pr, delivery_head, guid)]:
            numeric = 900 + len(listed)
            listed.append({"id": numeric, "guid": item_guid,
                           "event": "pull_request", "status_code": 202,
                           "action": "opened",
                           "delivered_at": "2026-06-01T00:00:00Z"})
            details[numeric] = {
                "request": {"payload": {"pull_request": {
                    "number": number, "head": {"sha": head}}}},
                "response": {"payload": {"status": disposition}}}
        if list_body is ...:
            list_body = listed

        original = self.gh._route

        def route(method, path, body):
            if path == "/app/hook/deliveries?per_page=50":
                return list_status, list_body
            if path.startswith("/app/hook/deliveries/"):
                numeric = int(path.rsplit("/", 1)[1])
                if numeric not in details:
                    return 404, {}
                return 200, details[numeric]
            return original(method, path, body)

        self.gh._route = route
        return store

    def run_case(self, case="block", pr=40):
        return self.driver.verify_case("dsn", case, pr, BASE, HEAD,
                                       "2026-01-01T00:00:00+00:00")

    # -- the paths that must succeed --------------------------------------

    def test_the_correct_block_path_succeeds(self):
        self.wire([review_row()], [attempt_row("rev-1", 1, BLOCK_CHANGES)])
        record = self.run_case()
        self.assertTrue(record["assertion"]["passed"], record["assertion"]["failures"])
        self.assertEqual(record["review_id"], "rev-1")
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertEqual(record["semantic_evidence_status"], "evaluated")
        self.assertEqual(record["final_verdict"], "NOT_DETERMINED_AT_THIS_STAGE")

    def test_the_correct_allow_path_succeeds(self):
        self.wire([review_row("rev-2")], [attempt_row("rev-2", 1, ALLOW_CHANGES)])
        record = self.run_case("allow")
        self.assertTrue(record["assertion"]["passed"], record["assertion"]["failures"])
        self.assertEqual(record["lifecycle_state"], "WAITING_FOR_METADATA")

    def test_the_verified_record_carries_persisted_provenance(self):
        self.wire([review_row()], [attempt_row("rev-1", 1, BLOCK_CHANGES)])
        record = self.run_case()
        for field in ("case", "pull_number", "base_sha", "head_sha", "review_id",
                      "attempt", "lifecycle_state", "semantic_evidence_status",
                      "semantic_changes", "genuine_delivery", "final_verdict"):
            self.assertIn(field, record)
        self.assertTrue((self.evidence / "semantic-diff-block-verified.json").exists())

    # -- delivery correlation ---------------------------------------------

    def test_a_delivery_for_another_head_sha_does_not_satisfy_the_case(self):
        self.wire([review_row()], [], delivery_head="z" * 40)
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_the_matching_delivery_is_selected_from_several(self):
        """Regression for run 31360954740.

        The first accepted delivery in the window belonged to the earlier
        case; selecting by position matched the wrong pull request.
        """
        self.wire([review_row()], [attempt_row("rev-1", 1, BLOCK_CHANGES)],
                  extra_deliveries=[(39, "x" * 40, "guid-other")])
        record = self.run_case()
        found = record["genuine_delivery"]
        self.assertEqual(found["pull_number"], 40)
        self.assertEqual(found["head_sha"], HEAD)
        self.assertTrue(found["correlated"])

    def test_no_delivery_for_this_pull_request_fails(self):
        self.wire([review_row()], [], delivery_pr=41)
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("PR #40", str(caught.exception))

    def test_a_delivery_for_another_head_sha_is_not_matched(self):
        self.wire([review_row()], [], delivery_head="z" * 40)
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_an_unreadable_delivery_list_fails(self):
        self.wire([review_row()], [], list_status=503, list_body={})
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_a_delivery_the_application_did_not_accept_fails(self):
        self.wire([review_row()], [attempt_row("rev-1", 1, BLOCK_CHANGES)],
                  disposition="ignored")
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("not 'accepted'", str(caught.exception))

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

    def test_a_review_without_persisted_semantic_evidence_fails(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, BLOCK_CHANGES, evidence=None)])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("never reached WAITING_FOR_METADATA", str(caught.exception))
        self.assertTrue(
            (self.evidence / "semantic-diff-block-attempt-timeout.json").exists())

    def test_an_attempt_not_yet_metadata_waiting_is_not_final(self):
        """decision present, health still NULL: not a durable verdict."""
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, BLOCK_CHANGES, state="ANALYZING")])
        with self.assertRaises(StageFailure):
            self.run_case()

    # -- assertions actually run -------------------------------------------

    def test_evidence_with_an_unusable_status_fails(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, BLOCK_CHANGES, status="unavailable")])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("assertions failed against persisted state", str(caught.exception))

    def test_a_block_review_missing_required_evidence_fails(self):
        partial = [c for c in BLOCK_CHANGES if c["kind"] != "join_removed"]
        self.wire([review_row()], [attempt_row("rev-1", 1, partial)])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("join_removed", str(caught.exception))

    def test_evidence_about_an_untouched_model_fails(self):
        stray = BLOCK_CHANGES + [{"kind": "grouping_changed",
                                  "model_name": "dim_customers",
                                  "before_sql": "a", "after_sql": "b"}]
        self.wire([review_row()], [attempt_row("rev-1", 1, stray)])
        with self.assertRaises(StageFailure) as caught:
            self.run_case()
        self.assertIn("dim_customers", str(caught.exception))

    def test_a_null_decision_while_waiting_is_accepted(self):
        """The whole point: no verdict exists yet, and that is correct."""
        self.wire([review_row("rev-3")],
                  [attempt_row("rev-3", 1, ALLOW_CHANGES, decision=None,
                               health=None)])
        record = self.run_case("allow")
        self.assertTrue(record["assertion"]["passed"], record["assertion"]["failures"])
        self.assertIsNone(record["decision_at_this_stage"])
        self.assertIsNone(record["health_at_this_stage"])

    def test_no_final_verdict_is_ever_invented(self):
        self.wire([review_row()], [attempt_row("rev-1", 1, BLOCK_CHANGES)])
        record = self.run_case()
        self.assertEqual(record["final_verdict"], "NOT_DETERMINED_AT_THIS_STAGE")
        self.assertNotIn("decision", record)
        self.assertNotIn("health", record)
        for banned in ("BLOCK", "ALLOW", "WARN"):
            self.assertNotIn(banned, json.dumps(record["assertion"]))

    def test_an_allow_case_missing_its_filter_evidence_fails(self):
        self.wire([review_row("rev-4")], [attempt_row("rev-4", 1, BLOCK_CHANGES)])
        with self.assertRaises(StageFailure) as caught:
            self.run_case("allow")
        self.assertIn("filter_changed", str(caught.exception))

    def test_a_null_semantic_evidence_column_is_never_read_as_no_change(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, [], evidence=None)])
        with self.assertRaises(StageFailure):
            self.run_case()

    def test_json_encoded_columns_are_decoded(self):
        """psycopg can return JSON text rather than parsed objects."""
        row = attempt_row("rev-1", 1, BLOCK_CHANGES)
        row["semantic_evidence"] = json.dumps(row["semantic_evidence"])
        row["payload"] = json.dumps(row["payload"])
        self.wire([review_row()], [row])
        self.assertTrue(self.run_case()["assertion"]["passed"])

    # -- evidence and cleanup ----------------------------------------------

    def test_a_failed_case_writes_a_failure_record_not_a_verified_one(self):
        self.wire([review_row()],
                  [attempt_row("rev-1", 1, BLOCK_CHANGES, status="unavailable")])
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


class ManifestContract(HarnessTestCase):
    """The runner fetches the manifest from the repo; the harness must commit it.

    Run 31360420394 delivered a correlated webhook that the application
    answered "accepted", and still created no review: neither fixture branch
    carried target/manifest.json, so there was nothing to compare.
    """

    def test_the_harness_uses_the_product_manifest_path(self):
        from agent.github_app.config import DEFAULT_MANIFEST_PATH
        self.assertEqual(self.driver.MANIFEST_PATH, DEFAULT_MANIFEST_PATH)

    def test_both_branches_receive_a_manifest_commit(self):
        import inspect
        source = inspect.getsource(self.driver.main)
        commits = [line for line in source.splitlines()
                   if "MANIFEST_PATH" in line]
        self.assertGreaterEqual(len(commits), 2,
                                "base and head branches must each get a manifest")
        self.assertIn("base_manifest", source)
        self.assertIn("head_manifest", source)
