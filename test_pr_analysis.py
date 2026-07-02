import copy
import unittest
from types import SimpleNamespace
from unittest.mock import sentinel

from agent.decision_engine import DeploymentDecision
from agent.deployment_snapshot import DeploymentSnapshot
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

        self.assertEqual(len(calls["semantic_context"]), 1)
        self.assertEqual(len(calls["kpi_impact_signal"]), 1)
        self.assertEqual(incident.signals[-2].component, "kpi_impact")
        self.assertEqual(incident.health, 0)
        self.assertEqual(
            incident.signals[-2].metadata["impacted_kpis"],
            ["Revenue / GMV"],
        )

    def test_pr_analysis_builds_semantic_context(self):
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

        with _patched_detectors() as calls:
            analyze_changed_models(changed_models)

        self.assertEqual(
            calls["semantic_context"],
            [
                {
                    "project_context": {
                        "model_names": ["stg_orders"],
                        "column_names": ["gross_revenue"],
                        "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
                    },
                    "changed_models": ["stg_orders"],
                    "metadata": {
                        "model_count": 1,
                        "models": ["stg_orders"],
                    },
                }
            ],
        )

    def test_semantic_contract_signal_participates_in_decision(self):
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

        self.assertEqual(incident.signals[-1].component, "semantic_contract")
        self.assertEqual(incident.signals[-1].metadata["contract_names"], ["Revenue / GMV"])
        self.assertEqual(incident.health, 0)

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
        self.assertIn("semantic_context", incident.metadata)
        self.assertEqual(
            incident.metadata["semantic_context"]["kpi_impact_report"]["impacted_kpis"][0]["name"],
            "Revenue / GMV",
        )
        self.assertEqual(
            incident.metadata["impacted_kpis"],
            ["Revenue / GMV"],
        )
        self.assertEqual(
            incident.metadata["impact_paths"],
            [["stg_orders", "fct_orders", "Revenue / GMV"]],
        )
        self.assertEqual(
            incident.metadata["contract_validation"]["contract_names"],
            ["Revenue / GMV"],
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
                "semantic_contract",
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

    def test_pr_analysis_accepts_previous_snapshot(self):
        previous_snapshot = _previous_snapshot("previous", [])

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(incident.metadata["previous_snapshot_id"], "previous")
        self.assertEqual(
            incident.metadata["current_snapshot"]["deployment_id"],
            "deploy-current",
        )

    def test_semantic_diff_signal_is_added_when_previous_snapshot_is_provided(self):
        previous_snapshot = _previous_snapshot("previous", [])

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(incident.signals[-1].component, "semantic_diff")
        self.assertEqual(incident.signals[-1].metadata["added_kpis"], ["Revenue / GMV"])

    def test_semantic_diff_signal_affects_final_health_and_decision(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract(related_columns=["gross_revenue"])],
        )

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(incident.signals[-1].component, "semantic_diff")
        self.assertEqual(incident.signals[-1].score, -20)
        self.assertEqual(incident.health, 80)
        self.assertEqual(incident.decision, DeploymentDecision.WARN)

    def test_low_semantic_diff_does_not_reduce_health(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract()],
        )

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(incident.signals[-1].component, "semantic_diff")
        self.assertEqual(incident.signals[-1].severity, Severity.LOW)
        self.assertEqual(incident.signals[-1].score, 0)
        self.assertEqual(incident.health, 100)
        self.assertEqual(incident.decision, DeploymentDecision.ALLOW)

    def test_no_previous_snapshot_keeps_existing_behavior_unchanged(self):
        with _patched_detectors(neutral=True):
            incident = analyze_changed_models([_semantic_model_spec()])

        self.assertNotIn("semantic_diff", [signal.component for signal in incident.signals])
        self.assertNotIn("current_snapshot", incident.metadata)
        self.assertNotIn("semantic_diff", incident.metadata)
        self.assertNotIn("previous_snapshot_id", incident.metadata)

    def test_incident_preserves_semantic_diff_metadata(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [
                _semantic_contract(
                    related_columns=["gross_revenue"],
                    invariants=["never negative"],
                )
            ],
        )

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        semantic_diff = incident.metadata["semantic_diff"]
        self.assertEqual(semantic_diff["previous_snapshot_id"], "previous")
        self.assertEqual(incident.metadata["changed_kpis"], ["Revenue / GMV"])
        self.assertEqual(
            incident.metadata["dependency_changes"],
            semantic_diff["dependency_changes"],
        )
        self.assertEqual(
            incident.metadata["contract_changes"],
            semantic_diff["contract_changes"],
        )
        self.assertIn("Revenue / GMV", incident.metadata["contract_changes"])

    def test_current_snapshot_is_stored_in_incident_metadata(self):
        previous_snapshot = _previous_snapshot("previous", [])

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        current_snapshot = incident.metadata["current_snapshot"]
        self.assertEqual(current_snapshot["deployment_id"], "deploy-current")
        self.assertEqual(current_snapshot["changed_models"], ["stg_orders"])
        self.assertEqual(
            incident.metadata["current_snapshot_id"],
            current_snapshot["snapshot_id"],
        )

    def test_signal_ordering_places_semantic_diff_after_semantic_contract(self):
        previous_snapshot = _previous_snapshot("previous", [])

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(
            [signal.component for signal in incident.signals],
            [
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
                "kpi_impact",
                "semantic_contract",
                "semantic_diff",
            ],
        )

    def test_previous_snapshot_input_is_not_mutated(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract(invariants=["never negative"])],
        )
        original = copy.deepcopy(previous_snapshot)

        with _patched_detectors(neutral=True):
            analyze_changed_models(
                [_semantic_model_spec()],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(previous_snapshot, original)

    def test_project_context_input_is_not_mutated_with_previous_snapshot(self):
        changed_models = [_semantic_model_spec()]
        previous_snapshot = _previous_snapshot("previous", [])
        original = copy.deepcopy(changed_models)

        with _patched_detectors(neutral=True):
            analyze_changed_models(
                changed_models,
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        self.assertEqual(changed_models, original)


class _patched_detectors:
    def __init__(self, *, neutral=False):
        self.neutral = neutral

    def __enter__(self):
        from unittest.mock import patch

        self.calls = {
            "ast": [],
            "metadata": [],
            "drift": [],
            "blast": [],
            "history": [],
            "business_metrics": [],
            "semantic_context": [],
            "kpi_impact_signal": [],
            "semantic_contract_signal": [],
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
            patch("agent.pr_analysis.build_semantic_context", side_effect=self._build_semantic_context),
            patch("agent.pr_analysis.kpi_impact_to_signal", side_effect=self._kpi_impact_signal),
            patch("agent.pr_analysis.semantic_contract_to_signal", side_effect=self._semantic_contract_signal),
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
        if self.neutral:
            return Signal("ast", Severity.LOW, 90, 0, metadata=dict(result))
        return Signal("ast", Severity.LOW, 75, -5, metadata=dict(result))

    def _run_metadata(self, conn, model_name, key_columns):
        self.calls["metadata"].append((conn, model_name, list(key_columns)))
        return {"model_name": model_name}

    def _metadata_signal(self, result):
        if self.neutral:
            return Signal(
                "metadata_checks",
                Severity.LOW,
                90,
                0,
                metadata=dict(result),
            )
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
        if self.neutral:
            return Signal(
                "metadata_drift",
                Severity.LOW,
                90,
                0,
                metadata=dict(result),
            )
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
        if self.neutral:
            return Signal(
                "blast_radius",
                Severity.LOW,
                90,
                0,
                metadata=dict(result),
            )
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
        if self.neutral:
            return Signal(
                "historical_reliability",
                Severity.LOW,
                90,
                0,
                metadata=dict(result.get("metadata", {})),
            )
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

    def _build_semantic_context(self, *, project_context, changed_models=None, metadata=None):
        self.calls["semantic_context"].append(
            {
                "project_context": copy.deepcopy(project_context),
                "changed_models": list(changed_models),
                "metadata": copy.deepcopy(metadata),
            }
        )
        impacted_kpi = SimpleNamespace(
            name="Revenue / GMV",
            confidence=95,
            impacted_by_models=["stg_orders", "fct_orders"],
            related_columns=["gross_revenue"],
            reasons=["Revenue / GMV is impacted through stg_orders → fct_orders → Revenue / GMV"],
            metadata={"impact_paths": [["stg_orders", "fct_orders", "Revenue / GMV"]]},
        )
        kpi_impact_report = SimpleNamespace(
            changed_models=["stg_orders"],
            impacted_kpis=[impacted_kpi],
            unaffected_kpis=[],
            confidence=95,
            reasons=["Revenue / GMV impacted by stg_orders, fct_orders"],
            metadata={"semantic_graph_provided": True},
        )
        contract_validation_result = {
            "severity": "MEDIUM",
            "confidence": 85,
            "score": -15,
            "reasons": ["Revenue / GMV may be impacted by changed model stg_orders"],
            "metadata": {
                "contract_names": ["Revenue / GMV"],
                "violated_invariants": {},
                "impacted_models": ["stg_orders"],
                "impacted_kpis": ["Revenue / GMV"],
                "impact_paths": [["stg_orders", "fct_orders", "Revenue / GMV"]],
            },
        }
        return SimpleNamespace(
            project_context=copy.deepcopy(project_context),
            discovered_kpis=[sentinel.revenue_kpi],
            semantic_graph=sentinel.semantic_graph,
            kpi_impact_report=kpi_impact_report,
            knowledge_report=sentinel.knowledge_report,
            contract_validation_result=contract_validation_result,
            metadata={"kpi_count": 1, "contract_count": 1},
            to_dict=lambda: {
                "project_context": copy.deepcopy(project_context),
                "discovered_kpis": [{"name": "Revenue / GMV"}],
                "semantic_graph": {"nodes": {}, "edges": []},
                "kpi_impact_report": {
                    "impacted_kpis": [{"name": "Revenue / GMV"}],
                    "confidence": 95,
                },
                "knowledge_report": {"contracts": [{"kpi_name": "Revenue / GMV"}]},
                "contract_validation_result": copy.deepcopy(contract_validation_result),
                "metadata": {"kpi_count": 1, "contract_count": 1},
            },
        )

    def _kpi_impact_signal(self, report):
        self.calls["kpi_impact_signal"].append(report)
        if self.neutral:
            return Signal(
                "kpi_impact",
                Severity.LOW,
                90,
                0,
                reasons=[],
                metadata={
                    "changed_models": ["stg_orders"],
                    "impacted_kpis": ["Revenue / GMV"],
                    "unaffected_kpis": [],
                    "impact_paths": [["stg_orders", "fct_orders", "Revenue / GMV"]],
                },
            )
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

    def _semantic_contract_signal(self, result):
        self.calls["semantic_contract_signal"].append(copy.deepcopy(result))
        if self.neutral:
            return Signal(
                "semantic_contract",
                Severity.LOW,
                90,
                0,
                reasons=[],
                metadata=dict(result["metadata"]),
            )
        return Signal(
            "semantic_contract",
            Severity.MEDIUM,
            85,
            -15,
            reasons=list(result["reasons"]),
            metadata=dict(result["metadata"]),
        )


def _semantic_model_spec():
    return {
        "model_name": "stg_orders",
        "sql": "select 1",
        "project_context": {
            "model_names": ["stg_orders"],
            "column_names": ["gross_revenue"],
            "models": [{"name": "stg_orders"}],
            "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
        },
    }


def _previous_snapshot(snapshot_id, contracts):
    return DeploymentSnapshot(
        snapshot_id=snapshot_id,
        deployment_id=f"deploy-{snapshot_id}",
        created_at="2026-07-02T00:00:00+00:00",
        changed_models=["stg_orders"],
        semantic_context={
            "discovered_kpis": [{"name": contract["kpi_name"]} for contract in contracts],
            "knowledge_report": {"contracts": copy.deepcopy(contracts)},
            "metadata": {"kpi_count": len(contracts)},
        },
        decision=None,
        incident_summary=None,
        metadata={"source": "unit-test"},
    )


def _semantic_contract(
    *,
    kpi_name="Revenue / GMV",
    related_models=None,
    related_columns=None,
    upstream_sources=None,
    downstream_consumers=None,
    assumptions=None,
    invariants=None,
    business_meaning=None,
):
    return {
        "kpi_name": kpi_name,
        "description": f"{kpi_name} contract",
        "business_meaning": business_meaning,
        "related_models": list(related_models or []),
        "related_columns": list(related_columns or []),
        "upstream_sources": list(upstream_sources or []),
        "downstream_consumers": list(downstream_consumers or []),
        "assumptions": list(assumptions or []),
        "invariants": list(invariants or []),
        "confidence": 80,
        "metadata": {},
    }


if __name__ == "__main__":
    unittest.main()
