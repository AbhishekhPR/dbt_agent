"""SQL semantic evidence must survive the CI manifest handoff.

The defect
----------
There are two paths that begin a review, and they ran the same analysis:

  * the DIRECT path - the webhook arrives with both manifests readable from
    the repository - lifted the SQL semantic comparison out of the analysis
    result and stored it against the attempt;
  * the CI MANIFEST HANDOFF path - the webhook arrives first, CI submits the
    exact base and head manifests afterwards, and the lifecycle worker resumes
    the review - ran ``review_manifest_change``, produced the very same
    comparison, and then called ``begin_review`` without it.

So a review that took the canonical hosted-manifest route persisted SQL NULL,
and the dashboard reported that SQL semantic comparison was not available for
it. The comparison had run. Nothing carried it to storage.

These tests drive the resume path and assert the evidence arrives, and they
pin the specific SQL shape a real review reported as unavailable: a preserved
LEFT JOIN with a new WHERE predicate on the right-hand table.
"""
from __future__ import annotations

import unittest

from agent.github_app.runner import _semantic_evidence
from agent.metadata_evidence.manifest_handoff import (
    begin_manifest_wait,
    resume_manifest_review,
)
from agent.metadata_evidence.review_lifecycle import review_id_for
from agent.metadata_evidence.semantic_evidence import (
    semantic_evidence_from_incident,
)
from agent.sql_semantic_diff import compare_model_sql
from lifecycle_store_test_support import InMemoryLifecycleStore

ORG, REPO, ENV = "AcmeOrg", "analytics", "production"
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40
MODEL = "int_subscription_revenue"
MODEL_ID = f"model.a.{MODEL}"
MODEL_PATH = f"models/{MODEL}.sql"

# The exact shape reported as "SQL semantic comparison is not available": the
# LEFT JOIN is untouched and a predicate on the RIGHT-hand table appears in
# the WHERE clause.
BASE_SQL = """
select
    s.subscription_id,
    s.customer_id,
    p.amount as revenue
from {{ ref('stg_subscriptions') }} s
left join {{ ref('stg_payments') }} p
    on s.subscription_id = p.subscription_id
"""

HEAD_SQL = """
select
    s.subscription_id,
    s.customer_id,
    p.amount as revenue
from {{ ref('stg_subscriptions') }} s
left join {{ ref('stg_payments') }} p
    on s.subscription_id = p.subscription_id

where p.payment_status = 'succeeded'
"""


def _manifest(sql):
    return {
        "metadata": {"project_name": "a"},
        "nodes": {MODEL_ID: {
            "unique_id": MODEL_ID, "resource_type": "model", "name": MODEL,
            "schema": "analytics", "alias": MODEL, "database": "warehouse",
            "path": MODEL_PATH, "original_file_path": MODEL_PATH,
            "raw_code": sql, "compiled_code": sql,
            "depends_on": {"nodes": ["model.a.stg_subscriptions",
                                     "model.a.stg_payments"]},
            "columns": {c: {"name": c} for c in
                        ("subscription_id", "customer_id", "revenue")},
            "config": {"materialized": "table"}, "description": ""}},
        "sources": {}, "exposures": {}, "macros": {},
        "child_map": {}, "parent_map": {},
    }


BASE_MANIFEST, HEAD_MANIFEST = _manifest(BASE_SQL), _manifest(HEAD_SQL)


class TheAnalyzerDetectsTheChange(unittest.TestCase):
    """Within current analyzer capability, and proven rather than assumed."""

    def setUp(self):
        self.comparison = compare_model_sql(
            MODEL, BASE_SQL, HEAD_SQL, model_unique_id=MODEL_ID).to_dict()

    def test_the_comparison_is_evaluated_rather_than_unavailable(self):
        self.assertEqual(self.comparison["status"], "evaluated")

    def test_the_new_right_table_predicate_is_recorded(self):
        filters = [c for c in self.comparison["changes"]
                   if c["kind"] == "filter_changed"]
        self.assertEqual(len(filters), 1)
        change = filters[0]
        self.assertEqual(change["scope"], "where")
        self.assertIsNone(change["before_sql"])
        self.assertIn("payment_status", change["after_sql"])
        self.assertIn("succeeded", change["after_sql"])

    def test_the_change_names_the_model_it_describes(self):
        self.assertEqual(self.comparison["changes"][0]["model_name"], MODEL)
        self.assertEqual(self.comparison["changes"][0]["model_unique_id"], MODEL_ID)

    def test_the_untouched_left_join_is_not_reported_as_changed(self):
        """A false 'changed' here would cost a reviewer their trust in the rest."""
        kinds = {c["kind"] for c in self.comparison["changes"]}
        self.assertNotIn("join_removed", kinds)
        self.assertNotIn("join_type_changed", kinds)
        self.assertNotIn("join_condition_changed", kinds)

    def test_identical_sql_produces_no_changes_but_still_evaluates(self):
        same = compare_model_sql(MODEL, HEAD_SQL, HEAD_SQL).to_dict()
        self.assertEqual(same["status"], "evaluated")
        self.assertEqual(same["changes"], [])

    def test_unreadable_sql_is_unavailable_rather_than_unchanged(self):
        missing = compare_model_sql(MODEL, None, HEAD_SQL).to_dict()
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["changes"], [])


class TheHandoffPathPersistsIt(unittest.TestCase):
    """The plumbing: the worker's resume must store what its analysis found."""

    def setUp(self):
        self.store = InMemoryLifecycleStore()
        self.review_id = review_id_for(REPO, 1, HEAD_SHA)
        begin_manifest_wait(
            self.store, organization_id=ORG, repository_id=REPO, environment=ENV,
            pull_number=1, base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest=BASE_MANIFEST, head_manifest=None,
            changed_files=[MODEL_PATH], enforcement_mode="enforce",
            delivery_id="delivery-1")
        # CI submits the exact head manifest, exactly as the hosted handoff does.
        self.store.submit_manifest_evidence(
            ORG, REPO, commit_sha=HEAD_SHA, manifest=HEAD_MANIFEST,
            manifest_hash="head-hash", idempotency_key="ci-head",
            payload_hash="payload-head")
        self.result = resume_manifest_review(
            self.store, organization_id=ORG, repository_id=REPO, environment=ENV,
            review_id=self.review_id, commit_sha=HEAD_SHA)

    def _stored(self):
        return self.store.decisions[-1]["semantic_evidence"]

    def test_the_review_actually_resumed(self):
        self.assertEqual(self.result["status"], "resumed")
        self.assertTrue(self.result["applied"])

    def test_semantic_evidence_is_not_null(self):
        """The regression: this was SQL NULL, so the dashboard said unavailable."""
        self.assertIsNotNone(self._stored())

    def test_the_stored_document_is_evaluated(self):
        self.assertEqual(self._stored()["status"], "evaluated")

    def test_the_stored_document_carries_the_before_and_after_sql(self):
        changes = [c for model in self._stored()["models"]
                   for c in model["changes"] if c["kind"] == "filter_changed"]
        self.assertEqual(len(changes), 1)
        self.assertIsNone(changes[0]["before_sql"])
        self.assertIn("payment_status", changes[0]["after_sql"])

    def test_the_handoff_path_stores_what_the_direct_path_would_have(self):
        """One comparison, one document, whichever path began the review."""
        from agent.deployment_review_service import review_manifest_change

        direct = review_manifest_change(
            manifest=HEAD_MANIFEST, previous_manifest=BASE_MANIFEST,
            changed_files=[MODEL_PATH],
            deployment_id=f"github:{REPO}:{HEAD_SHA}",
            manifest_source={"base": "ci_or_committed", "head": "ci"},
            base_sha=BASE_SHA, head_sha=HEAD_SHA)
        self.assertEqual(self._stored(),
                         _semantic_evidence(direct["incident"]))

    def test_the_dashboard_projection_accepts_the_stored_document(self):
        from agent.api.routes import _semantic_evidence_view

        view = _semantic_evidence_view(self._stored())
        self.assertIsNotNone(view)
        self.assertEqual(view["status"], "evaluated")
        kinds = {change["kind"] for change in view["changes"]}
        self.assertIn("filter_changed", kinds)


class AbsenceStaysDistinguishableFromEmptiness(unittest.TestCase):
    """Storing something whenever a comparison ran must not store a lie."""

    def test_no_comparison_at_all_extracts_to_none(self):
        self.assertIsNone(semantic_evidence_from_incident({}))
        self.assertIsNone(semantic_evidence_from_incident(
            {"metadata": {"manifest_comparison": {}}}))

    def test_a_comparison_with_no_models_extracts_to_none(self):
        self.assertIsNone(semantic_evidence_from_incident(
            {"metadata": {"manifest_comparison": {
                "sql_semantic_comparison": {"status": "unavailable",
                                            "models": []}}}}))

    def test_the_runner_and_the_worker_share_one_extraction(self):
        incident = {"metadata": {"manifest_comparison": {
            "sql_semantic_comparison": {"status": "evaluated",
                                        "models": [{"model_name": MODEL,
                                                    "status": "evaluated",
                                                    "changes": []}]}}}}
        self.assertEqual(_semantic_evidence(incident),
                         semantic_evidence_from_incident(incident))


if __name__ == "__main__":
    unittest.main()
