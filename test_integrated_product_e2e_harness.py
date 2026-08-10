"""The integrated product E2E harness, driven against fakes.

No test here touches GitHub, PostgreSQL or the network. The driver is
orchestration over already-proven helpers, so what needs proving is the
orchestration itself: does every assertion actually fail when the product
would be wrong, and does cleanup still run when a stage dies.

The fakes mirror shapes observed in real runs — attempt rows as
`review_attempts` returns them, comparisons as `_metadata_comparison_view`
projects them, plans as `collection_plan` persists them. A fake that invented
its own shape would let the harness pass against data the product never
produces.

Every failure case asserts StageFailure specifically, not "an exception":
an AttributeError that happens to abort the run is not the driver noticing a
product defect.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "e2e"))

from live_flow import StageFailure  # noqa: E402


def load_driver(evidence_dir):
    """Import the driver with a private evidence directory per test."""
    argv = sys.argv
    sys.argv = ["integrated_product_e2e.py", str(evidence_dir)]
    try:
        module = importlib.import_module("integrated_product_e2e")
        return importlib.reload(module)
    finally:
        sys.argv = argv


class DriverTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self._tmp.name)
        self.d = load_driver(self.evidence)
        self.addCleanup(self._tmp.cleanup)


# ------------------------------------------------------------- fixtures

SNAP_A = "snap-aaaaaaaaaaaaaaaa"
SNAP_B = "snap-bbbbbbbbbbbbbbbb"


def comparison(*, baseline=SNAP_A, current=SNAP_B, status="evaluated",
               drop=(), extra=()):
    """A comparison shaped exactly as the API projection produces one."""
    changes = [
        {"kind": "row_count_changed", "model": "model.a.raw_orders",
         "relation": "raw.orders", "column": None, "signal": "row_count",
         "before": 1000, "after": 800, "absolute_delta": -200,
         "relative_delta": -0.2},
        {"kind": "null_rate_changed", "model": "model.a.raw_orders",
         "relation": "raw.orders", "column": "discount_amount",
         "signal": "null_rate", "before": 0.01, "after": 0.82,
         "percentage_point_delta": 81.0},
        {"kind": "cardinality_changed", "model": "model.a.raw_orders",
         "relation": "raw.orders", "column": "discount_amount",
         "signal": "cardinality", "before": 0.37, "after": 0.42,
         "percentage_point_delta": 5.0},
    ]
    changes = [c for c in changes if c["signal"] not in drop] + list(extra)
    return {"status": status, "baseline_snapshot_id": baseline,
            "current_snapshot_id": current,
            "baseline_observed_at": "2026-08-10T00:00:00+00:00",
            "current_observed_at": "2026-08-10T06:00:00+00:00",
            "changes": changes, "change_count": len(changes),
            "coverage": {"relations_observed": 1, "relations_compared": 1}}


def semantic_evidence(models=("int_customer_orders",), status="evaluated",
                      kind="filter_changed", scope="where", extra=()):
    """Shaped as sql_semantic_diff emits it: filter_changed carries a scope."""
    return {"status": status,
            "models": [{"model_name": name, "status": "evaluated",
                        "changes": [{"kind": kind, "model_name": name,
                                     "scope": scope,
                                     "before_sql": "status is not null",
                                     "after_sql": "status is not null and x"}]
                        + list(extra)}
                       for name in models]}


def attempt(*, number=2, health=100, findings=(), comparison_doc=None,
            semantic=None, decision=None):
    return {"attempt": number, "health": health, "decision": decision,
            "snapshot_id": SNAP_B,
            "payload": {"findings": list(findings)},
            "semantic_evidence": semantic,
            "metadata_comparison": comparison_doc}


NULL_RATE_FINDING = {"code": "column.high_null_rate", "category": "production",
                     "severity": "medium"}

MANIFEST = {"nodes": {
    "model.a.int_customer_orders": {
        "resource_type": "model", "name": "int_customer_orders",
        "depends_on": {"nodes": ["model.a.stg_orders"]}},
    "model.a.dim_customers": {
        "resource_type": "model", "name": "dim_customers",
        "depends_on": {"nodes": ["model.a.int_customer_orders"]}},
    "model.a.customer_lifetime_value": {
        "resource_type": "model", "name": "customer_lifetime_value",
        "depends_on": {"nodes": ["model.a.int_customer_orders"]}},
    "model.a.exec_daily_kpis": {
        "resource_type": "model", "name": "exec_daily_kpis",
        # Transitive only: must never appear in direct blast radius.
        "depends_on": {"nodes": ["model.a.dim_customers"]}},
}}


# ------------------------------------------------------- baseline / A

class BaselineTests(DriverTestCase):
    def test_missing_baseline_fails(self):
        """no_baseline means A never existed; the run must not pass."""
        with self.assertRaises(StageFailure):
            self.d.assert_comparison(
                comparison(status="no_baseline", baseline=None), SNAP_A, SNAP_B)

    def test_comparison_against_a_different_baseline_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(
                comparison(baseline="snap-someone-else"), SNAP_A, SNAP_B)
        self.assertIn("baseline", str(caught.exception))

    def test_baseline_from_another_environment_cannot_be_accepted(self):
        """A baseline the engine did not choose is a different snapshot id."""
        with self.assertRaises(StageFailure):
            self.d.assert_comparison(
                comparison(baseline="snap-staging-observation"), SNAP_A, SNAP_B)

    def test_exact_a_to_b_binding_passes(self):
        proof = self.d.assert_comparison(comparison(), SNAP_A, SNAP_B)
        self.assertEqual(proof["baseline_snapshot_id"], SNAP_A)
        self.assertEqual(proof["current_snapshot_id"], SNAP_B)


# ------------------------------------------------------------ webhook

class DeliveryTests(DriverTestCase):
    def test_a_delivery_for_another_pull_request_is_not_accepted(self):
        """Payload correlation, not position: the driver delegates to the
        proven selector, so the guard must be the one that rejects PR #44
        when PR #45 is under test."""
        import semantic_diff_e2e as sd

        seen = {}

        def fake(since, pr_number, head_sha):
            seen.update({"pr": pr_number, "head": head_sha})
            raise StageFailure(
                f"delivery is for PR #44 {'x' * 4}, not PR #{pr_number}")

        original = sd._poll_for_correlated_delivery
        sd._poll_for_correlated_delivery = fake
        try:
            with self.assertRaises(StageFailure) as caught:
                self.d._poll_correlated_delivery("2026-01-01T00:00:00+00:00",
                                                 45, "b" * 40)
        finally:
            sd._poll_for_correlated_delivery = original
        self.assertEqual(seen["pr"], 45)
        self.assertIn("not PR #45", str(caught.exception))


# ----------------------------------------------------------- semantic

class SemanticTests(DriverTestCase):
    def test_missing_semantic_evidence_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_semantic(attempt(semantic=None))

    def test_empty_semantic_evidence_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_semantic(attempt(
                semantic={"status": "evaluated", "models": []}))
        self.assertIn("empty", str(caught.exception))

    def test_evidence_about_an_untouched_model_fails(self):
        """The required change is present, but a model the fixture never
        edited also carries evidence. That is unexplained and must fail."""
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_semantic(attempt(semantic=semantic_evidence(
                models=("int_customer_orders", "fct_orders"))))
        self.assertIn("untouched", str(caught.exception))
        self.assertIn("fct_orders", str(caught.exception))

    def test_evidence_only_about_an_untouched_model_also_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_semantic(attempt(
                semantic=semantic_evidence(models=("fct_orders",))))

    def test_real_semantic_evidence_passes(self):
        proof = self.d.assert_semantic(attempt(semantic=semantic_evidence()))
        self.assertEqual(proof["models"], ["int_customer_orders"])
        self.assertEqual(proof["change_kinds"], ["filter_changed"])
        self.assertTrue(proof["required_change_present"])

    # -- the pinned expectation ------------------------------------------

    def test_a_different_change_kind_fails(self):
        """Non-empty is not enough: the fixture edits a WHERE clause."""
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_semantic(attempt(
                semantic=semantic_evidence(kind="grouping_changed")))
        self.assertIn("filter_changed", str(caught.exception))

    def test_a_filter_change_in_the_wrong_scope_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_semantic(attempt(
                semantic=semantic_evidence(scope="having")))
        self.assertIn("scope", str(caught.exception))

    def test_the_required_change_on_the_wrong_model_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_semantic(attempt(
                semantic=semantic_evidence(models=("dim_customers",))))

    def test_additional_truthful_evidence_on_the_same_model_is_allowed(self):
        proof = self.d.assert_semantic(attempt(semantic=semantic_evidence(
            extra=[{"kind": "projection_expression_changed",
                    "model_name": "int_customer_orders",
                    "output_name": "order_total"}])))
        self.assertEqual(proof["change_count"], 2)
        self.assertTrue(proof["required_change_present"])

    def test_the_pinned_expectation_matches_the_fixture_and_the_engine(self):
        """kind/model/scope must be what the fixture and engine really do."""
        import semantic_fixtures as sf

        self.assertEqual(self.d.REQUIRED_SEMANTIC_CHANGE["model_name"],
                         sf.ALLOW_MUTATED_MODELS[0])
        self.assertEqual(self.d.REQUIRED_SEMANTIC_CHANGE["kind"], "filter_changed")
        # sql_semantic_diff emits scope="where" for a WHERE clause change.
        engine = (REPO_ROOT / "agent" / "sql_semantic_diff.py").read_text(
            encoding="utf-8")
        self.assertIn('("where", "where")', engine)
        self.assertEqual(self.d.REQUIRED_SEMANTIC_CHANGE["scope"], "where")


# -------------------------------------------------------- blast radius

class BlastRadiusTests(DriverTestCase):
    #: Node ids, exactly as collection_plan persists them.
    DIRECT_IDS = ["model.a.customer_lifetime_value", "model.a.dim_customers"]

    def test_expectation_is_derived_from_the_parsed_manifest(self):
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        self.assertEqual(expectation["direct_downstream_models"],
                         self.DIRECT_IDS)
        self.assertNotIn("model.a.exec_daily_kpis",
                         expectation["direct_downstream_models"])

    def test_the_expectation_is_node_ids_not_names(self):
        """REGRESSION for run 31394411123.

        `collection_plan` stores `downstream.add(node_id)`, so the persisted
        set is dbt node ids. Deriving names made the run fail with
        `['model.relium_e2e_dbt.dim_customers'] != ['dim_customers']` - the
        right set in the wrong identity. Names are still reported, but only as
        a human-readable companion.
        """
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        for identity in expectation["direct_downstream_models"]:
            self.assertTrue(identity.startswith("model."), identity)
        self.assertEqual(expectation["direct_downstream_names"],
                         ["customer_lifetime_value", "dim_customers"])
        # The exact shape the product persists must satisfy the expectation.
        proof = self.d.assert_blast_radius(
            {"downstream_models": self.DIRECT_IDS}, expectation)
        self.assertEqual(proof["direct_downstream_models"], self.DIRECT_IDS)

    def test_bare_names_from_the_product_would_fail(self):
        """If the product ever switched to names, this must not pass silently."""
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        with self.assertRaises(StageFailure):
            self.d.assert_blast_radius(
                {"downstream_models": ["customer_lifetime_value",
                                       "dim_customers"]}, expectation)

    def test_wrong_blast_radius_fails(self):
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        with self.assertRaises(StageFailure):
            self.d.assert_blast_radius(
                {"downstream_models": ["model.a.dim_customers"]}, expectation)

    def test_transitive_expansion_fails(self):
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        with self.assertRaises(StageFailure):
            self.d.assert_blast_radius(
                {"downstream_models": self.DIRECT_IDS
                 + ["model.a.exec_daily_kpis"]}, expectation)

    def test_exact_direct_set_passes(self):
        expectation = self.d.expected_direct_downstream(
            MANIFEST, "int_customer_orders")
        proof = self.d.assert_blast_radius(
            {"downstream_models": list(reversed(self.DIRECT_IDS))}, expectation)
        self.assertFalse(proof["transitive_expansion"])

    def test_the_identity_matches_what_collection_plan_persists(self):
        """Cross-check against the product, not against this harness."""
        plan = (REPO_ROOT / "agent" / "metadata_evidence"
                / "collection_plan.py").read_text(encoding="utf-8")
        self.assertIn("downstream.add(node_id)", plan)
        self.assertIn("plan.downstream_models = sorted(downstream)", plan)


class ObservationFreshnessTests(DriverTestCase):
    """REGRESSION for run 31394411123's second, masked defect.

    Observation A was backdated six hours against a one-hour TTL, so
    `classify_freshness` correctly returned STALE and the baseline review
    recomputed to METADATA_STALE / BLOCK. The product was right; the fixture
    was asking it to treat a six-hour-old observation as current.
    """

    def test_the_backdate_is_well_inside_the_declared_ttl(self):
        self.assertLess(self.d.A_BACKDATE_SECONDS,
                        self.d.OBSERVATION_TTL_SECONDS)
        # Comfortably inside, not marginally: clock skew must not decide this.
        self.assertLess(self.d.A_BACKDATE_SECONDS,
                        self.d.OBSERVATION_TTL_SECONDS / 4)

    def test_the_backdate_still_orders_a_strictly_before_b(self):
        self.assertGreater(self.d.A_BACKDATE_SECONDS, 0)

    def test_a_would_not_be_classified_stale(self):
        """Run the REAL classifier over the body the driver actually sends."""
        from datetime import datetime, timedelta, timezone

        from agent.metadata_evidence.decision import classify_freshness

        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        observed = now - timedelta(seconds=self.d.A_BACKDATE_SECONDS)
        review = {"review_id": "r", "attempt": 1, "base_sha": "a" * 40,
                  "head_sha": "b" * 40, "base_manifest_hash": "bh",
                  "head_manifest_hash": "hh"}
        rel, col = self.d.select_carrier(REQUEST)
        body = self.d.observation_from_request(
            review, REQUEST, carrier_relation=rel, carrier_column=col,
            row_count=1000, null_rate=0.01, cardinality=0.37,
            observed_at=observed)
        snapshot = {"observed_at": observed,
                    "ttl_seconds": body["ttl_seconds"],
                    "relations": body["relations"]}
        self.assertEqual(classify_freshness(snapshot, now=now), "CURRENT")

    def test_a_six_hour_backdate_would_have_been_stale(self):
        """The exact condition that produced METADATA_STALE."""
        from datetime import datetime, timedelta, timezone

        from agent.metadata_evidence.decision import classify_freshness

        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        stale = {"observed_at": now - timedelta(hours=6),
                 "ttl_seconds": self.d.OBSERVATION_TTL_SECONDS,
                 "relations": []}
        self.assertEqual(classify_freshness(stale, now=now), "STALE")

    def test_a_model_with_no_consumers_cannot_prove_blast_radius(self):
        with self.assertRaises(StageFailure):
            self.d.expected_direct_downstream(MANIFEST, "exec_daily_kpis")


# ----------------------------------------------------- metadata request

class MetadataRequestTests(DriverTestCase):
    def test_absent_request_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_metadata_request({})

    def test_unbounded_request_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_metadata_request(
                {"request_id": "req-1", "bounded": False})

    def test_raw_row_request_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_metadata_request(
                {"request_id": "req-1", "bounded": True,
                 "required_signals": ["row_count", "raw_rows"]})
        self.assertIn("prohibited", str(caught.exception))

    def test_arbitrary_sql_request_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_metadata_request(
                {"request_id": "req-1", "bounded": True,
                 "required_signals": ["arbitrary_sql"]})

    def test_bounded_targeted_request_passes(self):
        proof = self.d.assert_metadata_request(
            {"request_id": "req-1", "bounded": True,
             "relations": ["raw.orders"], "columns": ["discount_amount"],
             "required_signals": ["row_count", "null_rate"]})
        self.assertFalse(proof["raw_row_request"])


# -------------------------------------------------------- comparison

class ComparisonTests(DriverTestCase):
    def test_missing_comparison_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_comparison(None, SNAP_A, SNAP_B)

    def test_a_missing_expected_delta_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(comparison(drop=("row_count",)),
                                     SNAP_A, SNAP_B)
        self.assertIn("row_count", str(caught.exception))

    def test_a_missing_cardinality_delta_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(comparison(drop=("cardinality",)),
                                     SNAP_A, SNAP_B)
        self.assertIn("cardinality", str(caught.exception))

    def test_percentage_point_delta_must_be_points_not_percent(self):
        wrong = comparison(drop=("null_rate",), extra=[{
            "kind": "null_rate_changed", "relation": "raw.orders",
            "column": "discount_amount", "signal": "null_rate",
            "before": 0.01, "after": 0.82,
            # 8100% as a percent change; not what was measured.
            "percentage_point_delta": 8100.0}])
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(wrong, SNAP_A, SNAP_B)
        self.assertIn("points", str(caught.exception))

    def test_an_unchanged_signal_reported_as_a_change_fails(self):
        noisy = comparison(extra=[{
            "kind": "schema_fingerprint_changed", "relation": "raw.orders",
            "column": None, "signal": "schema_fingerprint",
            "before": "fp-integrated-e2e", "after": "fp-integrated-e2e"}])
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(noisy, SNAP_A, SNAP_B)
        self.assertIn("schema_fingerprint", str(caught.exception))

    def test_the_unchanged_signal_is_recorded_for_phase_b(self):
        proof = self.d.assert_comparison(comparison(), SNAP_A, SNAP_B)
        self.assertEqual(proof["unchanged_signal_for_phase_b"],
                         "schema_fingerprint")
        self.assertNotIn("schema_fingerprint", proof["signals_changed"])


# ------------------------------------------------- comparison is evidence

class EvidenceOnlyTests(DriverTestCase):
    def test_a_comparison_shaped_finding_fails(self):
        final = attempt(findings=[NULL_RATE_FINDING,
                                  {"code": "metadata_comparison.row_count_drop",
                                   "category": "production"}])
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison_is_evidence_only(attempt(health=100), final)
        self.assertIn("comparison produced policy findings",
                      str(caught.exception))

    def test_a_drift_finding_fails(self):
        final = attempt(findings=[{"code": "metadata.drift_detected"}])
        with self.assertRaises(StageFailure):
            self.d.assert_comparison_is_evidence_only(attempt(health=100), final)

    def test_health_other_than_the_policy_expectation_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison_is_evidence_only(
                attempt(health=100), attempt(health=80,
                                             findings=[NULL_RATE_FINDING]))
        self.assertIn("expected 100", str(caught.exception))

    def test_evidence_only_comparison_passes(self):
        proof = self.d.assert_comparison_is_evidence_only(
            attempt(health=100), attempt(health=100, findings=[NULL_RATE_FINDING]))
        self.assertEqual(proof["comparison_findings"], 0)
        self.assertEqual(proof["health"], 100)

    # -- the corrected assertion -----------------------------------------

    def test_health_equality_is_not_used_as_the_proof(self):
        """REGRESSION. The first version required
        `final health == waiting health`. Under the real policy contract
        (agent/evidence_policy.py: policy "cannot manufacture a finding or
        subtract health") that equality holds for EVERY metadata finding, so
        it passed whether or not the comparison had contributed one. A
        comparison-derived finding must fail even when health is untouched.
        """
        final = attempt(health=100,
                        findings=[NULL_RATE_FINDING,
                                  {"code": "metadata_comparison.row_count_drop",
                                   "category": "production"}])
        with self.assertRaises(StageFailure):
            self.d.assert_comparison_is_evidence_only(attempt(health=100), final)

        proof = self.d.assert_comparison_is_evidence_only(
            attempt(health=100), attempt(health=100, findings=[NULL_RATE_FINDING]))
        self.assertFalse(proof["equality_with_waiting_health_used_as_proof"])

    def test_the_policy_contract_still_passes_health_through(self):
        """If this ever changes, the health expectation above is wrong."""
        policy = (REPO_ROOT / "agent" / "evidence_policy.py").read_text(
            encoding="utf-8")
        self.assertIn("cannot manufacture a finding or subtract health", policy)

    def test_a_comparison_derived_category_fails(self):
        final = attempt(findings=[{"code": "x", "category": "metadata_comparison"}])
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison_is_evidence_only(attempt(health=100), final)
        self.assertIn("comparison-derived", str(caught.exception))


class ComparisonNotAPolicyInputTests(DriverTestCase):
    def test_the_real_recompute_path_decides_before_it_compares(self):
        proof = self.d.assert_comparison_is_not_a_policy_input()
        self.assertTrue(proof["decision_computed_before_comparison"])
        self.assertFalse(proof["comparison_passed_to_policy"])

    def test_a_comparison_fed_into_the_decision_would_fail(self):
        """The check must actually read the source, not assert a constant."""
        import agent.metadata_evidence.recompute as recompute

        original = Path(recompute.__file__).read_text(encoding="utf-8")
        self.assertIn("decision = evaluate_metadata_decision(", original)
        self.assertIn("metadata_comparison = compute_comparison(", original)
        self.assertLess(original.index("decision = evaluate_metadata_decision("),
                        original.index("metadata_comparison = compute_comparison("))


# ------------------------------------------------------ final decision

class FinalDecisionTests(DriverTestCase):
    def decided(self, **overrides):
        review = {"decision": "WARN", "health": 100,
                  "evidence_coverage": "COMPLETE",
                  "lifecycle_state": "DECISION_READY"}
        review.update(overrides)
        return review

    def test_a_review_that_never_decides_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_final_decision(self.decided(decision=None),
                                         attempt(findings=[NULL_RATE_FINDING]))
        self.assertIn("never reached a decision", str(caught.exception))

    def test_a_review_still_waiting_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_final_decision(
                self.decided(lifecycle_state="WAITING_FOR_METADATA"),
                attempt(findings=[NULL_RATE_FINDING]))

    def test_a_different_decision_than_the_policy_establishes_fails(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_final_decision(self.decided(decision="ALLOW"),
                                         attempt(findings=[NULL_RATE_FINDING]))
        self.assertIn("expected 'WARN'", str(caught.exception))

    def test_a_missing_production_finding_fails(self):
        with self.assertRaises(StageFailure):
            self.d.assert_final_decision(self.decided(), attempt(findings=[]))

    def test_a_code_finding_fails_the_code_neutral_fixture(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_final_decision(
                self.decided(),
                attempt(findings=[NULL_RATE_FINDING,
                                  {"code": "sql.x", "category": "code"}]))
        self.assertIn("code findings", str(caught.exception))

    def test_the_established_policy_outcome_passes(self):
        proof = self.d.assert_final_decision(
            self.decided(), attempt(findings=[NULL_RATE_FINDING]))
        self.assertEqual(proof["decision"], "WARN")
        self.assertEqual(proof["production_finding"], "column.high_null_rate")


# ------------------------------------------------------------ readiness

class ReadinessTests(DriverTestCase):
    def test_stale_migrations_fail(self):
        with self.assertRaises(StageFailure):
            self.d.readiness_gate({"migrations": "pending", "database": "ok",
                                   "review_lifecycle": "postgresql"})

    def test_current_migrations_pass_without_a_pinned_version_list(self):
        proof = self.d.readiness_gate({"migrations": "current", "database": "ok",
                                       "review_lifecycle": "postgresql"})
        self.assertFalse(proof["pinned_version_list_used"])

    def test_the_driver_pins_no_migration_version_list(self):
        """The stale metadata-review gate must not be copied in."""
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        self.assertNotIn("[1, 2, 3, 4]", source)
        self.assertNotIn("migrations 1-4", source)


# ---------------------------------------------------- observation bodies

#: A request shaped exactly as the store returns one, using the identities the
#: real fixture produced in run 31397009727.
REQUEST = {
    "request_id": "req-gh-abc-1",
    "targets": [
        {"target_index": 0, "relation_name": "main.dim_customers",
         "relation_schema": "main", "relation_database": "warehouse",
         "model_unique_id": "model.relium_e2e_dbt.dim_customers",
         "columns": ["customer_id"], "criticality": "standard",
         "dependency_kind": "external"},
        {"target_index": 1, "relation_name": "main.stg_orders",
         "relation_schema": "main", "relation_database": "warehouse",
         "model_unique_id": "model.relium_e2e_dbt.stg_orders",
         "columns": ["customer_id", "order_id"], "criticality": "standard",
         "dependency_kind": "external"},
    ],
}


class ObservationTests(DriverTestCase):
    def review(self):
        return {"review_id": "rev-1", "attempt": 1, "base_sha": "a" * 40,
                "head_sha": "b" * 40, "base_manifest_hash": "bh",
                "head_manifest_hash": "hh"}

    def build(self, *, row_count, null_rate, cardinality, request=None):
        from datetime import datetime, timezone

        request = request or REQUEST
        rel, col = self.d.select_carrier(request)
        return self.d.observation_from_request(
            self.review(), request, carrier_relation=rel, carrier_column=col,
            row_count=row_count, null_rate=null_rate, cardinality=cardinality,
            observed_at=datetime(2026, 8, 10, tzinfo=timezone.utc))

    # -- REGRESSION for run 31397009727 ----------------------------------

    def test_relation_identity_comes_from_the_request(self):
        body = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        names = sorted(r["relation_name"] for r in body["relations"])
        self.assertEqual(names, ["main.dim_customers", "main.stg_orders"])
        for relation in body["relations"]:
            self.assertTrue(relation["model_unique_id"].startswith("model."))

    def test_column_identity_comes_from_the_request(self):
        body = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        by_relation = {r["relation_name"]: sorted(c["column_name"]
                                                  for c in r["columns"])
                       for r in body["relations"]}
        self.assertEqual(by_relation, {"main.dim_customers": ["customer_id"],
                                       "main.stg_orders": ["customer_id",
                                                           "order_id"]})

    def test_no_unrequested_column_is_submitted(self):
        body = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        requested = {(t["relation_name"], c)
                     for t in REQUEST["targets"] for c in t["columns"]}
        submitted = {(r["relation_name"], c["column_name"])
                     for r in body["relations"] for c in r["columns"]}
        self.assertEqual(submitted, requested)

    def test_every_requested_column_is_present(self):
        """This fixture does not test missing-production policy."""
        for values in ((1000, 0.01, 0.37), (800, self.d.B_NULL_RATE, 0.42)):
            body = self.build(row_count=values[0], null_rate=values[1],
                              cardinality=values[2])
            for relation in body["relations"]:
                self.assertTrue(relation["exists_in_production"])
                for column in relation["columns"]:
                    self.assertTrue(column["exists"], column)

    def test_no_hardcoded_raw_orders_fixture_survives(self):
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        self.assertNotIn("raw.orders", source)
        self.assertNotIn("discount_amount", source)
        self.assertNotIn("observation_body", source)

    def test_the_carrier_is_a_requested_non_critical_column(self):
        relation, column = self.d.select_carrier(REQUEST)
        requested = {(t["relation_name"], c)
                     for t in REQUEST["targets"] for c in t["columns"]}
        self.assertIn((relation, column), requested)
        target = [t for t in REQUEST["targets"]
                  if t["relation_name"] == relation][0]
        self.assertNotEqual(target["criticality"], "critical")

    def test_a_critical_target_is_never_chosen_as_carrier(self):
        """high_null_rate is BLOCK severity on a critical target."""
        critical = {"request_id": "r", "targets": [
            dict(REQUEST["targets"][0], criticality="critical"),
            dict(REQUEST["targets"][1], criticality="standard")]}
        relation, _column = self.d.select_carrier(critical)
        self.assertEqual(relation, "main.stg_orders")

    def test_no_carrier_available_fails_loudly(self):
        allcritical = {"request_id": "r", "targets": [
            dict(t, criticality="critical") for t in REQUEST["targets"]]}
        with self.assertRaises(StageFailure):
            self.d.select_carrier(allcritical)

    # -- the A -> B differences ------------------------------------------

    def test_a_to_b_moves_row_count_null_rate_and_cardinality(self):
        a = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        b = self.build(row_count=800, null_rate=self.d.B_NULL_RATE,
                       cardinality=0.42)
        rel, col = self.d.select_carrier(REQUEST)

        def carrier(body):
            relation = [r for r in body["relations"]
                        if r["relation_name"] == rel][0]
            column = [c for c in relation["columns"]
                      if c["column_name"] == col][0]
            return relation, column

        a_rel, a_col = carrier(a)
        b_rel, b_col = carrier(b)
        self.assertEqual((a_rel["row_count"], b_rel["row_count"]), (1000, 800))
        self.assertEqual((a_col["null_rate"], b_col["null_rate"]),
                         (0.01, self.d.B_NULL_RATE))
        self.assertEqual((a_col["cardinality"], b_col["cardinality"]),
                         (0.37, 0.42))

    def test_the_unchanged_signal_is_identical_in_a_and_b(self):
        a = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        b = self.build(row_count=800, null_rate=self.d.B_NULL_RATE,
                       cardinality=0.42)
        for ra, rb in zip(sorted(a["relations"], key=lambda r: r["relation_name"]),
                          sorted(b["relations"], key=lambda r: r["relation_name"])):
            self.assertEqual(ra["schema_fingerprint"], rb["schema_fingerprint"])

    def test_non_carrier_signals_do_not_move(self):
        a = self.build(row_count=1000, null_rate=0.01, cardinality=0.37)
        b = self.build(row_count=800, null_rate=self.d.B_NULL_RATE,
                       cardinality=0.42)
        rel, col = self.d.select_carrier(REQUEST)
        for ra, rb in zip(sorted(a["relations"], key=lambda r: r["relation_name"]),
                          sorted(b["relations"], key=lambda r: r["relation_name"])):
            if ra["relation_name"] != rel:
                self.assertEqual(ra["row_count"], rb["row_count"])
            for ca, cb in zip(ra["columns"], rb["columns"]):
                if (ra["relation_name"], ca["column_name"]) != (rel, col):
                    self.assertEqual(ca["null_rate"], cb["null_rate"])

    def test_no_raw_rows_are_transmitted(self):
        import json

        blob = json.dumps(self.build(row_count=1000, null_rate=0.01,
                                     cardinality=0.37))
        for forbidden in ("sample", "select ", "min_value", "max_value"):
            self.assertNotIn(forbidden, blob)

    def test_b_null_rate_crosses_the_existing_policy_threshold(self):
        """An illustrative 12% would never fire column.high_null_rate."""
        self.assertGreater(self.d.B_NULL_RATE, self.d.NULL_RATE_THRESHOLD)
        self.assertLess(self.d.A_NULL_RATE, self.d.NULL_RATE_THRESHOLD)


class MissingProductionPolicyTests(DriverTestCase):
    """This fixture must not trip missing-production policy at all."""

    def test_the_comparison_refuses_a_column_availability_change(self):
        with self.assertRaises(StageFailure) as caught:
            self.d.assert_comparison(comparison(extra=[{
                "kind": "column_availability_changed", "relation": "main.stg_orders",
                "column": "order_id", "signal": "column_exists",
                "before": True, "after": False}]), SNAP_A, SNAP_B)
        self.assertIn("requested column", str(caught.exception))

    def test_a_missing_production_finding_fails_the_final_assertion(self):
        review = {"decision": "BLOCK", "health": 100,
                  "evidence_coverage": "COMPLETE",
                  "lifecycle_state": "DECISION_READY"}
        row = attempt(findings=[
            {"code": "column.missing_in_production", "category": "production",
             "severity": "block"}])
        with self.assertRaises(StageFailure):
            self.d.assert_final_decision(review, row)

    def test_the_expected_outcome_is_warn_from_the_high_null_rate(self):
        proof = self.d.assert_final_decision(
            {"decision": "WARN", "health": 100, "evidence_coverage": "COMPLETE",
             "lifecycle_state": "DECISION_READY"},
            attempt(findings=[NULL_RATE_FINDING]))
        self.assertEqual(proof["decision"], "WARN")
        self.assertEqual(proof["production_finding"], "column.high_null_rate")
        self.assertEqual(proof["code_findings"], 0)


# --------------------------------------------------------------- cleanup

class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.returncode = None if alive else 0
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class CleanupTests(DriverTestCase):
    def arrange(self, *, branches=(), pulls=()):
        record = self.d._initial_recovery()
        record["branches"] = list(branches)
        record["pulls"] = list(pulls)
        record["webhook_preserved"] = True
        record["original_webhook"] = {"url": "https://original.example/hook",
                                      "content_type": "json", "insecure_ssl": "0"}
        self.d._write_recovery(record)

    def install_github(self, *, deleted_ok=True, closed_ok=True):
        calls = []

        def fake_gh(method, path, token, body=None, bearer=True):
            calls.append((method, path))
            if path == "/app/hook/config":
                return 200, {"url": "https://original.example/hook",
                             "content_type": "json", "insecure_ssl": "0"}
            if method == "DELETE":
                return (204, {}) if deleted_ok else (500, {})
            if path.startswith("/repos/") and "/git/ref/heads/" in path:
                return (404, {}) if deleted_ok else (200, {"ref": path})
            if method == "GET" and "/pulls/" in path:
                return 200, {"state": "open", "merged": False}
            if method == "PATCH" and "/pulls/" in path:
                return (200, {}) if closed_ok else (500, {})
            return 200, {}

        self.d.GH = fake_gh
        self.d.APP_JWT = lambda: "jwt"
        self.d._REF_ABSENCE_INTERVAL = 0.0
        return calls

    def test_cleanup_restores_the_webhook_first(self):
        self.arrange(branches=["e2e/x"], pulls=[7])
        calls = self.install_github()
        self.d.cleanup("test")
        patches = [c for c in calls if c[0] == "PATCH"
                   and c[1] == "/app/hook/config"]
        self.assertEqual(len(patches), 1)
        first_mutation = next(i for i, c in enumerate(calls)
                              if c[0] in ("PATCH", "DELETE"))
        self.assertEqual(calls[first_mutation][1], "/app/hook/config")

    def test_cleanup_closes_owned_pulls_and_deletes_owned_branches(self):
        self.arrange(branches=["e2e/a", "e2e/b"], pulls=[7])
        self.install_github()
        result = self.d.cleanup("test")
        self.assertEqual(result["pulls_closed_unmerged"], [7])
        self.assertEqual(sorted(result["branches_deleted"]), ["e2e/a", "e2e/b"])
        self.assertTrue(result["passed"])

    def test_cleanup_touches_nothing_it_does_not_own(self):
        self.arrange(branches=["e2e/mine"], pulls=[])
        calls = self.install_github()
        self.d.cleanup("test")
        deleted = [c[1] for c in calls if c[0] == "DELETE"]
        self.assertEqual(deleted, ["/repos/" + self.d.REPO
                                   + "/git/refs/heads/e2e/mine"])

    def test_a_branch_that_survives_deletion_fails_cleanup(self):
        self.arrange(branches=["e2e/stuck"])
        self.install_github(deleted_ok=False)
        result = self.d.cleanup("test")
        self.assertFalse(result["passed"])
        self.assertTrue(any("still exists" in f for f in result["failures"]))

    def test_a_merged_owned_pull_fails_cleanup(self):
        self.arrange(pulls=[9])

        def fake_gh(method, path, token, body=None, bearer=True):
            if path == "/app/hook/config":
                return 200, {"url": "https://original.example/hook",
                             "content_type": "json", "insecure_ssl": "0"}
            if method == "GET" and "/pulls/" in path:
                return 200, {"state": "closed", "merged": True}
            return 200, {}

        self.d.GH = fake_gh
        self.d.APP_JWT = lambda: "jwt"
        result = self.d.cleanup("test")
        self.assertFalse(result["passed"])
        self.assertTrue(any("MERGED" in f for f in result["failures"]))

    def test_cleanup_stops_and_reaps_processes(self):
        self.arrange()
        self.install_github()
        proc = FakeProc(alive=True)
        self.d.state["procs"] = [("api", proc)]
        self.d.cleanup("test")
        self.assertTrue(proc.terminated)
        self.assertIsNotNone(proc.returncode)

    def test_cleanup_is_idempotent(self):
        self.arrange(branches=["e2e/a"])
        calls = self.install_github()
        first = self.d.cleanup("one")
        count = len(calls)
        second = self.d.cleanup("two")
        self.assertEqual(len(calls), count)
        self.assertTrue(second.get("repeat") or second is first)

    def test_cleanup_runs_after_a_stage_failure_in_any_stage(self):
        """Every major stage failure must still reach cleanup."""
        for stage in ("semantic", "blast_radius", "metadata_request",
                      "comparison", "final_decision"):
            with self.subTest(stage=stage):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                driver = load_driver(Path(tmp.name))
                record = driver._initial_recovery()
                record["branches"] = ["e2e/after-" + stage]
                record["webhook_preserved"] = True
                record["original_webhook"] = {"url": "https://o.example/h",
                                              "content_type": "json",
                                              "insecure_ssl": "0"}
                driver._write_recovery(record)
                driver.GH = lambda m, p, t, b=None, bearer=True: (
                    (200, {"url": "https://o.example/h", "content_type": "json",
                           "insecure_ssl": "0"}) if p == "/app/hook/config"
                    else (404, {}) if m == "GET" else (204, {}))
                driver.APP_JWT = lambda: "jwt"
                driver._REF_ABSENCE_INTERVAL = 0.0
                result = driver.cleanup(f"stage-failure: {stage}")
                self.assertIn(stage, result["reason"])
                self.assertEqual(result["branches_deleted"],
                                 ["e2e/after-" + stage])


class TunnelWiringTests(DriverTestCase):
    """REGRESSION for run 31392463042.

    The 69 tests before this one exercised every assertion and never the
    wiring, so two contract mistakes reached a live run: `start_tunnel`
    RETURNS a proof mapping and PUBLISHES the URL into
    `state["tunnel"]["url"]`, and `state["tunnel"]` is a mapping rather than a
    process. The fake below behaves exactly as live_flow does, so a driver that
    misreads either contract fails here instead of on a runner.
    """

    def faithful_start_tunnel(self, state, log_path, on_start=None):
        """Byte-for-byte the shape live_flow.start_tunnel leaves behind."""
        proc = FakeProc(alive=True)
        state["tunnel"] = {"proc": proc, "url": None}
        if on_start is not None:
            on_start("tunnel", proc, "cloudflared")
        state["tunnel"]["url"] = "https://fake-tunnel.trycloudflare.com"
        return {"pid": 4242, "url_host": "fake-tunnel.trycloudflare.com",
                "scheme": "https", "edge_reachable_from_public_internet": True,
                "verified_before_webhook_repoint": True}

    def test_the_tunnel_url_is_a_string_not_the_proof_mapping(self):
        proof = self.faithful_start_tunnel(self.d.state, "ignored",
                                           on_start=self.d.register_process)
        url = self.d.tunnel_url(self.d.state)
        self.assertIsInstance(url, str)
        self.assertTrue(url.startswith("https://"))
        # The returned proof is NOT the URL; passing it on is what broke.
        self.assertIsInstance(proof, dict)
        self.assertNotEqual(proof, url)

    def test_point_webhook_receives_something_it_can_rstrip(self):
        self.faithful_start_tunnel(self.d.state, "ignored",
                                   on_start=self.d.register_process)
        url = self.d.tunnel_url(self.d.state)
        # The exact operation live_flow.point_webhook performs.
        self.assertEqual(url.rstrip("/") + "/github/webhook",
                         "https://fake-tunnel.trycloudflare.com/github/webhook")

    def test_a_tunnel_without_a_url_fails_loudly(self):
        self.d.state["tunnel"] = {"proc": FakeProc(), "url": None}
        with self.assertRaises(StageFailure):
            self.d.tunnel_url(self.d.state)

    def test_no_tunnel_at_all_fails_loudly(self):
        self.d.state["tunnel"] = None
        with self.assertRaises(StageFailure):
            self.d.tunnel_url(self.d.state)

    def test_the_tunnel_process_is_registered_for_cleanup(self):
        self.d.state["procs"] = []
        self.faithful_start_tunnel(self.d.state, "ignored",
                                   on_start=self.d.register_process)
        labels = [label for label, _ in self.d.state["procs"]]
        self.assertIn("tunnel", labels)

    def test_cleanup_stops_the_tunnel_without_touching_the_mapping(self):
        """The old code called .poll() on state['tunnel'], a dict."""
        record = self.d._initial_recovery()
        record["webhook_preserved"] = True
        record["original_webhook"] = {"url": "https://o.example/h",
                                      "content_type": "json", "insecure_ssl": "0"}
        self.d._write_recovery(record)
        self.d.GH = lambda m, p, t, b=None, bearer=True: (
            (200, {"url": "https://o.example/h", "content_type": "json",
                   "insecure_ssl": "0"}) if p == "/app/hook/config" else (200, {}))
        self.d.APP_JWT = lambda: "jwt"
        self.d.state["procs"] = []
        self.faithful_start_tunnel(self.d.state, "ignored",
                                   on_start=self.d.register_process)
        tunnel_proc = self.d.state["tunnel"]["proc"]

        result = self.d.cleanup("test")

        self.assertTrue(tunnel_proc.terminated)
        self.assertTrue(result["passed"], result.get("failures"))
        self.assertIn("tunnel", [p["label"] for p in result["processes_stopped"]])

    def test_the_driver_never_polls_the_tunnel_mapping(self):
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        self.assertNotIn("tunnel.poll()", source)
        self.assertNotIn('state["tunnel"].poll', source)

    def test_the_driver_reads_the_url_from_state(self):
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        self.assertIn("public_url = tunnel_url(state)", source)
        self.assertIn("lf.point_webhook(state, GH, APP_JWT, public_url)", source)


class EvidenceOrderingTests(DriverTestCase):
    def test_the_summary_is_not_written_before_assertions_pass(self):
        """A failed run must not leave an artifact that looks like a pass."""
        self.assertFalse((self.evidence / "integrated-product-summary.json").exists())
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        summary_write = source.index('_write("integrated-product-summary.json"')
        for assertion in ("assert_semantic(", "assert_blast_radius(",
                          "assert_metadata_request(", "assert_comparison(",
                          "assert_final_decision(",
                          "assert_comparison_is_evidence_only("):
            self.assertLess(source.index(assertion), summary_write,
                            f"{assertion} must run before the summary is written")

    def test_ownership_is_recorded_before_the_mutating_call(self):
        source = (REPO_ROOT / "scripts" / "e2e"
                  / "integrated_product_e2e.py").read_text(encoding="utf-8")
        branch_fn = source[source.index("def make_branch("):
                           source.index("def commit_file(")]
        self.assertLess(branch_fn.index("_write_recovery(record)"),
                        branch_fn.index('GH("POST"'))


class WorkflowSelectionTests(unittest.TestCase):
    """Static checks on the workflow, so a dispatch cannot silently no-op."""

    def setUp(self):
        self.text = (REPO_ROOT / ".github" / "workflows"
                     / "governance-e2e.yml").read_text(encoding="utf-8")

    def test_the_operation_is_selectable(self):
        self.assertIn("integrated-product", self.text)
        self.assertIn("if: inputs.operation == 'integrated-product'", self.text)

    def test_every_required_secret_is_wired(self):
        job = self.text[self.text.index("  integrated-product:"):
                        self.text.index("  webhook-recovery:")]
        for secret in ("RELIUM_E2E_APP_ID", "RELIUM_E2E_PRIVATE_KEY",
                       "RELIUM_E2E_WEBHOOK_SECRET", "RELIUM_E2E_INSTALLATION_ID",
                       "RELIUM_E2E_FIXTURE_TOKEN"):
            self.assertIn(f"secrets.{secret}", job, f"{secret} is not wired")

    def test_the_private_key_is_written_outside_the_workspace(self):
        job = self.text[self.text.index("  integrated-product:"):
                        self.text.index("  webhook-recovery:")]
        self.assertIn("$RUNNER_TEMP/relium-secrets/app.pem", job)
        self.assertNotIn("github.workspace }}/app.pem", job)

    def test_cleanup_secret_scan_and_upload_are_always_run(self):
        job = self.text[self.text.index("  integrated-product:"):
                        self.text.index("  webhook-recovery:")]
        self.assertIn("Mandatory exact integrated-product cleanup", job)
        self.assertIn("--cleanup-only", job)
        self.assertIn("secret_scan.py", job)
        self.assertIn("integrated_secret_scan.outcome == 'success'", job)

    def test_the_job_shares_the_webhook_concurrency_group(self):
        self.assertIn("group: metadata-review-e2e", self.text)

    def test_the_harness_tests_gate_the_fixture_repository(self):
        job = self.text[self.text.index("  integrated-product:"):
                        self.text.index("  webhook-recovery:")]
        self.assertLess(job.index("test_integrated_product_e2e_harness"),
                        job.index("integrated_product_e2e.py \"$EVIDENCE_DIR\""))


if __name__ == "__main__":
    unittest.main()
