import copy
import unittest

from agent.semantic_graph import (
    Edge,
    Node,
    SemanticGraph,
    build_semantic_graph,
    explain_path,
)


class SemanticGraphTests(unittest.TestCase):
    def test_graph_builds_correctly(self):
        graph = build_semantic_graph(_project_context())

        self.assertIsInstance(graph, SemanticGraph)
        self.assertEqual(graph.nodes["raw_orders"].type, "source")
        self.assertEqual(graph.nodes["stg_orders"].type, "model")
        self.assertEqual(graph.nodes["Revenue"].type, "metric")
        self.assertIn(Edge("raw_orders", "stg_orders", "source"), graph.edges)
        self.assertIn(Edge("fct_orders", "Revenue", "metric"), graph.edges)

    def test_downstream_traversal(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(graph.downstream("stg_orders"), ["fct_orders", "Revenue"])

    def test_upstream_traversal(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(graph.upstream("Revenue"), ["fct_orders", "stg_orders", "raw_orders"])

    def test_shortest_path(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(
            graph.shortest_path("raw_orders", "Revenue"),
            ["raw_orders", "stg_orders", "fct_orders", "Revenue"],
        )

    def test_affected_nodes(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(
            graph.affected_nodes(["stg_orders"]),
            ["stg_orders", "fct_orders", "Revenue"],
        )

    def test_metric_dependency(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(graph.upstream("Revenue"), ["fct_orders", "stg_orders", "raw_orders"])
        self.assertEqual(graph.shortest_path("stg_orders", "Revenue"), ["stg_orders", "fct_orders", "Revenue"])

    def test_explanation_path(self):
        graph = build_semantic_graph(_project_context())

        self.assertEqual(
            explain_path(graph, "stg_orders", "Revenue"),
            ["stg_orders", "fct_orders", "Revenue"],
        )

    def test_duplicate_edges_removed(self):
        context = _project_context()
        context["refs"].append({"parent": "stg_orders", "child": "fct_orders"})
        context["metrics"].append({"name": "Revenue", "model": "fct_orders"})

        graph = build_semantic_graph(context)

        self.assertEqual(
            graph.edges.count(Edge("stg_orders", "fct_orders", "ref")),
            1,
        )
        self.assertEqual(
            graph.edges.count(Edge("fct_orders", "Revenue", "metric")),
            1,
        )

    def test_cycles_handled_safely(self):
        context = _project_context()
        context["refs"].append({"parent": "fct_orders", "child": "stg_orders"})
        graph = build_semantic_graph(context)

        self.assertEqual(graph.downstream("stg_orders"), ["fct_orders", "Revenue"])
        self.assertEqual(graph.shortest_path("stg_orders", "Revenue"), ["stg_orders", "fct_orders", "Revenue"])

    def test_deterministic_ordering(self):
        context = {
            "models": [{"name": "fct_b"}, {"name": "stg_a"}, {"name": "fct_a"}],
            "metrics": [{"name": "Metric B", "model": "fct_b"}, {"name": "Metric A", "model": "fct_a"}],
            "refs": [
                {"parent": "stg_a", "child": "fct_b"},
                {"parent": "stg_a", "child": "fct_a"},
            ],
        }

        first = build_semantic_graph(context)
        second = build_semantic_graph(copy.deepcopy(context))

        self.assertEqual(first, second)
        self.assertEqual(first.downstream("stg_a"), ["fct_a", "fct_b", "Metric A", "Metric B"])

    def test_input_project_not_mutated(self):
        context = _project_context()
        original = copy.deepcopy(context)

        build_semantic_graph(context)

        self.assertEqual(context, original)


def _project_context():
    return {
        "sources": [{"name": "raw_orders"}],
        "models": [
            {"name": "stg_orders", "sources": ["raw_orders"]},
            {"name": "fct_orders"},
        ],
        "metrics": [{"name": "Revenue", "model": "fct_orders"}],
        "refs": [{"parent": "stg_orders", "child": "fct_orders"}],
    }


if __name__ == "__main__":
    unittest.main()
