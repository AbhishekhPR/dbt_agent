"""KPI impact must survive the review that inferred it.

The defect
----------
``infer_impacted_kpis`` runs on every review with a project context. It walks
the discovered KPIs, the semantic graph and the model lineage, and reports
which KPIs a changed model reaches. That result reached the incident, was used
to derive a root cause, and was then dropped: nothing wrote it against the
review or the attempt, so the dashboard had no per-review KPI impact to show
and the frontend adapter documented the absence as "KPI impact exists per
deployment, not per pre-merge review".

Basic KPI impact is part of the Free product contract, so an inference that
runs and is then discarded is not a missing feature — it is a feature that
does all of the work and throws the answer away.

Two paths begin a review and BOTH must persist it, which is the same shape of
problem ``test_manifest_handoff_semantic_evidence.py`` pins for the SQL
comparison:

  * the DIRECT path — the webhook arrives with both manifests readable;
  * the CI MANIFEST HANDOFF path — the webhook arrives first, CI submits the
    exact manifests, and the lifecycle worker resumes the review.

What is deliberately NOT asserted
---------------------------------
Any monetary figure. Nothing in the KPI machinery observes revenue, volume or
cost, so there is no currency amount to persist and none is stored. One test
below asserts its absence, because a plausible-looking number that nobody
measured is worse than no number at all.
"""
from __future__ import annotations

import contextlib
import json
import unittest

from agent.api.routes import _kpi_impact_view
from agent.deployment_review_service import review_manifest_change
from agent.github_app.runner import PullRequestReviewRunner
from agent.metadata_evidence.kpi_impact import kpi_impact_from_incident
from agent.metadata_evidence.manifest_handoff import (
    begin_manifest_wait,
    resume_manifest_review,
)
from agent.metadata_evidence.review_lifecycle import review_id_for
from agent.metadata_evidence.service import ReviewLifecycleService
from lifecycle_store_test_support import InMemoryLifecycleStore

ORG, REPO, ENV = "AcmeOrg", "analytics", "production"
BASE_SHA, HEAD_SHA = "a" * 40, "b" * 40

# A revenue model, because KPI discovery is keyword-driven over the project's
# own names. Nothing about this scenario is hardcoded anywhere in the product:
# the inference reads whatever the customer's manifest happens to be called,
# and this fixture only has to be a project where it finds something.
MODEL = "int_subscription_revenue"
MODEL_ID = f"model.a.{MODEL}"
MODEL_PATH = f"models/{MODEL}.sql"

BASE_SQL = """
select s.subscription_id, s.customer_id, p.amount as revenue
from {{ ref('stg_subscriptions') }} s
left join {{ ref('stg_payments') }} p on s.subscription_id = p.subscription_id
"""

HEAD_SQL = BASE_SQL + "\nwhere p.payment_status = 'succeeded'\n"


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


def _analysis():
    """The analysis both review paths run, with nothing stubbed."""
    return review_manifest_change(
        manifest=HEAD_MANIFEST, previous_manifest=BASE_MANIFEST,
        changed_files=[MODEL_PATH],
        deployment_id=f"github:{REPO}:{HEAD_SHA}",
        manifest_source={"base": "ci_or_committed", "head": "ci"},
        base_sha=BASE_SHA, head_sha=HEAD_SHA)


class _Pool:
    def __init__(self, store):
        self._store = store

    @contextlib.contextmanager
    def acquire(self):
        yield self._store


class _Repository:
    owner, name = ORG, REPO


class _Event:
    repository = _Repository()
    pull_number = 7
    base_sha, head_sha = BASE_SHA, HEAD_SHA
    delivery_id = "delivery-1"


class _Config:
    enforcement_mode = "enforce"


class TheInferenceActuallyRuns(unittest.TestCase):
    """Proven, not assumed. A vacuous fixture would make every test below pass."""

    def setUp(self):
        self.document = kpi_impact_from_incident(_analysis()["incident"])

    def test_the_analysis_produces_a_kpi_impact_document(self):
        self.assertIsNotNone(self.document)
        self.assertEqual(self.document["status"], "evaluated")

    def test_it_names_the_kpis_the_change_reaches(self):
        self.assertIn("Revenue / GMV", self.document["impacted_kpis"])
        self.assertEqual(self.document["changed_models"], [MODEL])

    def test_each_impacted_kpi_says_which_model_carries_the_impact(self):
        detail = next(d for d in self.document["impacted_kpi_details"]
                      if d["name"] == "Revenue / GMV")
        self.assertEqual(detail["impacted_by_models"], [MODEL])
        self.assertTrue(detail["reasons"])
        self.assertIsInstance(detail["confidence"], int)

    def test_it_admits_where_the_evidence_stops(self):
        """Column lineage is usually unavailable, and the document says so.

        A model-level lineage claim presented as a column-level one would be
        the kind of overstatement this whole review plane exists to avoid.
        """
        self.assertEqual(self.document["fallback_reason"],
                         "changed columns unavailable")

    def test_no_monetary_figure_is_invented_anywhere_in_it(self):
        """Nothing upstream measures money, so nothing here may claim to."""
        serialised = json.dumps(self.document).lower()
        for forbidden in ("revenue_at_risk", "monetary", "usd", "dollar",
                          "arr_impact", "mrr_impact", "estimated_loss",
                          "amount_at_risk", "$"):
            self.assertNotIn(forbidden, serialised, forbidden)


class TheDirectPathPersistsIt(unittest.TestCase):
    """The webhook path, through the real runner method and the real service."""

    def setUp(self):
        self.store = InMemoryLifecycleStore()
        runner = PullRequestReviewRunner(
            storage=None,
            lifecycle=ReviewLifecycleService(_Pool(self.store),
                                             environment=ENV))
        result = _analysis()
        self.expected = kpi_impact_from_incident(result["incident"])
        runner._begin_lifecycle(
            _Event(), _Config(), manifest=HEAD_MANIFEST,
            previous_manifest=BASE_MANIFEST, result=result)

    def _stored(self):
        return self.store.decisions[-1]["kpi_impact"]

    def test_the_attempt_records_the_inference(self):
        """The regression: this was NULL, so the dashboard had nothing to show."""
        self.assertIsNotNone(self._stored())
        self.assertEqual(self._stored()["status"], "evaluated")

    def test_it_is_the_document_the_analysis_produced(self):
        self.assertEqual(self._stored(), self.expected)

    def test_it_is_bound_to_the_attempt_rather_than_the_review(self):
        """Attempt-scoped, like semantic evidence, so attempt 1's inference
        can never be shown beside attempt 2's decision."""
        row = self.store.decisions[-1]
        self.assertIn("attempt", row)
        self.assertIsNotNone(row["attempt"])


class TheCiManifestHandoffPathPersistsIt(unittest.TestCase):
    """The canonical hosted route: webhook first, manifests from CI afterwards."""

    def setUp(self):
        self.store = InMemoryLifecycleStore()
        self.review_id = review_id_for(REPO, 1, HEAD_SHA)
        begin_manifest_wait(
            self.store, organization_id=ORG, repository_id=REPO, environment=ENV,
            pull_number=1, base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest=BASE_MANIFEST, head_manifest=None,
            changed_files=[MODEL_PATH], enforcement_mode="enforce",
            delivery_id="delivery-1")
        self.store.submit_manifest_evidence(
            ORG, REPO, commit_sha=HEAD_SHA, manifest=HEAD_MANIFEST,
            manifest_hash="head-hash", idempotency_key="ci-head",
            payload_hash="payload-head")
        self.result = resume_manifest_review(
            self.store, organization_id=ORG, repository_id=REPO, environment=ENV,
            review_id=self.review_id, commit_sha=HEAD_SHA)

    def _stored(self):
        return self.store.decisions[-1]["kpi_impact"]

    def test_the_review_actually_resumed(self):
        self.assertEqual(self.result["status"], "resumed")
        self.assertTrue(self.result["applied"])

    def test_the_attempt_records_the_inference(self):
        self.assertIsNotNone(self._stored())
        self.assertEqual(self._stored()["status"], "evaluated")
        self.assertIn("Revenue / GMV", self._stored()["impacted_kpis"])

    def test_both_paths_store_the_same_document(self):
        """One inference, one document, whichever route began the review.

        This is the assertion that would have caught the semantic-evidence
        defect a release earlier, applied to KPI impact before it can happen
        again.
        """
        self.assertEqual(self._stored(),
                         kpi_impact_from_incident(_analysis()["incident"]))


class TheApiReturnsIt(unittest.TestCase):
    """What the dashboard is handed, from what was stored."""

    def setUp(self):
        self.stored = kpi_impact_from_incident(_analysis()["incident"])
        self.view = _kpi_impact_view(self.stored)

    def test_the_projection_carries_the_impacted_kpis(self):
        self.assertEqual(self.view["status"], "evaluated")
        self.assertIn("Revenue / GMV", self.view["impacted_kpis"])
        self.assertEqual(self.view["changed_models"], [MODEL])

    def test_the_projection_carries_the_per_kpi_explanation(self):
        detail = next(d for d in self.view["impacted_kpi_details"]
                      if d["name"] == "Revenue / GMV")
        self.assertEqual(detail["impacted_by_models"], [MODEL])
        self.assertTrue(detail["reasons"])

    def test_counts_agree_with_the_lists_beside_them(self):
        """Derived from what is returned, never from a stored count."""
        self.assertEqual(self.view["impacted_count"],
                         len(self.view["impacted_kpis"]))
        self.assertEqual(self.view["unaffected_count"],
                         len(self.view["unaffected_kpis"]))

    def test_it_carries_no_monetary_figure_either(self):
        serialised = json.dumps(self.view).lower()
        for forbidden in ("monetary", "usd", "revenue_at_risk", "$"):
            self.assertNotIn(forbidden, serialised, forbidden)

    def test_a_stored_key_nobody_projected_does_not_reach_the_api(self):
        """Fields are copied through explicitly, so storage cannot leak."""
        view = _kpi_impact_view({**self.stored, "internal_debug": "secret"})
        self.assertNotIn("internal_debug", view)


class HistoricalReviewsStayReadable(unittest.TestCase):
    """Every attempt written before migration 0020 reads back as NULL."""

    def test_a_legacy_attempt_projects_to_none(self):
        """Not an empty document. "Never inferred" is the honest answer."""
        self.assertIsNone(_kpi_impact_view(None))

    def test_a_row_missing_the_column_entirely_projects_to_none(self):
        self.assertIsNone(_kpi_impact_view({}.get("kpi_impact")))

    def test_junk_in_the_column_is_refused_rather_than_rendered(self):
        for junk in ("", [], 0, {"status": "something_newer"}, {"impacted_kpis": []}):
            self.assertIsNone(_kpi_impact_view(junk), junk)

    def test_a_partial_document_still_projects_without_raising(self):
        """A document from an older writer must not break the read path."""
        view = _kpi_impact_view({"status": "evaluated"})
        self.assertEqual(view["impacted_kpis"], [])
        self.assertEqual(view["impacted_kpi_details"], [])
        self.assertIsNone(view["confidence"])


class AbsenceStaysDistinguishableFromEmptiness(unittest.TestCase):
    """The reason KPI impact is a column rather than a key in ``payload``."""

    def test_an_incident_with_no_inference_extracts_to_none(self):
        self.assertIsNone(kpi_impact_from_incident({}))
        self.assertIsNone(kpi_impact_from_incident({"metadata": {}}))
        self.assertIsNone(kpi_impact_from_incident(None))

    def test_a_document_without_a_recognised_status_extracts_to_none(self):
        self.assertIsNone(kpi_impact_from_incident(
            {"metadata": {"kpi_impact": {"impacted_kpis": ["Revenue / GMV"]}}}))

    def test_an_inference_that_found_nothing_is_still_stored(self):
        """"We looked and found nothing" is an answer. NULL is not."""
        document = kpi_impact_from_incident({"metadata": {"kpi_impact": {
            "status": "evaluated", "impacted_kpis": [], "changed_models": ["m"]}}})
        self.assertIsNotNone(document)
        self.assertEqual(document["impacted_kpis"], [])
        self.assertEqual(_kpi_impact_view(document)["impacted_count"], 0)


if __name__ == "__main__":
    unittest.main()
