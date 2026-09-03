"""Downstream impact must not stop at the first hop.

The defect
----------
``build_collection_plan`` computed ``downstream_models`` as "every model whose
``depends_on`` mentions a changed model" - one hop, and nothing further. The
persisted plan is what the dashboard renders as blast radius, so a project
whose lineage runs

    int_subscription_revenue -> fct_customer_mrr
                             -> metric_recurring_revenue
                             -> executive_revenue_dashboard

reported exactly one downstream model. The other two are real consequences of
the change and were simply not walked to. Every other downstream traversal in
this repository (``dbt_project_scan`` and ``semantic_kpi_inference``) is
already transitive, so the truncation was an outlier rather than a rule.

What is deliberately NOT changed
--------------------------------
Two things stay depth-1, for reasons that are not truncation:

  * ``direct_edges`` - the graph-v0 contract that says which changed model a
    node actually reads. A grandchild does not read the changed model.
  * the COLLECTION targets - a request against a customer warehouse must stay
    bounded, and must not grow with the depth of their project.

Describing the blast radius truthfully and scanning the warehouse for all of
it are separate decisions, and this pins both.
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


def _plan(nodes, changed, **kwargs):
    manifest = {"nodes": nodes, "sources": {}}
    return build_collection_plan(base_manifest=manifest, head_manifest=manifest,
                                 changed_models=list(changed), **kwargs)


def _edges(plan):
    return [(e["source_model_unique_id"], e["target_model_unique_id"], e["depth"])
            for e in plan.downstream_edges]


#: The reported lineage, as dbt declares it.
REVENUE_CHAIN = {
    "model.a.stg_subscriptions": _model("stg_subscriptions"),
    "model.a.int_subscription_revenue": _model(
        "int_subscription_revenue", ["model.a.stg_subscriptions"]),
    "model.a.fct_customer_mrr": _model(
        "fct_customer_mrr", ["model.a.int_subscription_revenue"]),
    "model.a.metric_recurring_revenue": _model(
        "metric_recurring_revenue", ["model.a.fct_customer_mrr"]),
    "model.a.executive_revenue_dashboard": _model(
        "executive_revenue_dashboard", ["model.a.metric_recurring_revenue"]),
}


class TheFullChainIsReported(unittest.TestCase):
    def setUp(self):
        self.plan = _plan(REVENUE_CHAIN, ["int_subscription_revenue"])

    def test_every_downstream_model_appears_not_only_the_first(self):
        self.assertEqual(self.plan.downstream_models, [
            "model.a.executive_revenue_dashboard",
            "model.a.fct_customer_mrr",
            "model.a.metric_recurring_revenue",
        ])

    def test_each_hop_is_a_real_dbt_edge_at_its_own_depth(self):
        self.assertEqual(_edges(self.plan), [
            ("model.a.int_subscription_revenue", "model.a.fct_customer_mrr", 1),
            ("model.a.fct_customer_mrr", "model.a.metric_recurring_revenue", 2),
            ("model.a.metric_recurring_revenue",
             "model.a.executive_revenue_dashboard", 3),
        ])

    def test_no_edge_is_invented_between_non_adjacent_models(self):
        """The changed model is never claimed to be read by its grandchild."""
        pairs = {(s, t) for s, t, _ in _edges(self.plan)}
        self.assertNotIn(
            ("model.a.int_subscription_revenue", "model.a.metric_recurring_revenue"),
            pairs)
        self.assertNotIn(
            ("model.a.int_subscription_revenue",
             "model.a.executive_revenue_dashboard"), pairs)

    def test_the_maximum_depth_is_reported(self):
        self.assertEqual(self.plan.as_dict()["max_downstream_depth"], 3)

    def test_an_upstream_model_is_not_downstream(self):
        self.assertNotIn("model.a.stg_subscriptions", self.plan.downstream_models)


class TheCollectionScopeStaysBounded(unittest.TestCase):
    def setUp(self):
        self.plan = _plan(REVENUE_CHAIN, ["int_subscription_revenue"])

    def test_only_the_direct_downstream_is_collected(self):
        self.assertEqual(self.plan.collected_downstream_models,
                         ["model.a.fct_customer_mrr"])

    def test_the_deeper_models_are_not_collection_targets(self):
        names = {t.relation_name for t in self.plan.targets}
        self.assertIn("analytics.fct_customer_mrr", names)
        self.assertNotIn("analytics.metric_recurring_revenue", names)
        self.assertNotIn("analytics.executive_revenue_dashboard", names)

    def test_direct_edges_remain_depth_one(self):
        self.assertEqual(
            [(e["source_model_unique_id"], e["target_model_unique_id"])
             for e in self.plan.direct_edges],
            [("model.a.int_subscription_revenue", "model.a.fct_customer_mrr")])

    def test_depth_is_not_smuggled_into_the_direct_edge_contract(self):
        for edge in self.plan.direct_edges:
            self.assertEqual(set(edge), {"source_model_unique_id",
                                         "target_model_unique_id"})


class TheWalkIsWellBehaved(unittest.TestCase):
    def test_a_diamond_yields_both_real_edges_into_its_join_point(self):
        plan = _plan({
            "model.a.src": _model("src"),
            "model.a.left": _model("left", ["model.a.src"]),
            "model.a.right": _model("right", ["model.a.src"]),
            "model.a.join": _model("join", ["model.a.left", "model.a.right"]),
        }, ["src"])
        self.assertEqual(_edges(plan), [
            ("model.a.src", "model.a.left", 1),
            ("model.a.src", "model.a.right", 1),
            ("model.a.left", "model.a.join", 2),
            ("model.a.right", "model.a.join", 2),
        ])
        self.assertEqual(plan.downstream_models,
                         ["model.a.join", "model.a.left", "model.a.right"])

    def test_both_routes_to_a_node_reachable_two_ways_are_recorded(self):
        """`shortcut` reads the changed model directly AND through `mid`.

        Depth belongs to the EDGE, not to the node: both relationships are
        ones dbt declared, and collapsing them to whichever was found first
        would delete a dependency the project really has. The node's own
        distance from the change is the smallest of them.
        """
        plan = _plan({
            "model.a.src": _model("src"),
            "model.a.mid": _model("mid", ["model.a.src"]),
            "model.a.shortcut": _model("shortcut", ["model.a.src", "model.a.mid"]),
        }, ["src"])
        edges = _edges(plan)
        self.assertIn(("model.a.src", "model.a.shortcut", 1), edges)
        self.assertIn(("model.a.mid", "model.a.shortcut", 2), edges)
        self.assertEqual(
            min(d for _, t, d in edges if t == "model.a.shortcut"), 1)
        self.assertEqual(plan.downstream_models,
                         ["model.a.mid", "model.a.shortcut"])

    def test_a_cycle_terminates(self):
        """dbt forbids one; a hand-built manifest can still contain one."""
        plan = _plan({
            "model.a.one": _model("one", ["model.a.three"]),
            "model.a.two": _model("two", ["model.a.one"]),
            "model.a.three": _model("three", ["model.a.two"]),
        }, ["one"])
        self.assertEqual(plan.downstream_models,
                         ["model.a.three", "model.a.two"])

    def test_a_changed_model_is_never_its_own_blast_radius(self):
        plan = _plan({
            "model.a.src": _model("src"),
            "model.a.mid": _model("mid", ["model.a.src"]),
            "model.a.leaf": _model("leaf", ["model.a.mid"]),
        }, ["src", "mid"])
        self.assertNotIn("model.a.mid", plan.downstream_models)
        self.assertEqual(plan.downstream_models, ["model.a.leaf"])

    def test_a_change_with_no_downstream_reports_an_empty_walk(self):
        plan = _plan({"model.a.only": _model("only")}, ["only"])
        self.assertEqual(plan.downstream_models, [])
        self.assertEqual(plan.downstream_edges, [])
        self.assertEqual(plan.as_dict()["max_downstream_depth"], 0)

    def test_the_walk_is_deterministic_regardless_of_manifest_ordering(self):
        forward = _edges(_plan(REVENUE_CHAIN, ["int_subscription_revenue"]))
        reversed_nodes = dict(reversed(list(REVENUE_CHAIN.items())))
        backward = _edges(_plan(reversed_nodes, ["int_subscription_revenue"]))
        self.assertEqual(forward, backward)

    def test_the_walk_does_not_depend_on_the_plan(self):
        """Blast radius is analysis, and analysis is not what a plan buys."""
        entitled = _plan(REVENUE_CHAIN, ["int_subscription_revenue"],
                         warehouse_evidence_entitled=True)
        free = _plan(REVENUE_CHAIN, ["int_subscription_revenue"],
                     warehouse_evidence_entitled=False)
        self.assertEqual(free.downstream_models, entitled.downstream_models)
        self.assertEqual(free.downstream_edges, entitled.downstream_edges)


class TheDashboardProjectionCarriesIt(unittest.TestCase):
    """The same null-versus-empty rule the direct edges already follow."""

    def _view(self, plan_payload):
        return _change_plan_view({"payload": {"plan": plan_payload}})

    def test_recorded_edges_cross_the_boundary_with_their_depth(self):
        plan = _plan(REVENUE_CHAIN, ["int_subscription_revenue"]).as_dict()
        view = self._view(plan)
        self.assertEqual(view["downstream_edges"], plan["downstream_edges"])
        self.assertEqual(view["max_downstream_depth"], 3)
        self.assertEqual(len(view["downstream_models"]), 3)

    def test_a_review_predating_the_walk_projects_null_rather_than_empty(self):
        view = self._view({"downstream_models": ["model.a.fct_customer_mrr"]})
        self.assertIsNone(view["downstream_edges"])
        self.assertIsNone(view["collected_downstream_models"])
        self.assertIsNone(view["max_downstream_depth"])
        # The flat list stays usable for such a review.
        self.assertEqual(view["downstream_models"], ["model.a.fct_customer_mrr"])

    def test_a_change_with_no_downstream_projects_an_empty_list(self):
        view = self._view({"downstream_edges": []})
        self.assertEqual(view["downstream_edges"], [])

    def test_an_edge_with_no_readable_depth_is_dropped_rather_than_guessed(self):
        view = self._view({"downstream_edges": [
            {"source_model_unique_id": "model.a.ok",
             "target_model_unique_id": "model.a.also_ok", "depth": 2},
            {"source_model_unique_id": "model.a.bad",
             "target_model_unique_id": "model.a.also_bad"},
            {"source_model_unique_id": "model.a.bad",
             "target_model_unique_id": "model.a.also_bad", "depth": True},
            {"source_model_unique_id": "model.a.bad",
             "target_model_unique_id": "model.a.also_bad", "depth": 0},
        ]})
        self.assertEqual(view["downstream_edges"], [{
            "source_model_unique_id": "model.a.ok",
            "target_model_unique_id": "model.a.also_ok", "depth": 2}])


if __name__ == "__main__":
    unittest.main()
