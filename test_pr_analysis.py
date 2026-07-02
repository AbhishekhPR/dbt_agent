import copy
import unittest
from unittest.mock import sentinel

from agent.decision_engine import DeploymentDecision
from agent.pr_analysis import analyze_changed_models
from agent.signals import Severity, Signal


class PrAnalysisTests(unittest.TestCase):
    def test_multiple_models_are_analyzed_and_one_incident_is_created(self):
        changed_models = [
            {
                "model_name": "orders",
                "sql": "select * from raw_orders",
                "conn": sentinel.conn,
                "key_columns": ["order_id"],
                "project_path": "project",
                "changed_columns": ["order_id"],
                "history": {
                    "deployment_count": 10,
                    "incident_count": 0,
                    "rollback_count": 0,
                    "average_health_score": 98,
                },
                "metadata_db_path": "metrics.db",
                "project_name": "analytics",
            },
            {
                "model_name": "customers",
                "sql": "select * from raw_customers",
                "conn": sentinel.conn,
                "key_columns": ["customer_id"],
                "project_path": "project",
                "changed_columns": ["customer_id"],
                "history": {
                    "deployment_count": 3,
                    "incident_count": 2,
                    "rollback_count": 1,
                    "average_health_score": 72,
                },
                "metadata_db_path": "metrics.db",
                "project_name": "analytics",
            },
        ]

        with _patched_detectors() as calls:
            incident = analyze_changed_models(changed_models)

        self.assertEqual(len(calls["ast"]), 2)
        self.assertEqual(len(calls["metadata"]), 2)
        self.assertEqual(len(calls["drift"]), 2)
        self.assertEqual(len(calls["blast"]), 2)
        self.assertEqual(len(calls["history"]), 2)
        self.assertEqual(len(incident.signals), 10)
        self.assertEqual(incident.affected_models, ["orders", "customers"])
        self.assertEqual(incident.metadata["model_count"], 2)
        self.assertEqual(incident.metadata["signal_count"], 10)
        self.assertEqual(incident.decision, DeploymentDecision.BLOCK)

    def test_signals_from_every_model_are_preserved_with_model_attribution(self):
        changed_models = [
            {"model_name": "orders", "sql": "select 1"},
            {"model_name": "customers", "sql": "select 1"},
        ]

        with _patched_detectors():
            incident = analyze_changed_models(changed_models)

        self.assertEqual(
            [signal.metadata["model_name"] for signal in incident.signals],
            [
                "orders",
                "orders",
                "orders",
                "orders",
                "orders",
                "customers",
                "customers",
                "customers",
                "customers",
                "customers",
            ],
        )
        self.assertEqual(
            [signal.metadata["source_component"] for signal in incident.signals],
            [
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
            ],
        )

    def test_highest_severity_propagates_and_confidence_averages(self):
        changed_models = [
            {"model_name": "orders", "sql": "select 1"},
            {"model_name": "customers", "sql": "select 1"},
        ]

        with _patched_detectors():
            incident = analyze_changed_models(changed_models)

        self.assertEqual(incident.severity, Severity.CRITICAL)
        self.assertEqual(incident.confidence, 85)

    def test_inputs_are_never_mutated(self):
        changed_models = [
            {
                "model_name": "orders",
                "sql": "select 1",
                "key_columns": ["order_id"],
                "changed_columns": ["status"],
                "history": {
                    "deployment_count": 10,
                    "incident_count": 0,
                    "rollback_count": 0,
                    "average_health_score": 98,
                },
            }
        ]
        original = copy.deepcopy(changed_models)

        with _patched_detectors():
            incident = analyze_changed_models(changed_models)
        incident.signals[0].metadata["model_name"] = "mutated"

        self.assertEqual(changed_models, original)

    def test_business_metrics_are_analyzed_when_events_are_available(self):
        changed_models = [
            {
                "model_name": "orders",
                "sql": "select 1",
                "business_events": [
                    {
                        "event_type": "pickup",
                        "pickup_status": "failed",
                    }
                ],
                "business_metric_baseline": {
                    "carts_delivered_wrong_staging_area_and_late": 0,
                    "mis_sorts": 0,
                    "totes_loaded_in_incorrect_order": 0,
                    "failed_pickups": 0,
                    "overflow_avalanches": 0,
                    "total_events": 1,
                },
            }
        ]

        with _patched_detectors() as calls:
            incident = analyze_changed_models(changed_models)

        self.assertEqual(len(calls["business_metrics"]), 1)
        self.assertEqual(len(incident.signals), 6)
        business_signal = incident.signals[-1]
        self.assertEqual(business_signal.component, "business_metrics")
        self.assertEqual(business_signal.metadata["model_name"], "orders")
        self.assertEqual(
            business_signal.metadata["metrics"]["failed_pickups"],
            1,
        )
        self.assertEqual(
            business_signal.metadata["baseline"]["failed_pickups"],
            0,
        )
        self.assertEqual(
            business_signal.metadata["spike_percentages"]["failed_pickups"],
            100.0,
        )

    def test_kpi_impact_signal_participates_in_deployment_decision(self):
        changed_models = [
            {
                "model_name": "stg_orders",
                "sql": "select * from raw_orders",
                "project_context": {
                    "model_names": ["stg_orders", "fct_orders"],
                    "column_names": ["gross_revenue"],
                    "models": [{"name": "stg_orders"}, {"name": "fct_orders"}],
                    "refs": [{"parent": "stg_orders", "child": "fct_orders"}],
                    "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                },
            }
        ]

        with _patched_detectors() as calls:
            incident = analyze_changed_models(changed_models)

        self.assertEqual(len(calls["kpi_discovery"]), 1)
        self.assertEqual(len(calls["semantic_graph"]), 1)
        self.assertEqual(len(calls["semantic_kpi_inference"]), 1)
        self.assertEqual(len(calls["kpi_impact_signal"]), 1)
        self.assertEqual(incident.signals[-1].component, "kpi_impact")
        self.assertEqual(incident.health, 0)
        self.assertEqual(
            incident.signals[-1].metadata["impacted_kpis"],
            ["Revenue / GMV"],
        )

    def test_incident_preserves_kpi_impact_metadata(self):
        changed_models = [
            {
                "model_name": "stg_orders",
                "sql": "select 1",
                "project_context": {
                    "model_names": ["stg_orders"],
                    "column_names": ["gross_revenue"],
                    "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                },
            }
        ]

        with _patched_detectors():
            incident = analyze_changed_models(changed_models)

        self.assertEqual(
            incident.metadata["kpi_impact"]["impacted_kpis"],
            ["Revenue / GMV"],
        )
        self.assertEqual(
            incident.metadata["kpi_impact"]["impact_paths"],
            [["stg_orders", "fct_orders", "Revenue / GMV"]],
        )
        self.assertEqual(
            incident.metadata["kpi_impact"]["changed_models"],
            ["stg_orders"],
        )

    def test_existing_detector_ordering_is_unchanged_before_kpi_impact(self):
        changed_models = [
            {
                "model_name": "stg_orders",
                "sql": "select 1",
                "project_context": {
                    "model_names": ["stg_orders"],
                    "column_names": ["gross_revenue"],
                    "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                },
            }
        ]

        with _patched_detectors():
            incident = analyze_changed_models(changed_models)

        self.assertEqual(
            [signal.component for signal in incident.signals],
            [
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
                "kpi_impact",
            ],
        )

    def test_project_context_inputs_are_never_mutated(self):
        changed_models = [
            {
                "model_name": "stg_orders",
                "sql": "select 1",
                "project_context": {
                    "model_names": ["stg_orders"],
                    "column_names": ["gross_revenue"],
                    "models": [{"name": "stg_orders"}],
                    "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                },
            }
        ]
        original = copy.deepcopy(changed_models)

        with _patched_detectors():
            analyze_changed_models(changed_models)

        self.assertEqual(changed_models, original)


class _patched_detectors:
    def __enter__(self):
        from unittest.mock import patch

        self.calls = {
            "ast": [],
            "metadata": [],
            "drift": [],
            "blast": [],
            "history": [],
            "business_metrics": [],
            "kpi_discovery": [],
            "semantic_graph": [],
            "semantic_kpi_inference": [],
            "kpi_impact_signal": [],
        }
        self.patchers = [
            patch("agent.pr_analysis.run_ast_analysis", side_effect=self._run_ast),
            patch("agent.pr_analysis.ast_to_signal", side_effect=self._ast_signal),
            patch("agent.pr_analysis.run_metadata_checks", side_effect=self._run_metadata),
            patch("agent.pr_analysis.metadata_checks_to_signal", side_effect=self._metadata_signal),
            patch("agent.pr_analysis.compare_last_run", side_effect=self._compare_last_run),
            patch("agent.pr_analysis.metadata_drift_to_signal", side_effect=self._drift_signal),
            patch("agent.pr_analysis.calculate_blast_radius", side_effect=self._calculate_blast),
            patch("agent.pr_analysis.blast_radius_to_signal", side_effect=self._blast_signal),
            patch("agent.pr_analysis.evaluate_history", side_effect=self._evaluate_history),
            patch("agent.pr_analysis.historical_reliability_to_signal", side_effect=self._history_signal),
            patch("agent.pr_analysis.calculate_operational_metrics", side_effect=self._calculate_business_metrics),
            patch("agent.pr_analysis.evaluate_metric_reliability", side_effect=self._evaluate_metric_reliability),
            patch("agent.pr_analysis.business_metrics_to_signal", side_effect=self._business_metric_signal),
            patch("agent.pr_analysis.discover_kpis", side_effect=self._discover_kpis),
            patch("agent.pr_analysis.build_semantic_graph", side_effect=self._build_semantic_graph),
            patch("agent.pr_analysis.infer_impacted_kpis", side_effect=self._infer_impacted_kpis),
            patch("agent.pr_analysis.kpi_impact_to_signal", side_effect=self._kpi_impact_signal),
        ]
        for patcher in self.patchers:
            patcher.start()
        return self.calls

    def __exit__(self, exc_type, exc, tb):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _run_ast(self, sql, model_name):
        self.calls["ast"].append((sql, model_name))
        return {"model_name": model_name, "source": "ast"}

    def _ast_signal(self, result):
        return Signal("ast", Severity.LOW, 75, -5, metadata=dict(result))

    def _run_metadata(self, conn, model_name, key_columns):
        self.calls["metadata"].append((conn, model_name, list(key_columns)))
        return {"model_name": model_name}

    def _metadata_signal(self, result):
        return Signal(
            "metadata_checks",
            Severity.MEDIUM,
            85,
            -15,
            metadata=dict(result),
        )

    def _compare_last_run(self, db_path, project_name, model_name):
        self.calls["drift"].append((db_path, project_name, model_name))
        return {"model_name": model_name}

    def _drift_signal(self, result):
        return Signal(
            "metadata_drift",
            Severity.HIGH,
            95,
            -35,
            metadata=dict(result),
        )

    def _calculate_blast(self, project_path, model_name, changed_columns=None):
        self.calls["blast"].append((project_path, model_name, list(changed_columns or [])))
        return {"changed_model": model_name, "total_affected": 0}

    def _blast_signal(self, result):
        return Signal(
            "blast_radius",
            Severity.CRITICAL,
            95,
            -25,
            metadata=dict(result),
        )

    def _evaluate_history(self, history):
        self.calls["history"].append(copy.deepcopy(history))
        return {"severity": "LOW", "confidence": 75, "score": 95, "metadata": dict(history)}

    def _history_signal(self, result):
        return Signal(
            "historical_reliability",
            Severity.LOW,
            75,
            -5,
            metadata=dict(result.get("metadata", {})),
        )

    def _calculate_business_metrics(self, events):
        self.calls["business_metrics"].append(copy.deepcopy(events))
        return {
            "carts_delivered_wrong_staging_area_and_late": 0,
            "mis_sorts": 0,
            "totes_loaded_in_incorrect_order": 0,
            "failed_pickups": len(events),
            "overflow_avalanches": 0,
            "total_events": len(events),
        }

    def _evaluate_metric_reliability(self, metrics, baseline=None):
        return {
            "severity": "HIGH",
            "confidence": 95,
            "score": -35,
            "reasons": ["High severity metric spike detected"],
            "metadata": {
                "metrics": dict(metrics),
                "baseline": dict(baseline or {}),
                "spike_percentages": {"failed_pickups": 100.0},
            },
        }

    def _business_metric_signal(self, result):
        return Signal(
            "business_metrics",
            Severity.HIGH,
            95,
            -35,
            reasons=list(result["reasons"]),
            metadata=dict(result["metadata"]),
        )

    def _discover_kpis(self, project_context):
        self.calls["kpi_discovery"].append(copy.deepcopy(project_context))
        return [sentinel.revenue_kpi]

    def _build_semantic_graph(self, project_context):
        self.calls["semantic_graph"].append(copy.deepcopy(project_context))
        return sentinel.semantic_graph

    def _infer_impacted_kpis(self, *, changed_models, discovered_kpis, semantic_graph=None):
        self.calls["semantic_kpi_inference"].append(
            {
                "changed_models": list(changed_models),
                "discovered_kpis": list(discovered_kpis),
                "semantic_graph": semantic_graph,
            }
        )
        return sentinel.kpi_impact_report

    def _kpi_impact_signal(self, report):
        self.calls["kpi_impact_signal"].append(report)
        return Signal(
            "kpi_impact",
            Severity.HIGH,
            95,
            -30,
            reasons=["Revenue / GMV is impacted through stg_orders → fct_orders → Revenue / GMV"],
            metadata={
                "changed_models": ["stg_orders"],
                "impacted_kpis": ["Revenue / GMV"],
                "unaffected_kpis": [],
                "impact_paths": [["stg_orders", "fct_orders", "Revenue / GMV"]],
            },
        )


if __name__ == "__main__":
    unittest.main()
