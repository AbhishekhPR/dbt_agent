import copy
import json
import unittest

from agent.semantic_context import SemanticContext, build_semantic_context, to_dict
from agent.semantic_graph import SemanticGraph
from agent.semantic_knowledge import KnowledgeReport
from agent.semantic_kpi_inference import KPIImpactReport


class SemanticContextTests(unittest.TestCase):
    def test_context_builds_with_discovered_kpis(self):
        context = build_semantic_context(project_context=_project_context())

        self.assertIsInstance(context, SemanticContext)
        self.assertTrue(context.discovered_kpis)
        self.assertEqual(context.discovered_kpis[0].name, "Revenue / GMV")

    def test_context_builds_semantic_graph(self):
        context = build_semantic_context(project_context=_project_context())

        self.assertIsInstance(context.semantic_graph, SemanticGraph)
        self.assertIn("fct_orders", context.semantic_graph.nodes)
        self.assertIn("Revenue / GMV", context.semantic_graph.nodes)

    def test_context_includes_kpi_impact_report_when_changed_models_exist(self):
        context = build_semantic_context(
            project_context=_project_context(),
            changed_models=["stg_orders"],
        )

        self.assertIsInstance(context.kpi_impact_report, KPIImpactReport)
        self.assertEqual(
            [impact.name for impact in context.kpi_impact_report.impacted_kpis],
            ["Revenue / GMV"],
        )

    def test_context_includes_semantic_knowledge_report(self):
        context = build_semantic_context(project_context=_project_context())

        self.assertIsInstance(context.knowledge_report, KnowledgeReport)
        self.assertEqual(
            [contract.kpi_name for contract in context.knowledge_report.contracts],
            ["Revenue / GMV"],
        )

    def test_context_includes_contract_validation_result(self):
        context = build_semantic_context(
            project_context=_project_context(),
            changed_models=["fct_orders"],
            metadata={"metric_values": {"gross_revenue": -1}},
        )

        self.assertEqual(context.contract_validation_result["severity"], "HIGH")
        self.assertEqual(
            context.contract_validation_result["metadata"]["violated_invariants"],
            {"Revenue / GMV": ["never negative"]},
        )

    def test_context_includes_column_lineage_graph(self):
        context = build_semantic_context(project_context=_project_context())

        self.assertIn("fct_orders", context.column_lineage_graph.models)
        self.assertEqual(
            context.column_lineage_graph.models["fct_orders"].output_columns,
            ["gross_revenue", "order_id"],
        )

    def test_context_includes_assumption_verification_report(self):
        context = build_semantic_context(project_context=_project_context())

        check_types = [
            check["check_type"]
            for check in context.assumption_verification.to_dict()["checks"]
        ]

        self.assertIn("non_negative", check_types)
        self.assertIn("model_not_empty", check_types)
        self.assertTrue(
            all(
                not check["evaluated"]
                for check in context.assumption_verification.to_dict()["checks"]
            )
        )

    def test_context_serializes_to_dict(self):
        context = build_semantic_context(
            project_context=_project_context(),
            changed_models=["stg_orders"],
            metadata={"metric_values": {"gross_revenue": 100}},
        )

        payload = context.to_dict()

        json.dumps(payload)
        self.assertEqual(payload["discovered_kpis"][0]["name"], "Revenue / GMV")
        self.assertEqual(payload["kpi_impact_report"]["impacted_kpis"][0]["name"], "Revenue / GMV")
        self.assertIn("column_lineage_graph", payload)
        self.assertIn("assumption_verification", payload)
        self.assertEqual(to_dict(context), payload)

    def test_missing_sql_does_not_crash_column_lineage(self):
        context = build_semantic_context(
            project_context={
                "models": [{"name": "fct_orders", "columns": ["gross_revenue"]}],
                "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                "column_names": ["gross_revenue"],
            },
            changed_models=["fct_orders"],
        )

        self.assertEqual(
            context.column_lineage_graph.models["fct_orders"].unknown_columns,
            ["gross_revenue"],
        )
        json.dumps(context.to_dict())

    def test_inputs_are_not_mutated(self):
        project_context = _project_context()
        changed_models = ["stg_orders"]
        metadata = {"metric_values": {"gross_revenue": 100}}
        original_project_context = copy.deepcopy(project_context)
        original_changed_models = copy.deepcopy(changed_models)
        original_metadata = copy.deepcopy(metadata)

        context = build_semantic_context(
            project_context=project_context,
            changed_models=changed_models,
            metadata=metadata,
        )
        context.project_context["model_names"].append("mutated")

        self.assertEqual(project_context, original_project_context)
        self.assertEqual(changed_models, original_changed_models)
        self.assertEqual(metadata, original_metadata)

    def test_empty_project_context_handled_gracefully(self):
        context = build_semantic_context(project_context={})

        self.assertEqual(context.project_context, {})
        self.assertEqual(context.discovered_kpis, [])
        self.assertEqual(context.semantic_graph.nodes, {})
        self.assertIsNone(context.kpi_impact_report)
        self.assertEqual(context.knowledge_report.contracts, [])
        self.assertEqual(context.contract_validation_result["severity"], "LOW")
        self.assertEqual(context.column_lineage_graph.models, {})
        self.assertEqual(context.assumption_verification.checks, [])
        json.dumps(context.to_dict())


def _project_context():
    return {
        "model_names": ["stg_orders", "fct_orders"],
        "column_names": ["gross_revenue", "order_id"],
        "business_terms": ["completed customer payments"],
        "sources": [{"name": "raw_orders"}],
        "models": [
            {
                "name": "stg_orders",
                "sources": ["raw_orders"],
                "columns": ["order_id", "payment_amount"],
                "sql": "select order_id, payment_amount from raw_orders",
            },
            {
                "name": "fct_orders",
                "columns": ["gross_revenue", "order_id"],
                "sql": (
                    "select order_id, "
                    "sum(payment_amount) as gross_revenue "
                    "from stg_orders group by order_id"
                ),
            },
        ],
        "refs": [{"parent": "stg_orders", "child": "fct_orders"}],
        "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
    }


if __name__ == "__main__":
    unittest.main()
