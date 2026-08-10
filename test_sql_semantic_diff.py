"""SQL semantic comparison: what changed, proven from the AST.

The first test is the case that started this: removing a refund join so
`net_order_amount` quietly becomes gross. The previous refund fallback missed
it twice over — its regex needed an unqualified `refund*` so `r.refund_amount`
never matched, and `_canonical_sql` rewrote every `{{ ref(...) }}` to the same
`dbt_ref` token so the join could not be told apart from any other.

Nothing here asserts a hardcoded expression string that was not parsed out of
the SQL under test.
"""
from __future__ import annotations

import unittest

from agent.sql_semantic_diff import (
    FILTER_CHANGED, GROUPING_CHANGED, JOIN_ADDED, JOIN_CONDITION_CHANGED,
    JOIN_REMOVED, JOIN_TYPE_CHANGED, PROJECTION_ADDED,
    PROJECTION_EXPRESSION_CHANGED, PROJECTION_REMOVED, STATUS_UNAVAILABLE,
    compare_model_sql,
)

# The exact fixture pair from the probe, verbatim.
REFUND_BASE = """
select
    o.order_id,
    o.customer_id,
    o.amount - coalesce(r.refund_amount, 0) as net_order_amount
from {{ ref('stg_orders') }} o
left join {{ ref('stg_refunds') }} r on r.order_id = o.order_id
"""

REFUND_HEAD = """
select
    o.order_id,
    o.customer_id,
    o.amount as net_order_amount
from {{ ref('stg_orders') }} o
"""


def kinds(comparison):
    return [change["kind"] for change in comparison.changes]


def by_kind(comparison, kind):
    return [change for change in comparison.changes if change["kind"] == kind]


class RefundFixtureTests(unittest.TestCase):
    """The case the previous engine could not see."""

    def setUp(self):
        self.comparison = compare_model_sql(
            "fct_orders", REFUND_BASE, REFUND_HEAD,
            model_unique_id="model.probe.fct_orders")

    def test_the_comparison_is_evaluated(self):
        self.assertTrue(self.comparison.evaluated)
        self.assertEqual(self.comparison.status, "evaluated")

    def test_the_net_amount_expression_change_is_detected(self):
        changed = by_kind(self.comparison, PROJECTION_EXPRESSION_CHANGED)
        self.assertEqual(len(changed), 1, self.comparison.changes)
        change = changed[0]
        self.assertEqual(change["output_name"], "net_order_amount")
        # Parsed out of the SQL, not asserted as a literal: the refund
        # subtraction is present before and absent after.
        self.assertIn("refund_amount", change["before_sql"])
        self.assertNotIn("refund", change["after_sql"].lower())
        self.assertIn("-", change["before_sql"])

    def test_the_refund_join_removal_is_detected(self):
        removed = by_kind(self.comparison, JOIN_REMOVED)
        self.assertEqual([change["relation"] for change in removed], ["stg_refunds"])
        self.assertEqual(removed[0]["before_join_type"], "LEFT")
        self.assertIn("order_id", removed[0]["before_condition_sql"])
        self.assertIsNone(removed[0]["after"])

    def test_the_surviving_join_is_not_reported(self):
        relations = [c.get("relation") for c in self.comparison.changes if "relation" in c]
        self.assertNotIn("stg_orders", relations)

    def test_unchanged_projections_are_not_reported(self):
        names = [c.get("output_name") for c in self.comparison.changes if "output_name" in c]
        self.assertNotIn("order_id", names)
        self.assertNotIn("customer_id", names)

    def test_model_identity_is_carried_on_every_change(self):
        for change in self.comparison.changes:
            self.assertEqual(change["model_name"], "fct_orders")
            self.assertEqual(change["model_unique_id"], "model.probe.fct_orders")

    def test_no_prose_or_impact_claim_is_produced(self):
        text = str(self.comparison.to_dict()).lower()
        for banned in ("affect", "impact", "break", "revenue will", "every row"):
            self.assertNotIn(banned, text)


class SupportedChangeKindTests(unittest.TestCase):
    def test_join_added(self):
        comparison = compare_model_sql("m", "select a from t",
                                       "select a from t left join u on u.id = t.id")
        added = by_kind(comparison, JOIN_ADDED)
        self.assertEqual([c["relation"] for c in added], ["u"])
        self.assertEqual(added[0]["after_join_type"], "LEFT")

    def test_join_condition_changed(self):
        comparison = compare_model_sql(
            "m", "select a from t join u on u.id = t.id",
            "select a from t join u on u.id = t.other_id")
        changed = by_kind(comparison, JOIN_CONDITION_CHANGED)
        self.assertEqual(len(changed), 1)
        self.assertNotEqual(changed[0]["before_sql"], changed[0]["after_sql"])

    def test_join_type_changed(self):
        comparison = compare_model_sql(
            "m", "select a from t left join u on u.id = t.id",
            "select a from t inner join u on u.id = t.id")
        changed = by_kind(comparison, JOIN_TYPE_CHANGED)
        self.assertEqual(changed[0]["before_join_type"], "LEFT")
        self.assertIn("INNER", changed[0]["after_join_type"])

    def test_where_predicate_changed(self):
        comparison = compare_model_sql(
            "m", "select a from t where status = 'paid'",
            "select a from t where status = 'paid' and amount > 0")
        changed = by_kind(comparison, FILTER_CHANGED)
        self.assertEqual(changed[0]["scope"], "where")
        self.assertIn("amount", changed[0]["after_sql"])

    def test_having_predicate_changed(self):
        comparison = compare_model_sql(
            "m", "select a, sum(b) as t from x group by a having sum(b) > 0",
            "select a, sum(b) as t from x group by a having sum(b) > 100")
        scopes = [c["scope"] for c in by_kind(comparison, FILTER_CHANGED)]
        self.assertIn("having", scopes)

    def test_group_by_grain_changed(self):
        comparison = compare_model_sql(
            "m", "select a, sum(b) as t from x group by a",
            "select a, c, sum(b) as t from x group by a, c")
        changed = by_kind(comparison, GROUPING_CHANGED)
        self.assertEqual(len(changed), 1)
        self.assertNotEqual(changed[0]["before_sql"], changed[0]["after_sql"])

    def test_aggregate_expression_changed(self):
        comparison = compare_model_sql(
            "m", "select sum(amount) as total from t",
            "select sum(amount - refund) as total from t")
        changed = by_kind(comparison, PROJECTION_EXPRESSION_CHANGED)
        self.assertEqual(changed[0]["output_name"], "total")
        self.assertIn("refund", changed[0]["after_sql"])

    def test_output_column_added_and_removed(self):
        comparison = compare_model_sql("m", "select a, b from t", "select a, c from t")
        self.assertEqual([c["output_name"] for c in by_kind(comparison, PROJECTION_ADDED)], ["c"])
        self.assertEqual([c["output_name"] for c in by_kind(comparison, PROJECTION_REMOVED)], ["b"])


class NormalisationTests(unittest.TestCase):
    """Formatting is not a semantic change; uncertainty is not equivalence."""

    def test_whitespace_and_casing_are_not_changes(self):
        comparison = compare_model_sql(
            "m", "select   a,\n   b  FROM t  WHERE x=1",
            "SELECT a, b\nfrom t\nwhere x = 1")
        self.assertTrue(comparison.evaluated)
        self.assertEqual(comparison.changes, [])

    def test_redundant_parentheses_are_not_changes(self):
        comparison = compare_model_sql(
            "m", "select (a + b) as total from t",
            "select ((a + b)) as total from t")
        self.assertEqual(comparison.changes, [])

    def test_jinja_ref_formatting_is_not_a_change(self):
        comparison = compare_model_sql(
            "m", "select a from {{ ref('stg_orders') }}",
            "select a from {{ref(\"stg_orders\")}}")
        self.assertEqual(comparison.changes, [])

    def test_a_reordered_projection_is_not_a_change(self):
        comparison = compare_model_sql("m", "select a, b from t", "select b, a from t")
        self.assertEqual(comparison.changes, [])

    def test_a_real_precedence_change_is_still_reported(self):
        """The guard on paren unwrapping: these are different sums."""
        comparison = compare_model_sql(
            "m", "select (a + b) * c as t from x", "select a + b * c as t from x")
        self.assertEqual(kinds(comparison), [PROJECTION_EXPRESSION_CHANGED])

    def test_an_equivalent_expression_spelled_differently_is_still_reported(self):
        """Deliberate: prefer a false 'changed' over a silent miss."""
        comparison = compare_model_sql(
            "m", "select a - 0 as v from t", "select a as v from t")
        self.assertEqual(kinds(comparison), [PROJECTION_EXPRESSION_CHANGED])


class UnavailableTests(unittest.TestCase):
    """Unreadable is not the same as unchanged."""

    def test_unparseable_sql_is_explicitly_unavailable(self):
        comparison = compare_model_sql("m", "select a from t", "this is not sql at all !!!")
        self.assertEqual(comparison.status, STATUS_UNAVAILABLE)
        self.assertEqual(comparison.changes, [])
        self.assertTrue(comparison.unavailable_reason)

    def test_missing_sql_is_explicitly_unavailable(self):
        for before, after in (("", "select a from t"), ("select a from t", None)):
            comparison = compare_model_sql("m", before, after)
            self.assertEqual(comparison.status, STATUS_UNAVAILABLE)
            self.assertFalse(comparison.evaluated)

    def test_unavailable_is_distinguishable_from_zero_changes(self):
        unavailable = compare_model_sql("m", None, "select a from t")
        zero = compare_model_sql("m", "select a from t", "select a from t")
        self.assertEqual(unavailable.changes, [])
        self.assertEqual(zero.changes, [])
        self.assertNotEqual(unavailable.status, zero.status)
        self.assertTrue(zero.evaluated)
        self.assertFalse(unavailable.evaluated)

    def test_select_star_is_skipped_rather_than_guessed(self):
        comparison = compare_model_sql("m", "select * from t", "select * from t")
        self.assertTrue(comparison.evaluated)
        self.assertEqual(comparison.changes, [])


class DeterminismTests(unittest.TestCase):
    def test_the_same_input_produces_the_same_output(self):
        first = compare_model_sql("fct_orders", REFUND_BASE, REFUND_HEAD).to_dict()
        second = compare_model_sql("fct_orders", REFUND_BASE, REFUND_HEAD).to_dict()
        self.assertEqual(first, second)

    def test_the_result_is_json_serializable(self):
        import json

        json.dumps(compare_model_sql("fct_orders", REFUND_BASE, REFUND_HEAD).to_dict())

    def test_no_raw_manifest_or_path_leaks_into_evidence(self):
        text = str(compare_model_sql("fct_orders", REFUND_BASE, REFUND_HEAD).to_dict())
        for banned in ("original_file_path", "compiled_code", "raw_code", "manifest"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
