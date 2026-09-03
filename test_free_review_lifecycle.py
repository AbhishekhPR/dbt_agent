"""A Free review must reach a decision, not wait forever for paid evidence.

The defect
----------
A Free workspace has ``warehouse_evidence = False``. The endpoint that accepts
a warehouse snapshot refuses it with 402 before the store ever sees it, so no
collector on that workspace can ever settle a production-metadata request.

The review lifecycle did not know that. ``build_collection_plan`` derived
``metadata_required`` from manifest topology alone, ``begin_review`` created a
targeted collection request, and ``evaluate_metadata_decision`` returned
``decision=None`` with ``WAITING_FOR_METADATA`` -- waiting on an
acknowledgement that was structurally impossible. A real Free review sat in
that state indefinitely with an unacknowledged production metadata request.

What these tests pin
--------------------
1. Free + valid base/head manifests + no warehouse evidence REACHES a decision.
2. Free creates no collection request, so no acknowledgement is expected.
3. Starter and Pro keep the warehouse-evidence lifecycle exactly as it was.
4. The absent paid evidence is represented as NOT ENTITLED -- terminal and
   truthful -- rather than as an indefinitely pending requirement, and nothing
   claims production evidence was evaluated.
5. Free analysis is not degraded to make the lifecycle finish: the changed
   models, the blast radius, the targets and the health are identical to what
   an entitled workspace computes from the same manifests.

These run without PostgreSQL on purpose. The bug is in control flow, and a
regression test that skips wherever a database is absent is how it ships again.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from agent.billing.plans import PLAN_FREE, PLAN_PRO, PLAN_STARTER
from agent.billing.entitlements import FREE, PRO, STARTER, UNMETERED
from agent.metadata_evidence.review_lifecycle import begin_review
from lifecycle_store_test_support import InMemoryLifecycleStore, active_billing_row

ORG, REPO, ENV = "AcmeOrg", "analytics", "production"
BASE_SHA, HEAD_SHA = "1" * 40, "2" * 40


def _model(name, deps=(), cols=("id",)):
    return {"resource_type": "model", "name": name, "schema": "analytics",
            "alias": name, "database": "warehouse",
            "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols},
            "original_file_path": f"models/{name}.sql"}


#: A change that genuinely depends on external production state: the changed
#: model reads an unchanged upstream model and a raw source. This is the shape
#: that used to force WAITING_FOR_METADATA on every plan.
SOURCES = {"source.a.raw.subscriptions": {
    "schema": "raw", "name": "subscriptions", "database": "warehouse",
    "columns": {"subscription_id": {}, "payment_status": {}}}}


def _manifest(head=False):
    nodes = {
        "model.a.stg_payments": _model("stg_payments", ["source.a.raw.subscriptions"],
                                       ["subscription_id", "payment_status"]),
        "model.a.int_subscription_revenue": _model(
            "int_subscription_revenue",
            ["model.a.stg_payments", "source.a.raw.subscriptions"],
            ["subscription_id", "revenue"] + (["net_revenue"] if head else [])),
        "model.a.fct_customer_mrr": _model(
            "fct_customer_mrr", ["model.a.int_subscription_revenue"], ["mrr"]),
        "model.a.metric_recurring_revenue": _model(
            "metric_recurring_revenue", ["model.a.fct_customer_mrr"], ["mrr"]),
        "model.a.executive_revenue_dashboard": _model(
            "executive_revenue_dashboard", ["model.a.metric_recurring_revenue"],
            ["mrr"]),
    }
    return {"nodes": nodes, "sources": SOURCES}


BASE_MANIFEST, HEAD_MANIFEST = _manifest(), _manifest(head=True)
CHANGED = ["int_subscription_revenue"]


def _decision(entitlements, *, enforcement_mode="enforce", code_health=100):
    """The decision object itself, for the fields the outcome does not carry."""
    from agent.metadata_evidence.collection_plan import build_collection_plan
    from agent.metadata_evidence.decision import evaluate_metadata_decision

    plan = build_collection_plan(
        base_manifest=BASE_MANIFEST, head_manifest=HEAD_MANIFEST,
        changed_models=list(CHANGED),
        warehouse_evidence_entitled=entitlements.warehouse_evidence)
    return evaluate_metadata_decision(
        plan=plan.as_dict(), snapshot=None, enforcement_mode=enforcement_mode,
        code_health=code_health)


def _store(plan=None):
    """A store whose repository maps to a workspace on ``plan``."""
    if plan is None:
        return InMemoryLifecycleStore()
    return InMemoryLifecycleStore(
        tenants={(ORG, REPO): "tenant-1"},
        billing={"tenant-1": active_billing_row(plan)})


def _begin(store, *, entitlements, pull_number=1, enforcement_mode="enforce",
           head_sha=HEAD_SHA, code_health=100, semantic_evidence=None):
    return begin_review(
        store, organization_id=ORG, repository_id=REPO, environment=ENV,
        pull_number=pull_number, base_sha=BASE_SHA, head_sha=head_sha,
        base_manifest=BASE_MANIFEST, head_manifest=HEAD_MANIFEST,
        changed_models=list(CHANGED), enforcement_mode=enforcement_mode,
        code_health=code_health, entitlements=entitlements,
        semantic_evidence=semantic_evidence)


class FreeReachesADecision(unittest.TestCase):
    """1 and 2: the review finishes, and nothing is expected of a collector."""

    def setUp(self):
        self.store = _store(PLAN_FREE)
        self.outcome = _begin(self.store, entitlements=FREE)

    def test_the_review_is_not_stuck_waiting_for_metadata(self):
        self.assertNotEqual(self.outcome.lifecycle_state, "WAITING_FOR_METADATA")
        review = self.store.get_review(ORG, REPO, self.outcome.review_id)
        self.assertNotEqual(review["lifecycle_state"], "WAITING_FOR_METADATA")

    def test_a_decision_is_reached_and_persisted(self):
        self.assertIn(self.outcome.decision, {"ALLOW", "WARN", "BLOCK"})
        self.assertFalse(self.outcome.waiting)
        review = self.store.get_review(ORG, REPO, self.outcome.review_id)
        self.assertEqual(review["decision"], self.outcome.decision)

    def test_the_lifecycle_never_enters_a_metadata_wait_at_all(self):
        """Not merely "left it" - it must never pass through the wait."""
        states = [to_state for _, _, to_state, _ in self.store.transitions]
        self.assertNotIn("WAITING_FOR_METADATA", states)
        self.assertNotIn("METADATA_REQUESTED", states)

    def test_no_collection_request_is_created(self):
        self.assertIsNone(self.outcome.request_id)
        self.assertEqual(self.store.collection_requests, {})

    def test_free_does_not_require_a_collector_acknowledgement(self):
        """The review is complete with nothing outstanding against it."""
        self.assertEqual(
            self.store.requests_for_review(self.outcome.review_id), [])
        self.assertFalse(self.outcome.metadata_required)
        review = self.store.get_review(ORG, REPO, self.outcome.review_id)
        self.assertFalse(review["metadata_required"])

    def test_the_decision_reflects_code_health_rather_than_missing_evidence(self):
        healthy = _begin(_store(PLAN_FREE), entitlements=FREE, code_health=100)
        unhealthy = _begin(_store(PLAN_FREE), entitlements=FREE, code_health=40,
                           pull_number=2, head_sha="3" * 40)
        self.assertEqual(healthy.decision, "ALLOW")
        self.assertEqual(unhealthy.decision, "BLOCK")


class FreeStatesTheLimitationTruthfully(unittest.TestCase):
    """4: unavailable and not entitled, never pending, never 'evaluated'."""

    def setUp(self):
        self.store = _store(PLAN_FREE)
        self.outcome = _begin(self.store, entitlements=FREE)
        self.evidence = self.outcome.evidence
        self.findings = {f["code"]: f for f in self.outcome.findings}

    def test_production_metadata_is_not_entitled(self):
        self.assertEqual(self.evidence["production_metadata"], "NOT ENTITLED")

    def test_production_metadata_is_never_pending_or_missing(self):
        self.assertNotIn(self.evidence["production_metadata"],
                         {"PENDING", "MISSING", "STALE"})

    def test_production_evidence_is_not_claimed_to_have_been_evaluated(self):
        self.assertNotEqual(self.evidence["production_metadata"], "EVALUATED")
        # No production-category finding at all: every one of those is an
        # assertion about the warehouse, and nothing looked at the warehouse.
        production_findings = [f for f in self.outcome.findings
                               if f["category"] == "production"]
        self.assertEqual(production_findings, [])
        decision = _decision(FREE)
        self.assertFalse(decision.evidence_states_available["production"])

    def test_code_and_manifest_evidence_is_available(self):
        for source in ("base_manifest", "head_manifest", "changed_files",
                       "changed_models"):
            self.assertEqual(self.evidence[source], "EVALUATED", source)

    def test_the_finding_names_the_capability_rather_than_denying_the_dependency(self):
        finding = self.findings["metadata.not_entitled"]
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(finding["category"], "evidence")
        self.assertEqual(finding["detail"]["capability"], "warehouse_evidence")
        # The relations it would have collected are named, so the limitation
        # is specific rather than a shrug.
        self.assertIn("raw.subscriptions", finding["detail"]["requested_relations"])

    def test_it_never_claims_no_external_dependency_was_introduced(self):
        """``metadata.not_required`` would be a false statement here."""
        self.assertNotIn("metadata.not_required", self.findings)
        self.assertNotIn("metadata.pending", self.findings)

    def test_the_stored_evidence_row_is_optional_not_required(self):
        rows = self.store.evidence_states[(self.outcome.review_id,
                                           self.outcome.attempt)]
        requirement, state, group, detail = rows["production_metadata"]
        self.assertEqual(requirement, "optional")
        self.assertEqual(state, "NOT ENTITLED")
        self.assertEqual(group, "production")
        self.assertIn("not included on this workspace's plan", detail)

    def test_the_explanation_is_not_stamped_onto_unrelated_sources(self):
        rows = self.store.evidence_states[(self.outcome.review_id,
                                           self.outcome.attempt)]
        self.assertIsNone(rows["base_manifest"][3])
        self.assertEqual(rows["base_manifest"][1], "EVALUATED")

    def test_coverage_is_not_reported_as_incomplete_for_unentitled_evidence(self):
        self.assertEqual(self.outcome.coverage, "COMPLETE")
        decision = _decision(FREE)
        self.assertEqual(decision.required_missing, [])
        self.assertEqual(decision.coverage, "COMPLETE")


class FreeAnalysisIsNotDegraded(unittest.TestCase):
    """5: the same analysis, minus one input it was never entitled to."""

    def setUp(self):
        self.free = _begin(_store(PLAN_FREE), entitlements=FREE)
        self.pro = _begin(_store(PLAN_PRO), entitlements=PRO)

    def test_the_same_changed_models(self):
        self.assertEqual(self.free.plan["changed_models"],
                         self.pro.plan["changed_models"])

    def test_the_same_downstream_blast_radius(self):
        self.assertEqual(self.free.plan["downstream_models"],
                         self.pro.plan["downstream_models"])
        self.assertEqual(self.free.plan["downstream_edges"],
                         self.pro.plan["downstream_edges"])

    def test_the_same_dependency_analysis(self):
        self.assertEqual(self.free.plan["added_dependencies"],
                         self.pro.plan["added_dependencies"])
        self.assertEqual(self.free.plan["targets"], self.pro.plan["targets"])

    def test_the_same_health(self):
        self.assertEqual(self.free.health, self.pro.health)

    def test_the_targets_are_described_rather_than_deleted(self):
        """A Free plan still says which production state the change depends on."""
        external = [t for t in self.free.plan["targets"]
                    if t["dependency_kind"] == "external"]
        self.assertTrue(external)
        self.assertEqual(self.free.plan["metadata_not_required_reason"],
                         "warehouse_evidence_not_entitled")
        self.assertFalse(self.free.plan["warehouse_evidence_entitled"])


class PaidPlansAreUnchanged(unittest.TestCase):
    """3: Starter and Pro keep the warehouse-evidence lifecycle."""

    def _paid(self, plan, entitlements):
        store = _store(plan)
        return store, _begin(store, entitlements=entitlements)

    def test_starter_still_waits_for_warehouse_evidence(self):
        store, outcome = self._paid(PLAN_STARTER, STARTER)
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertIsNone(outcome.decision)
        self.assertTrue(outcome.waiting)
        self.assertTrue(outcome.metadata_required)

    def test_starter_still_gets_one_bounded_collection_request(self):
        store, outcome = self._paid(PLAN_STARTER, STARTER)
        self.assertIsNotNone(outcome.request_id)
        request = store.get_collection_request(ORG, REPO, outcome.request_id)
        self.assertIsNotNone(request)
        names = {t["relation_name"] for t in request["targets"]}
        self.assertIn("raw.subscriptions", names)

    def test_starter_production_metadata_is_pending_not_not_entitled(self):
        _, outcome = self._paid(PLAN_STARTER, STARTER)
        self.assertEqual(outcome.evidence["production_metadata"], "PENDING")

    def test_pro_still_waits_for_warehouse_evidence(self):
        _, outcome = self._paid(PLAN_PRO, PRO)
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertIsNone(outcome.decision)

    def test_a_deployment_with_no_billing_configuration_is_unmetered(self):
        """Self-hosted and pre-launch installs behave exactly as before."""
        store = _store()
        outcome = _begin(store, entitlements=UNMETERED)
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertTrue(outcome.metadata_required)

    def test_collection_stays_bounded_to_direct_downstream(self):
        """Expanding the blast radius must not expand the warehouse scan."""
        store, outcome = self._paid(PLAN_PRO, PRO)
        request = store.get_collection_request(ORG, REPO, outcome.request_id)
        collected = set(outcome.plan["collected_downstream_models"])
        self.assertEqual(collected, {"model.a.fct_customer_mrr"})
        self.assertNotIn("analytics.executive_revenue_dashboard",
                         {t["relation_name"] for t in request["targets"]})


class EntitlementsAreResolvedFromTheWorkspace(unittest.TestCase):
    """The wiring itself: no caller has to remember to pass the plan.

    The webhook runner and the lifecycle worker are separate processes, and a
    review must reach the same answer whichever one begins it. Both get there
    by ``begin_review`` resolving the plan from the store, so these exercise
    that resolution rather than an injected entitlement object.
    """

    POLAR = {
        "POLAR_ACCESS_TOKEN": "polar_oat_test",
        "POLAR_WEBHOOK_SECRET": "whsec-test",
        "POLAR_STARTER_PRODUCT_ID": "prod-starter",
        "POLAR_PRO_PRODUCT_ID": "prod-pro",
    }

    def _run(self, plan, environ):
        store = _store(plan)
        with mock.patch.dict(os.environ, environ, clear=True):
            return store, begin_review(
                store, organization_id=ORG, repository_id=REPO, environment=ENV,
                pull_number=9, base_sha=BASE_SHA, head_sha=HEAD_SHA,
                base_manifest=BASE_MANIFEST, head_manifest=HEAD_MANIFEST,
                changed_models=list(CHANGED), enforcement_mode="enforce")

    def test_a_free_workspace_resolves_to_a_decision(self):
        _, outcome = self._run(PLAN_FREE, self.POLAR)
        self.assertNotEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertIsNotNone(outcome.decision)

    def test_a_starter_workspace_resolves_to_the_metadata_wait(self):
        _, outcome = self._run(PLAN_STARTER, self.POLAR)
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")

    def test_no_polar_configuration_means_no_metering(self):
        """An unconfigured deployment must not silently downgrade to Free."""
        _, outcome = self._run(PLAN_FREE, {})
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")

    def test_a_repository_with_no_workspace_is_not_metered(self):
        """A pre-tenancy install keeps working rather than losing evidence."""
        store = InMemoryLifecycleStore()
        with mock.patch.dict(os.environ, self.POLAR, clear=True):
            outcome = begin_review(
                store, organization_id=ORG, repository_id=REPO, environment=ENV,
                pull_number=11, base_sha=BASE_SHA, head_sha=HEAD_SHA,
                base_manifest=BASE_MANIFEST, head_manifest=HEAD_MANIFEST,
                changed_models=list(CHANGED), enforcement_mode="enforce")
        self.assertEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")

    def test_a_revoked_paid_subscription_is_treated_as_free(self):
        store = InMemoryLifecycleStore(
            tenants={(ORG, REPO): "tenant-1"},
            billing={"tenant-1": {"plan": PLAN_PRO,
                                  "subscription_status": "canceled",
                                  "past_due_at": None}})
        with mock.patch.dict(os.environ, self.POLAR, clear=True):
            outcome = begin_review(
                store, organization_id=ORG, repository_id=REPO, environment=ENV,
                pull_number=12, base_sha=BASE_SHA, head_sha=HEAD_SHA,
                base_manifest=BASE_MANIFEST, head_manifest=HEAD_MANIFEST,
                changed_models=list(CHANGED), enforcement_mode="enforce")
        self.assertNotEqual(outcome.lifecycle_state, "WAITING_FOR_METADATA")
        self.assertIsNotNone(outcome.decision)


class FreeViaTheCanonicalManifestHandoff(unittest.TestCase):
    """The exact production route: webhook first, CI manifests after.

    This is how the reported review was created. The webhook arrives before CI
    has compiled target/manifest.json, the review parks in
    WAITING_FOR_MANIFEST, CI submits both exact manifests, and the lifecycle
    worker resumes it. That resume is a different call site from the direct
    path, so it needs its own proof that a Free workspace finishes.
    """

    POLAR = EntitlementsAreResolvedFromTheWorkspace.POLAR

    def _resume(self, plan):
        from agent.metadata_evidence.manifest_handoff import (
            begin_manifest_wait,
            resume_manifest_review,
        )
        from agent.metadata_evidence.review_lifecycle import review_id_for

        store = _store(plan)
        review_id = review_id_for(REPO, 21, HEAD_SHA)
        begin_manifest_wait(
            store, organization_id=ORG, repository_id=REPO, environment=ENV,
            pull_number=21, base_sha=BASE_SHA, head_sha=HEAD_SHA,
            base_manifest=BASE_MANIFEST, head_manifest=None,
            changed_files=["models/int_subscription_revenue.sql"],
            enforcement_mode="enforce", delivery_id="delivery-21")
        store.submit_manifest_evidence(
            ORG, REPO, commit_sha=HEAD_SHA, manifest=HEAD_MANIFEST,
            manifest_hash="head-hash", idempotency_key="ci-head",
            payload_hash="payload-head")
        with mock.patch.dict(os.environ, self.POLAR, clear=True):
            result = resume_manifest_review(
                store, organization_id=ORG, repository_id=REPO, environment=ENV,
                review_id=review_id, commit_sha=HEAD_SHA)
        return store, review_id, result

    def test_a_free_resume_reaches_a_decision(self):
        store, review_id, result = self._resume(PLAN_FREE)
        self.assertEqual(result["status"], "resumed")
        self.assertNotEqual(result["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertIsNotNone(result["decision"])
        self.assertIsNotNone(store.get_review(ORG, REPO, review_id)["decision"])

    def test_a_free_resume_creates_no_collection_request(self):
        store, _, _ = self._resume(PLAN_FREE)
        self.assertEqual(store.collection_requests, {})

    def test_a_free_resume_still_persists_the_semantic_comparison(self):
        """Both fixes on one review: decided, and with its SQL evidence."""
        store, _, _ = self._resume(PLAN_FREE)
        self.assertIsNotNone(store.decisions[-1]["semantic_evidence"])

    def test_a_paid_resume_still_waits(self):
        store, _, result = self._resume(PLAN_PRO)
        self.assertEqual(result["lifecycle_state"], "WAITING_FOR_METADATA")
        self.assertIsNone(result["decision"])
        self.assertNotEqual(store.collection_requests, {})

    def test_the_resume_enqueues_republication_either_way(self):
        for plan in (PLAN_FREE, PLAN_PRO):
            with self.subTest(plan=plan):
                store, _, _ = self._resume(plan)
                events = [e["event_type"] for e in store.outbox]
                self.assertIn("review.publication_reconcile_requested", events)


class EntitlementsAreNotRedefinedHere(unittest.TestCase):
    """A guard: the fix must not have quietly given Free warehouse evidence."""

    def test_free_still_excludes_warehouse_and_runtime_evidence(self):
        self.assertFalse(FREE.warehouse_evidence)
        self.assertFalse(FREE.runtime_evidence)

    def test_starter_and_pro_still_include_it(self):
        self.assertTrue(STARTER.warehouse_evidence)
        self.assertTrue(STARTER.runtime_evidence)
        self.assertTrue(PRO.warehouse_evidence)


if __name__ == "__main__":
    unittest.main()
