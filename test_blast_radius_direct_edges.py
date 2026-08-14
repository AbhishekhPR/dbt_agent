"""Direct blast-radius edges: the planner must record which changed model
each downstream node actually reads.

The flat ``downstream_models`` list cannot answer that question once more than
one model changed, and a graph drawn from it would be inference presented as
evidence. These tests pin the honest behaviour: exactly the pairs dbt's own
``depends_on`` supports, never the cartesian product, and never a reconstructed
edge for a review that predates the field.
"""
from __future__ import annotations

import unittest

from agent.api.routes import _change_plan_view
from agent.metadata_evidence.collection_plan import build_collection_plan


def _model(name, deps=(), cols=("id",), schema="analytics"):
    return {"resource_type": "model", "name": name, "schema": schema,
            "alias": name, "database": "warehouse",
            "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols}}


def _plan(nodes, changed):
    manifest = {"nodes": nodes, "sources": {}}
    return build_collection_plan(base_manifest=manifest, head_manifest=manifest,
                                 changed_models=list(changed))


def _edges(plan):
    return [(e["source_model_unique_id"], e["target_model_unique_id"])
            for e in plan.direct_edges]


class DirectEdgeEvidence(unittest.TestCase):
    def test_single_changed_model_with_one_downstream(self):
        plan = _plan({
            "model.a.int_customer_orders": _model("int_customer_orders"),
            "model.a.dim_customers": _model(
                "dim_customers", ["model.a.int_customer_orders"]),
        }, ["int_customer_orders"])

        self.assertEqual(
            _edges(plan),
            [("model.a.int_customer_orders", "model.a.dim_customers")])
        self.assertEqual(plan.downstream_models, ["model.a.dim_customers"])

    def test_multiple_changed_models_do_not_produce_a_cartesian_product(self):
        """Two changed models, two downstream models, one edge each.

        The cartesian product would be four edges. Three of those would be
        assertions dbt never made.
        """
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.stg_payments": _model("stg_payments"),
            "model.a.fct_orders": _model("fct_orders", ["model.a.stg_orders"]),
            "model.a.fct_payments": _model(
                "fct_payments", ["model.a.stg_payments"]),
        }, ["stg_orders", "stg_payments"])

        self.assertEqual(_edges(plan), [
            ("model.a.stg_orders", "model.a.fct_orders"),
            ("model.a.stg_payments", "model.a.fct_payments"),
        ])
        self.assertEqual(len(plan.direct_edges), 2)

    def test_one_target_reading_two_changed_models_yields_two_edges(self):
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.stg_customers": _model("stg_customers"),
            "model.a.dim_customers": _model(
                "dim_customers",
                ["model.a.stg_orders", "model.a.stg_customers"]),
        }, ["stg_orders", "stg_customers"])

        self.assertEqual(_edges(plan), [
            ("model.a.stg_customers", "model.a.dim_customers"),
            ("model.a.stg_orders", "model.a.dim_customers"),
        ])
        # One downstream model, two truthful edges into it.
        self.assertEqual(plan.downstream_models, ["model.a.dim_customers"])

    def test_unrelated_models_are_excluded(self):
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.dim_customers": _model(
                "dim_customers", ["model.a.stg_orders"]),
            "model.a.unrelated": _model("unrelated", ["model.a.other_source"]),
            "model.a.other_source": _model("other_source"),
        }, ["stg_orders"])

        self.assertEqual(
            _edges(plan),
            [("model.a.stg_orders", "model.a.dim_customers")])

    def test_transitive_downstream_is_not_an_edge(self):
        """Graph v0 is DIRECT only: a grandchild is not a direct edge."""
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.int_orders": _model("int_orders", ["model.a.stg_orders"]),
            "model.a.mart_orders": _model("mart_orders", ["model.a.int_orders"]),
        }, ["stg_orders"])

        self.assertEqual(
            _edges(plan),
            [("model.a.stg_orders", "model.a.int_orders")])
        self.assertNotIn("model.a.mart_orders", plan.downstream_models)

    def test_a_changed_model_reading_another_changed_model_is_not_an_edge(self):
        """Both ends changed in this PR, so the target is head-derived state
        rather than production blast radius."""
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.int_orders": _model("int_orders", ["model.a.stg_orders"]),
        }, ["stg_orders", "int_orders"])

        self.assertEqual(_edges(plan), [])
        self.assertEqual(plan.downstream_models, [])

    def test_ordering_is_deterministic(self):
        nodes = {
            "model.a.zeta": _model("zeta"),
            "model.a.alpha": _model("alpha"),
            "model.a.m_one": _model("m_one", ["model.a.zeta", "model.a.alpha"]),
            "model.a.m_two": _model("m_two", ["model.a.zeta"]),
        }
        first = _edges(_plan(nodes, ["zeta", "alpha"]))
        second = _edges(_plan(dict(reversed(list(nodes.items()))),
                              ["alpha", "zeta"]))

        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_as_dict_round_trips_the_edges(self):
        plan = _plan({
            "model.a.stg_orders": _model("stg_orders"),
            "model.a.dim_customers": _model(
                "dim_customers", ["model.a.stg_orders"]),
        }, ["stg_orders"])

        payload = plan.as_dict()
        self.assertEqual(payload["direct_edges"], [{
            "source_model_unique_id": "model.a.stg_orders",
            "target_model_unique_id": "model.a.dim_customers",
        }])


class ChangePlanProjection(unittest.TestCase):
    """The API must keep 'not recorded' distinguishable from 'none found'."""

    def test_legacy_plan_without_direct_edges_projects_null(self):
        view = _change_plan_view({"payload": {"plan": {
            "changed_models": ["int_customer_orders"],
            "downstream_models": ["model.a.dim_customers"],
        }}})

        self.assertIsNone(view["direct_edges"])
        # The list view stays fully usable for legacy reviews.
        self.assertEqual(view["downstream_models"], ["model.a.dim_customers"])

    def test_recorded_but_empty_projects_empty_list(self):
        view = _change_plan_view({"payload": {"plan": {"direct_edges": []}}})
        self.assertEqual(view["direct_edges"], [])

    def test_recorded_edges_cross_the_boundary(self):
        view = _change_plan_view({"payload": {"plan": {"direct_edges": [
            {"source_model_unique_id": "model.a.stg_orders",
             "target_model_unique_id": "model.a.dim_customers"},
        ]}}})

        self.assertEqual(view["direct_edges"], [{
            "source_model_unique_id": "model.a.stg_orders",
            "target_model_unique_id": "model.a.dim_customers",
        }])

    def test_malformed_edges_are_dropped_rather_than_guessed(self):
        view = _change_plan_view({"payload": {"plan": {"direct_edges": [
            {"source_model_unique_id": "model.a.ok",
             "target_model_unique_id": "model.a.fine"},
            {"source_model_unique_id": "model.a.only_source"},
            {"source_model_unique_id": "", "target_model_unique_id": "x"},
            {"source_model_unique_id": 7, "target_model_unique_id": "x"},
            "not-an-object",
        ]}}})

        self.assertEqual(view["direct_edges"], [{
            "source_model_unique_id": "model.a.ok",
            "target_model_unique_id": "model.a.fine",
        }])

    def test_absent_payload_is_not_an_error(self):
        self.assertIsNone(_change_plan_view({})["direct_edges"])


if __name__ == "__main__":
    unittest.main()
