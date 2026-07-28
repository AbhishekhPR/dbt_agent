import copy
import json
import unittest
from dataclasses import asdict

from agent.kpi_discovery import DiscoveredKPI
from agent.semantic_graph import build_semantic_graph
from agent.semantic_knowledge import (
    KnowledgeReport,
    SemanticContract,
    build_semantic_knowledge,
)


class SemanticKnowledgeTests(unittest.TestCase):
    def test_revenue_contract(self):
        report = build_semantic_knowledge(
            discovered_kpis=[
                _kpi(
                    "Revenue",
                    related_models=["fct_payments"],
                    related_columns=["gross_revenue"],
                )
            ],
            semantic_graph=_revenue_graph(),
            project_context={"business_terms": ["completed customer payments"]},
        )

        contract = report.contracts[0]

        self.assertIsInstance(report, KnowledgeReport)
        self.assertIsInstance(contract, SemanticContract)
        self.assertEqual(contract.kpi_name, "Revenue")
        self.assertEqual(contract.business_meaning, "Represents completed customer payments.")
        self.assertIn("completed payments only", contract.assumptions)
        self.assertIn("never negative", contract.invariants)

    def test_mrr_contract(self):
        report = build_semantic_knowledge(
            discovered_kpis=[
                _kpi(
                    "MRR",
                    description="Monthly recurring revenue",
                    related_models=["fct_subscriptions"],
                    related_columns=["mrr"],
                )
            ],
            semantic_graph=_subscription_graph(),
            project_context={"business_terms": ["subscription lifecycle recurring revenue"]},
        )

        contract = report.contracts[0]

        self.assertEqual(contract.business_meaning, "Represents recurring subscription revenue.")
        self.assertIn("subscription lifecycle preserved", contract.assumptions)
        self.assertIn("never negative", contract.invariants)

    def test_streaming_kpi_contract(self):
        report = build_semantic_knowledge(
            discovered_kpis=[
                _kpi(
                    "Playback Success Rate",
                    related_models=["fct_playback_sessions"],
                    related_columns=["successful_sessions", "playback_attempts"],
                )
            ],
            semantic_graph=_streaming_graph(),
            project_context={"business_terms": ["successful playback sessions"]},
        )

        contract = report.contracts[0]

        self.assertEqual(contract.business_meaning, "Represents successful playback sessions.")
        self.assertIn("successful session events required", contract.assumptions)
        self.assertIn("between 0 and 100%", contract.invariants)

    def test_related_models_preserved(self):
        report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Revenue", related_models=["fct_payments"])],
            semantic_graph=_revenue_graph(),
            project_context={},
        )

        self.assertIn("fct_payments", report.contracts[0].related_models)

    def test_graph_dependencies_preserved(self):
        report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Revenue", related_models=["fct_payments"])],
            semantic_graph=_revenue_graph(),
            project_context={},
        )

        contract = report.contracts[0]

        self.assertEqual(contract.upstream_sources, ["raw_payments"])
        self.assertIn("stg_payments", contract.related_models)
        self.assertEqual(contract.downstream_consumers, ["executive_revenue_dashboard"])

    def test_assumptions_inferred(self):
        report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Retention", related_columns=["retention_rate"])],
            semantic_graph=build_semantic_graph({"metrics": [{"name": "Retention"}]}),
            project_context={"business_terms": ["active users cohort definitions"]},
        )

        self.assertIn("active users exist", report.contracts[0].assumptions)
        self.assertIn("cohort definitions unchanged", report.contracts[0].assumptions)

    def test_invariants_inferred(self):
        report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Conversion", related_columns=["conversion_rate"])],
            semantic_graph=build_semantic_graph({"metrics": [{"name": "Conversion"}]}),
            project_context={},
        )

        self.assertEqual(report.contracts[0].invariants, ["between 0 and 100%"])

    def test_confidence_increases_with_more_supporting_evidence(self):
        sparse_report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Revenue", confidence=45)],
            semantic_graph=build_semantic_graph({"metrics": [{"name": "Revenue"}]}),
            project_context={"column_names": ["revenue"]},
        )
        rich_report = build_semantic_knowledge(
            discovered_kpis=[
                _kpi(
                    "Revenue",
                    related_models=["fct_payments"],
                    related_columns=["gross_revenue", "payment_amount"],
                    confidence=45,
                )
            ],
            semantic_graph=_revenue_graph(),
            project_context={
                "model_names": ["fct_payments"],
                "column_names": ["gross_revenue", "payment_amount"],
                "dashboard_names": ["Revenue Dashboard"],
                "business_terms": ["completed customer payments"],
            },
        )

        self.assertGreater(rich_report.contracts[0].confidence, sparse_report.contracts[0].confidence)
        self.assertGreater(rich_report.confidence, sparse_report.confidence)

    def test_inputs_never_mutated(self):
        discovered_kpis = [_kpi("Revenue", related_models=["fct_payments"])]
        graph = _revenue_graph()
        project_context = {
            "business_terms": ["completed customer payments"],
            "column_names": ["gross_revenue"],
        }
        original_kpis = copy.deepcopy(discovered_kpis)
        original_graph = copy.deepcopy(graph)
        original_context = copy.deepcopy(project_context)

        build_semantic_knowledge(
            discovered_kpis=discovered_kpis,
            semantic_graph=graph,
            project_context=project_context,
        )

        self.assertEqual(discovered_kpis, original_kpis)
        self.assertEqual(graph, original_graph)
        self.assertEqual(project_context, original_context)

    def test_serializable(self):
        report = build_semantic_knowledge(
            discovered_kpis=[_kpi("Revenue", related_models=["fct_payments"])],
            semantic_graph=_revenue_graph(),
            project_context={},
        )

        json.dumps(asdict(report))


def _kpi(
    name,
    *,
    description=None,
    related_models=None,
    related_columns=None,
    confidence=70,
):
    return DiscoveredKPI(
        name=name,
        description=description or f"{name} description",
        related_models=list(related_models or []),
        related_columns=list(related_columns or []),
        confidence=confidence,
        reasons=[f"{name} evidence"],
        metadata={"matched_sources": ["unit_test"]},
    )


def _revenue_graph():
    return build_semantic_graph(
        {
            "sources": [{"name": "raw_payments"}],
            "models": [
                {"name": "stg_payments", "sources": ["raw_payments"]},
                {"name": "fct_payments"},
            ],
            "refs": [{"parent": "stg_payments", "child": "fct_payments"}],
            "metrics": [{"name": "Revenue", "model": "fct_payments"}],
            "exposures": [{"name": "executive_revenue_dashboard", "depends_on": ["Revenue"]}],
        }
    )


def _subscription_graph():
    return build_semantic_graph(
        {
            "sources": [{"name": "raw_subscriptions"}],
            "models": [
                {"name": "stg_subscriptions", "sources": ["raw_subscriptions"]},
                {"name": "fct_subscriptions"},
            ],
            "refs": [{"parent": "stg_subscriptions", "child": "fct_subscriptions"}],
            "metrics": [{"name": "MRR", "model": "fct_subscriptions"}],
        }
    )


def _streaming_graph():
    return build_semantic_graph(
        {
            "sources": [{"name": "raw_playback_events"}],
            "models": [
                {"name": "stg_playback_events", "sources": ["raw_playback_events"]},
                {"name": "fct_playback_sessions"},
            ],
            "refs": [{"parent": "stg_playback_events", "child": "fct_playback_sessions"}],
            "metrics": [{"name": "Playback Success Rate", "model": "fct_playback_sessions"}],
        }
    )


if __name__ == "__main__":
    unittest.main()
