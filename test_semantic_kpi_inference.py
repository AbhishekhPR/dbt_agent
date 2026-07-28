import copy
import unittest

from agent.kpi_discovery import DiscoveredKPI
from agent.semantic_graph import build_semantic_graph
from agent.semantic_kpi_inference import ImpactedKPI, KPIImpactReport, infer_impacted_kpis, to_signal
from agent.signals import Severity


class SemanticKPIInferenceTests(unittest.TestCase):
    def test_direct_changed_model_impacts_kpi(self):
        kpi = _kpi(
            name="Revenue / GMV",
            related_models=["fct_orders"],
            related_columns=["gross_revenue"],
            reasons=["Revenue evidence found in orders mart"],
        )

        report = infer_impacted_kpis(changed_models=["fct_orders"], discovered_kpis=[kpi])

        self.assertIsInstance(report, KPIImpactReport)
        self.assertEqual([impact.name for impact in report.impacted_kpis], ["Revenue / GMV"])
        impact = report.impacted_kpis[0]
        self.assertIsInstance(impact, ImpactedKPI)
        self.assertGreaterEqual(impact.confidence, 90)
        self.assertEqual(impact.impacted_by_models, ["fct_orders"])
        self.assertEqual(impact.related_columns, ["gross_revenue"])
        self.assertIn("Revenue evidence found in orders mart", impact.reasons)
        self.assertIn("Direct model match: fct_orders", impact.reasons)

    def test_downstream_lineage_impacts_kpi(self):
        kpi = _kpi(name="Revenue / GMV", related_models=["fct_orders"])
        lineage = {
            "stg_orders": ["int_orders"],
            "int_orders": ["fct_orders"],
        }

        report = infer_impacted_kpis(
            changed_models=["stg_orders"],
            discovered_kpis=[kpi],
            lineage=lineage,
        )

        impact = report.impacted_kpis[0]
        self.assertEqual(impact.impacted_by_models, ["fct_orders"])
        self.assertGreaterEqual(impact.confidence, 80)
        self.assertLess(impact.confidence, 90)
        self.assertIn("Downstream lineage match: stg_orders -> fct_orders", impact.reasons)

    def test_unrelated_kpi_remains_unaffected(self):
        revenue = _kpi(name="Revenue / GMV", related_models=["fct_orders"])
        playback = _kpi(name="Playback Reliability", related_models=["fct_playback_sessions"])

        report = infer_impacted_kpis(
            changed_models=["stg_weather"],
            discovered_kpis=[revenue, playback],
            lineage={"stg_weather": ["fct_weather"]},
        )

        self.assertEqual(report.impacted_kpis, [])
        self.assertEqual(
            [kpi.name for kpi in report.unaffected_kpis],
            ["Playback Reliability", "Revenue / GMV"],
        )
        self.assertEqual(report.confidence, 0)

    def test_multiple_changed_models_aggregate_correctly(self):
        kpi = _kpi(
            name="Revenue / GMV",
            related_models=["fct_orders", "fct_payments"],
        )
        lineage = {
            "stg_orders": ["fct_orders"],
            "stg_payments": ["int_payments"],
            "int_payments": ["fct_payments"],
        }

        report = infer_impacted_kpis(
            changed_models=["stg_payments", "stg_orders"],
            discovered_kpis=[kpi],
            lineage=lineage,
        )

        impact = report.impacted_kpis[0]
        self.assertEqual(impact.impacted_by_models, ["fct_orders", "fct_payments"])
        self.assertEqual(
            impact.metadata["supporting_matches"],
            ["stg_orders -> fct_orders", "stg_payments -> fct_payments"],
        )

    def test_confidence_increases_with_multiple_supporting_matches(self):
        sparse = _kpi(name="Revenue / GMV", related_models=["fct_orders"])
        rich = _kpi(name="Revenue / GMV", related_models=["fct_orders", "fct_payments"])

        single = infer_impacted_kpis(
            changed_models=["fct_orders"],
            discovered_kpis=[sparse],
        ).impacted_kpis[0]
        multiple = infer_impacted_kpis(
            changed_models=["fct_orders", "fct_payments"],
            discovered_kpis=[rich],
        ).impacted_kpis[0]

        self.assertGreater(multiple.confidence, single.confidence)
        self.assertLessEqual(multiple.confidence, 100)

    def test_related_columns_are_preserved(self):
        kpi = _kpi(
            name="Churn / Retention",
            related_models=["fct_subscriptions"],
            related_columns=["churn_rate", "subscription_status"],
        )

        report = infer_impacted_kpis(
            changed_models=["fct_subscriptions"],
            discovered_kpis=[kpi],
        )

        self.assertEqual(
            report.impacted_kpis[0].related_columns,
            ["churn_rate", "subscription_status"],
        )

    def test_semantic_graph_path_impacts_kpi(self):
        graph = _semantic_graph()
        kpi = _kpi(name="Revenue", related_models=[])

        report = infer_impacted_kpis(
            changed_models=["stg_orders"],
            discovered_kpis=[kpi],
            semantic_graph=graph,
        )

        self.assertEqual([impact.name for impact in report.impacted_kpis], ["Revenue"])
        self.assertGreaterEqual(report.impacted_kpis[0].confidence, 90)

    def test_impact_path_is_preserved_in_metadata(self):
        graph = _semantic_graph()
        kpi = _kpi(name="Revenue", related_models=[])

        impact = infer_impacted_kpis(
            changed_models=["stg_orders"],
            discovered_kpis=[kpi],
            semantic_graph=graph,
        ).impacted_kpis[0]

        self.assertEqual(
            impact.metadata["impact_paths"],
            [["stg_orders", "fct_orders", "Revenue"]],
        )

    def test_human_readable_reason_includes_graph_path(self):
        graph = _semantic_graph()
        kpi = _kpi(name="Revenue", related_models=[])

        impact = infer_impacted_kpis(
            changed_models=["stg_orders"],
            discovered_kpis=[kpi],
            semantic_graph=graph,
        ).impacted_kpis[0]

        self.assertIn(
            "Revenue is impacted through stg_orders → fct_orders → Revenue",
            impact.reasons,
        )

    def test_multiple_semantic_paths_increase_confidence(self):
        graph = build_semantic_graph(
            {
                "models": [
                    {"name": "stg_orders"},
                    {"name": "stg_payments"},
                    {"name": "fct_revenue"},
                ],
                "refs": [
                    {"parent": "stg_orders", "child": "fct_revenue"},
                    {"parent": "stg_payments", "child": "fct_revenue"},
                ],
                "metrics": [{"name": "Revenue", "model": "fct_revenue"}],
            }
        )
        kpi = _kpi(name="Revenue", related_models=[])

        single = infer_impacted_kpis(
            changed_models=["stg_orders"],
            discovered_kpis=[kpi],
            semantic_graph=graph,
        ).impacted_kpis[0]
        multiple = infer_impacted_kpis(
            changed_models=["stg_orders", "stg_payments"],
            discovered_kpis=[kpi],
            semantic_graph=graph,
        ).impacted_kpis[0]

        self.assertGreater(multiple.confidence, single.confidence)
        self.assertLessEqual(multiple.confidence, 100)

    def test_output_ordering_is_deterministic(self):
        kpis = [
            _kpi(name="Revenue / GMV", related_models=["fct_orders"]),
            _kpi(name="Churn / Retention", related_models=["fct_subscriptions"]),
        ]
        changed_models = ["fct_orders", "fct_subscriptions"]

        first = infer_impacted_kpis(changed_models=changed_models, discovered_kpis=kpis)
        second = infer_impacted_kpis(changed_models=changed_models, discovered_kpis=list(reversed(kpis)))

        self.assertEqual(first, second)
        self.assertEqual(
            [impact.name for impact in first.impacted_kpis],
            ["Churn / Retention", "Revenue / GMV"],
        )

    def test_inputs_are_not_mutated(self):
        changed_models = ["stg_orders"]
        discovered_kpis = [
            _kpi(
                name="Revenue / GMV",
                related_models=["fct_orders"],
                related_columns=["gross_revenue"],
                reasons=["Revenue evidence"],
                metadata={"matched_sources": ["model_names"]},
            )
        ]
        lineage = {"stg_orders": ["fct_orders"]}
        semantic_graph = _semantic_graph()
        original_changed_models = copy.deepcopy(changed_models)
        original_discovered_kpis = copy.deepcopy(discovered_kpis)
        original_lineage = copy.deepcopy(lineage)
        original_semantic_graph = copy.deepcopy(semantic_graph)

        infer_impacted_kpis(
            changed_models=changed_models,
            discovered_kpis=discovered_kpis,
            lineage=lineage,
            semantic_graph=semantic_graph,
        )

        self.assertEqual(changed_models, original_changed_models)
        self.assertEqual(discovered_kpis, original_discovered_kpis)
        self.assertEqual(lineage, original_lineage)
        self.assertEqual(semantic_graph, original_semantic_graph)

    def test_high_confidence_impact_is_high_context_but_neutral_signal(self):
        report = _impact_report(confidence=95)

        signal = to_signal(report)

        self.assertEqual(signal.component, "kpi_impact")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 95)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])
        self.assertEqual(signal.metadata["context_severity"], "HIGH")
        self.assertIn(
            "Revenue impacted by stg_orders, fct_orders",
            signal.metadata["context_reasons"],
        )

    def test_medium_impact_is_medium_context_but_neutral_signal(self):
        report = _impact_report(confidence=85)

        signal = to_signal(report)

        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 85)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])
        self.assertEqual(signal.metadata["context_severity"], "MEDIUM")

    def test_no_impacted_kpis_becomes_low_signal(self):
        report = KPIImpactReport(
            changed_models=["stg_weather"],
            impacted_kpis=[],
            unaffected_kpis=[_kpi(name="Revenue", related_models=["fct_orders"])],
            confidence=0,
            reasons=[],
            metadata={"semantic_graph_provided": True},
        )

        signal = to_signal(report)

        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 0)
        self.assertEqual(signal.score, 0)

    def test_signal_metadata_preserves_impacted_kpi_names(self):
        signal = to_signal(_impact_report(confidence=95))

        self.assertEqual(signal.metadata["impacted_kpis"], ["Revenue"])
        self.assertEqual(signal.metadata["unaffected_kpis"], ["Churn / Retention"])

    def test_signal_metadata_preserves_impact_paths(self):
        signal = to_signal(_impact_report(confidence=95))

        self.assertEqual(
            signal.metadata["impact_paths"],
            [["stg_orders", "fct_orders", "Revenue"]],
        )

    def test_context_reasons_include_semantic_path_explanation(self):
        signal = to_signal(_impact_report(confidence=95))

        self.assertEqual(signal.reasons, [])
        self.assertIn(
            "Revenue impacted by stg_orders, fct_orders",
            signal.metadata["context_reasons"],
        )
        self.assertIn(
            "Revenue is impacted through stg_orders → fct_orders → Revenue",
            signal.metadata["context_reasons"],
        )

    def test_to_signal_does_not_mutate_report(self):
        report = _impact_report(confidence=95)
        original = copy.deepcopy(report)

        to_signal(report)

        self.assertEqual(report, original)


def _kpi(
    *,
    name,
    related_models,
    related_columns=None,
    reasons=None,
    metadata=None,
):
    return DiscoveredKPI(
        name=name,
        description=f"{name} description",
        industry_hint=None,
        related_models=list(related_models),
        related_columns=list(related_columns or []),
        confidence=75,
        reasons=list(reasons or []),
        metadata=dict(metadata or {}),
    )


def _semantic_graph():
    return build_semantic_graph(
        {
            "models": [
                {"name": "stg_orders"},
                {"name": "fct_orders"},
            ],
            "refs": [{"parent": "stg_orders", "child": "fct_orders"}],
            "metrics": [{"name": "Revenue", "model": "fct_orders"}],
        }
    )


def _impact_report(confidence):
    return KPIImpactReport(
        changed_models=["stg_orders"],
        impacted_kpis=[
            ImpactedKPI(
                name="Revenue",
                confidence=confidence,
                impacted_by_models=["stg_orders", "fct_orders"],
                related_columns=["gross_revenue"],
                reasons=[
                    "Revenue evidence",
                    "Revenue is impacted through stg_orders → fct_orders → Revenue",
                ],
                metadata={
                    "impact_paths": [["stg_orders", "fct_orders", "Revenue"]],
                    "source_kpi_confidence": 75,
                },
            )
        ],
        unaffected_kpis=[_kpi(name="Churn / Retention", related_models=["fct_subscriptions"])],
        confidence=confidence,
        reasons=["Revenue impacted by stg_orders, fct_orders"],
        metadata={"semantic_graph_provided": True, "impacted_count": 1},
    )


if __name__ == "__main__":
    unittest.main()
