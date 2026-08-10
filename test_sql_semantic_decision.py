"""The refund decision signal, now proved from AST evidence.

The semantic comparison could see a refund adjustment disappear while the
decision engine still said ALLOW, because the two used different detectors.
Evidence and decision disagreeing is worse than either being wrong alone: the
dashboard would have shown the change and the check would have passed it.

These assert the decision, not just the evidence, and they pin the *existing*
policy outcome rather than choosing a new one.
"""
from __future__ import annotations

import copy
import unittest

from agent.deployment_review_service import review_manifest_change


def manifest(sql, columns, depends_on=()):
    return {
        "metadata": {"project_name": "p"},
        "nodes": {"model.p.fct_orders": {
            "unique_id": "model.p.fct_orders", "resource_type": "model",
            "name": "fct_orders", "schema": "a", "alias": "fct_orders",
            "database": "w", "path": "models/fct_orders.sql",
            "original_file_path": "models/fct_orders.sql",
            "raw_code": sql, "compiled_code": sql,
            "depends_on": {"nodes": list(depends_on)},
            "columns": {c: {"name": c} for c in columns},
            "config": {"materialized": "table"}, "description": ""}},
        "sources": {}, "exposures": {}, "macros": {}, "child_map": {},
        "parent_map": {},
    }


def review(before, after):
    return review_manifest_change(
        manifest=copy.deepcopy(after), changed_files=["models/fct_orders.sql"],
        changed_models=["fct_orders"], deployment_id="d",
        previous_manifest=copy.deepcopy(before), manifest_source={"head": "x"},
        base_sha="a" * 40, head_sha="b" * 40)


UNALIASED_BEFORE = manifest(
    "select total_sales - refund_total as realized_sales from source_data",
    ["realized_sales"])
UNALIASED_AFTER = manifest(
    "select total_sales as realized_sales from source_data", ["realized_sales"])

ALIASED_BEFORE = manifest(
    "select o.order_id, o.amount - coalesce(r.refund_amount, 0) as net_order_amount "
    "from {{ ref('stg_orders') }} o "
    "left join {{ ref('stg_refunds') }} r on r.order_id = o.order_id",
    ["order_id", "net_order_amount"], ["model.p.stg_orders", "model.p.stg_refunds"])
ALIASED_AFTER = manifest(
    "select o.order_id, o.amount as net_order_amount from {{ ref('stg_orders') }} o",
    ["order_id", "net_order_amount"], ["model.p.stg_orders"])


class RefundDecisionTests(unittest.TestCase):
    #: The outcome the unaliased fixture already received before this change.
    EXPECTED_DECISION = "BLOCK"
    EXPECTED_HEALTH = 65

    def _incident(self, before, after):
        return review(before, after)["incident"]

    def test_the_unaliased_case_keeps_its_existing_decision(self):
        incident = self._incident(UNALIASED_BEFORE, UNALIASED_AFTER)
        self.assertEqual(incident["decision"], self.EXPECTED_DECISION)
        self.assertEqual(incident["health"], self.EXPECTED_HEALTH)

    def test_the_aliased_case_now_matches_the_unaliased_one(self):
        """The gap: idiomatic `r.refund_amount` used to pass silently."""
        incident = self._incident(ALIASED_BEFORE, ALIASED_AFTER)
        self.assertEqual(incident["decision"], self.EXPECTED_DECISION)
        self.assertEqual(incident["health"], self.EXPECTED_HEALTH)

    def test_both_are_proved_by_the_ast_detector(self):
        for before, after in ((UNALIASED_BEFORE, UNALIASED_AFTER),
                              (ALIASED_BEFORE, ALIASED_AFTER)):
            changes = self._incident(before, after)["metadata"][
                "manifest_comparison"]["material_sql_changes"]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["detector"], "ast_projection_expression")
            self.assertEqual(changes[0]["finding_type"],
                             "refund_adjustment_subtraction_removed")

    def test_the_finding_names_the_exact_projection_that_proved_it(self):
        changes = self._incident(ALIASED_BEFORE, ALIASED_AFTER)["metadata"][
            "manifest_comparison"]["material_sql_changes"]
        self.assertEqual(changes[0]["output_name"], "net_order_amount")
        self.assertIn("refund_amount", changes[0]["before_sql"])
        self.assertNotIn("refund", changes[0]["after_sql"].lower())

    def test_evidence_and_decision_agree(self):
        """The whole point: both surfaces tell the same story."""
        incident = self._incident(ALIASED_BEFORE, ALIASED_AFTER)
        comparison = incident["metadata"]["manifest_comparison"]
        self.assertTrue(comparison["sql_semantic_comparison"]["change_count"])
        self.assertTrue(comparison["material_sql_changes"])
        self.assertEqual(incident["decision"], self.EXPECTED_DECISION)


class NoFalseRefundSignalTests(unittest.TestCase):
    """Descriptive evidence must not become a decision on its own."""

    def _decision(self, before, after):
        incident = review(before, after)["incident"]
        return (incident["decision"], incident["health"],
                incident["metadata"]["manifest_comparison"]["material_sql_changes"])

    def test_an_unrelated_projection_change_is_not_a_refund_signal(self):
        decision, health, changes = self._decision(
            manifest("select a + b as total from t", ["total"]),
            manifest("select a * b as total from t", ["total"]))
        self.assertEqual(changes, [])
        self.assertEqual((decision, health), ("ALLOW", 100))

    def test_an_unrelated_join_removal_is_not_a_refund_signal(self):
        """`join_removed` is evidence, not a policy trigger."""
        decision, health, changes = self._decision(
            manifest("select a from {{ ref('t') }} t join {{ ref('u') }} u on u.id = t.id", ["a"]),
            manifest("select a from {{ ref('t') }} t", ["a"]))
        self.assertEqual(changes, [])
        self.assertEqual((decision, health), ("ALLOW", 100))

    def test_a_filter_change_is_not_a_refund_signal(self):
        decision, _health, changes = self._decision(
            manifest("select a from t where x = 1", ["a"]),
            manifest("select a from t where x = 2", ["a"]))
        self.assertEqual(changes, [])
        self.assertEqual(decision, "ALLOW")

    def test_formatting_only_produces_neither_evidence_nor_signal(self):
        decision, health, changes = self._decision(
            manifest("select   A ,  b   FROM t", ["a", "b"]),
            manifest("SELECT a, b\nfrom t", ["a", "b"]))
        comparison = review(
            manifest("select   A ,  b   FROM t", ["a", "b"]),
            manifest("SELECT a, b\nfrom t", ["a", "b"]),
        )["incident"]["metadata"]["manifest_comparison"]
        self.assertEqual(comparison["sql_semantic_comparison"]["change_count"], 0)
        self.assertEqual(changes, [])
        self.assertEqual((decision, health), ("ALLOW", 100))

    def test_a_refund_column_added_is_not_a_removal(self):
        decision, _health, changes = self._decision(
            manifest("select total_sales as v from t", ["v"]),
            manifest("select total_sales - refund_total as v from t", ["v"]))
        self.assertEqual(changes, [])
        self.assertEqual(decision, "ALLOW")


class UnparseableSqlTests(unittest.TestCase):
    def test_unparseable_sql_does_not_fabricate_the_signal(self):
        incident = review(
            manifest("select total_sales - refund_total as v from t", ["v"]),
            manifest("!!! this is not sql !!!", ["v"]),
        )["incident"]
        comparison = incident["metadata"]["manifest_comparison"]
        model = comparison["sql_semantic_comparison"]["models"][0]
        self.assertEqual(model["status"], "unavailable")
        self.assertEqual(model["changes"], [])
        # The regex fallback still runs for SQL the AST could not read, which
        # is why it was kept; it must not invent a finding from nothing.
        for change in comparison["material_sql_changes"]:
            self.assertEqual(change["detector"], "regex_fallback_unparsed_sql")


if __name__ == "__main__":
    unittest.main()
