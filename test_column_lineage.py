import copy
import json
import unittest

from agent.column_lineage import (
    ColumnLineageGraph,
    build_column_lineage_graph,
)


class ColumnLineageTests(unittest.TestCase):
    def test_extracts_direct_column_lineage(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select customer_id from stg_orders",
                output_columns=["customer_id"],
            )
        )

        lineage = graph.models["fct_revenue"]

        self.assertEqual(lineage.output_columns, ["customer_id"])
        self.assertEqual(len(lineage.edges), 1)
        self.assertEqual(lineage.edges[0].from_column, "customer_id")
        self.assertEqual(lineage.edges[0].to_column, "customer_id")
        self.assertEqual(lineage.edges[0].confidence, 0.7)

    def test_extracts_aliased_column_lineage(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select o.customer_id as customer_id from stg_orders o",
                output_columns=["customer_id"],
            )
        )

        edge = graph.models["fct_revenue"].edges[0]

        self.assertEqual(edge.from_model, "stg_orders")
        self.assertEqual(edge.from_column, "customer_id")
        self.assertEqual(edge.relation_alias, "o")
        self.assertEqual(edge.confidence, 0.95)

    def test_extracts_aggregate_dependency(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select sum(o.order_total) as revenue from stg_orders o",
                output_columns=["revenue"],
            )
        )

        edge = graph.models["fct_revenue"].edges[0]

        self.assertEqual(edge.from_model, "stg_orders")
        self.assertEqual(edge.from_column, "order_total")
        self.assertEqual(edge.to_column, "revenue")

    def test_extracts_case_expression_dependency(self):
        graph = build_column_lineage_graph(
            _project_context(
                """
                select
                  case when status = 'completed' then order_total else 0 end as revenue
                from stg_orders
                """,
                output_columns=["revenue"],
            )
        )

        from_columns = {
            edge.from_column
            for edge in graph.models["fct_revenue"].edges
        }

        self.assertEqual(from_columns, {"status", "order_total"})

    def test_extracts_arithmetic_dependency(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select revenue - refunds as net_revenue from fct_revenue",
                output_columns=["net_revenue"],
            )
        )

        from_columns = {
            edge.from_column
            for edge in graph.models["fct_revenue"].edges
        }

        self.assertEqual(from_columns, {"revenue", "refunds"})

    def test_handles_unqualified_columns(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select count(distinct order_id) as orders from stg_orders",
                output_columns=["orders"],
            )
        )

        edge = graph.models["fct_revenue"].edges[0]

        self.assertIsNone(edge.from_model)
        self.assertEqual(edge.from_column, "order_id")
        self.assertEqual(edge.confidence, 0.7)

    def test_handles_unknown_sql_safely(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select * from {{ ref('stg_orders') }}",
                output_columns=["customer_id", "debug_flag"],
            )
        )

        lineage = graph.models["fct_revenue"]

        self.assertEqual(lineage.edges, [])
        self.assertEqual(lineage.unknown_columns, ["customer_id", "debug_flag"])
        self.assertEqual(lineage.metadata["status"], "unknown")

    def test_output_is_json_serializable(self):
        graph = build_column_lineage_graph(
            _project_context(
                "select o.customer_id as customer_id from stg_orders o",
                output_columns=["customer_id"],
            )
        )

        payload = graph.to_dict()

        json.dumps(payload)
        self.assertEqual(ColumnLineageGraph.from_dict(payload).to_dict(), payload)

    def test_inputs_are_not_mutated(self):
        project_context = _project_context(
            "select customer_id from stg_orders",
            output_columns=["customer_id"],
        )
        original = copy.deepcopy(project_context)

        graph = build_column_lineage_graph(project_context)
        graph.models["fct_revenue"].output_columns.append("mutated")

        self.assertEqual(project_context, original)


def _project_context(sql, *, output_columns):
    return {
        "models": [
            {
                "name": "fct_revenue",
                "columns": list(output_columns),
                "sql": sql,
            }
        ]
    }


if __name__ == "__main__":
    unittest.main()
