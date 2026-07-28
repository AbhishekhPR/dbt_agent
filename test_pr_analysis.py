import copy
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import sentinel

from agent.decision_engine import DeploymentDecision
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_outcomes import DeploymentOutcome
from agent.deployment_snapshot import DeploymentSnapshot
from agent.pr_analysis import analyze_changed_models, analyze_pr_with_history
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

    def test_kpi_impact_signal_is_contextual_in_deployment_decision(self):
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

    def test_outcome_memory_signal_is_added_before_kpi_impact(self):
        outcomes = [
            _outcome("out-1", "deploy-previous", DeploymentDecision.ALLOW, "incident_occurred")
        ]

        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                deployment_id="deploy-current",
                outcomes=outcomes,
            )

        self.assertEqual(
            [signal.component for signal in incident.signals],
            [
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
                "deployment_outcomes",
                "kpi_impact",
                "semantic_contract",
            ],
        )
        outcome_signal = _signal(incident, "deployment_outcomes")
        self.assertEqual(outcome_signal.severity, Severity.MEDIUM)
        self.assertEqual(outcome_signal.score, -10)
        self.assertIn(
            "Previous allowed or warned deployments were followed by incidents",
            outcome_signal.reasons,
        )

    def test_no_outcomes_keeps_existing_signal_ordering(self):
        with _patched_detectors(neutral=True):
            incident = analyze_changed_models(
                [_semantic_model_spec()],
                deployment_id="deploy-current",
                outcomes=[],
            )

        self.assertNotIn("deployment_outcomes", [signal.component for signal in incident.signals])

    def test_outcome_memory_does_not_mutate_input_outcomes(self):
        outcomes = [
            _outcome("out-1", "deploy-previous", DeploymentDecision.ALLOW, "incident_occurred")
        ]
        original = copy.deepcopy(outcomes)

        with _patched_detectors(neutral=True):
            analyze_changed_models(
                [_semantic_model_spec()],
                deployment_id="deploy-current",
                outcomes=outcomes,
            )

        self.assertEqual(outcomes, original)

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

    def test_analyze_pr_with_history_loads_latest_snapshot_from_history_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_previous_snapshot("older", []))
            latest = _previous_snapshot("latest", [])
            store.save_snapshot(latest)

            with _patched_detectors(neutral=True):
                incident = analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

        self.assertEqual(incident.metadata["previous_snapshot_id"], "latest")
        self.assertTrue(incident.metadata["previous_snapshot_loaded"])
        self.assertTrue(incident.metadata["history_enabled"])

    def test_analyze_pr_with_history_adds_semantic_diff_signal_when_latest_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_previous_snapshot("latest", []))

            with _patched_detectors(neutral=True):
                incident = analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

        self.assertEqual(incident.signals[-1].component, "semantic_diff")

    def test_analyze_pr_with_history_adds_outcome_memory_signal(self):
        outcomes = [
            _outcome("out-1", "deploy-previous", DeploymentDecision.WARN, "reverted")
        ]

        with _patched_detectors(neutral=True):
            incident = analyze_pr_with_history(
                changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                project_context=_project_context(),
                deployment_id="deploy-current",
                outcomes=outcomes,
            )

        outcome_signal = _signal(incident, "deployment_outcomes")
        self.assertEqual(outcome_signal.severity, Severity.MEDIUM)
        self.assertEqual(outcome_signal.metadata["total_outcomes"], 1)

    def test_analyze_pr_with_history_empty_store_keeps_behavior_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")

            with _patched_detectors(neutral=True):
                incident = analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

        self.assertNotIn("semantic_diff", [signal.component for signal in incident.signals])
        self.assertTrue(incident.metadata["history_enabled"])
        self.assertFalse(incident.metadata["previous_snapshot_loaded"])
        self.assertNotIn("previous_snapshot_id", incident.metadata)

    def test_analyze_pr_with_history_without_store_keeps_behavior_unchanged(self):
        with _patched_detectors(neutral=True):
            incident = analyze_pr_with_history(
                changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                project_context=_project_context(),
                deployment_id="deploy-current",
            )

        self.assertNotIn("semantic_diff", [signal.component for signal in incident.signals])
        self.assertFalse(incident.metadata["history_enabled"])
        self.assertFalse(incident.metadata["previous_snapshot_loaded"])

    def test_analyze_pr_with_history_sets_previous_snapshot_loaded_true_when_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_previous_snapshot("latest", []))

            with _patched_detectors(neutral=True):
                incident = analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

        self.assertTrue(incident.metadata["previous_snapshot_loaded"])
        self.assertEqual(incident.metadata["previous_snapshot_id"], "latest")

    def test_analyze_pr_with_history_sets_previous_snapshot_loaded_false_when_not_loaded(self):
        with _patched_detectors(neutral=True):
            incident = analyze_pr_with_history(
                changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                project_context=_project_context(),
                deployment_id="deploy-current",
            )

        self.assertFalse(incident.metadata["previous_snapshot_loaded"])

    def test_analyze_pr_with_history_does_not_save_current_snapshot_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_previous_snapshot("latest", []))

            with _patched_detectors(neutral=True):
                analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

            snapshots = store.list_snapshots()

        self.assertEqual([snapshot["snapshot_id"] for snapshot in snapshots], ["latest"])

    def test_analyze_pr_with_history_does_not_mutate_project_context(self):
        project_context = _project_context()
        original = copy.deepcopy(project_context)

        with _patched_detectors(neutral=True):
            analyze_pr_with_history(
                changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                project_context=project_context,
                deployment_id="deploy-current",
            )

        self.assertEqual(project_context, original)

    def test_analyze_pr_with_history_does_not_mutate_loaded_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            previous = _previous_snapshot("latest", [_semantic_contract(invariants=["never negative"])])
            store.save_snapshot(previous)
            original = copy.deepcopy(store.load_latest_snapshot())

            with _patched_detectors(neutral=True):
                analyze_pr_with_history(
                    changed_models=[{"model_name": "stg_orders", "sql": "select 1"}],
                    project_context=_project_context(),
                    history_store=store,
                    deployment_id="deploy-current",
                )

            loaded_after = store.load_snapshot("latest")

        self.assertEqual(loaded_after, original)

    def test_unrelated_derived_changed_column_reduces_kpi_impact(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract(related_models=["fct_revenue"], related_columns=["net_revenue"])],
            column_lineage_graph=_column_graph(
                "fct_revenue",
                ["net_revenue"],
                {"net_revenue": ["order_total"]},
            ),
        )

        with _patched_non_semantic_detectors():
            incident = analyze_changed_models(
                [
                    _lineage_model_spec(
                        "select order_total as net_revenue, true as debug_flag from stg_orders"
                    )
                ],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        kpi_signal = _signal(incident, "kpi_impact")

        self.assertEqual(kpi_signal.severity, Severity.LOW)
        self.assertEqual(kpi_signal.score, 0)
        self.assertEqual(
            kpi_signal.metadata["changed_columns_by_model"],
            {"fct_revenue": ["debug_flag"]},
        )
        self.assertIn(
            "Revenue / GMV does not read fct_revenue.debug_flag",
            kpi_signal.metadata["column_level_evidence"],
        )

    def test_kpi_relevant_derived_changed_column_preserves_kpi_impact(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract(related_models=["fct_revenue"], related_columns=["net_revenue"])],
            column_lineage_graph=_column_graph(
                "fct_revenue",
                ["net_revenue"],
                {"net_revenue": ["stg_orders.order_total"]},
            ),
        )

        with _patched_non_semantic_detectors():
            incident = analyze_changed_models(
                [
                    _lineage_model_spec(
                        (
                            "select order_total - refund_amount as net_revenue "
                            "from stg_orders"
                        ),
                        output_columns=["net_revenue"],
                    )
                ],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        kpi_signal = _signal(incident, "kpi_impact")

        self.assertEqual(kpi_signal.severity, Severity.LOW)
        self.assertEqual(kpi_signal.score, 0)
        self.assertEqual(kpi_signal.metadata["context_severity"], "HIGH")
        self.assertGreaterEqual(kpi_signal.confidence, 90)
        self.assertEqual(
            kpi_signal.metadata["changed_columns_by_model"],
            {"fct_revenue": ["net_revenue"]},
        )
        self.assertIn(
            "Revenue / GMV reads fct_revenue.net_revenue",
            kpi_signal.metadata["column_level_evidence"],
        )

    def test_no_previous_snapshot_falls_back_to_model_level_kpi_impact(self):
        with _patched_non_semantic_detectors():
            incident = analyze_changed_models(
                [_lineage_model_spec("select net_revenue, true as debug_flag from stg_orders")]
            )

        kpi_signal = _signal(incident, "kpi_impact")

        self.assertEqual(kpi_signal.severity, Severity.LOW)
        self.assertEqual(kpi_signal.score, 0)
        self.assertEqual(kpi_signal.metadata["context_severity"], "HIGH")
        self.assertEqual(kpi_signal.metadata["fallback_reason"], "changed columns unavailable")

    def test_old_snapshot_without_column_lineage_keeps_model_level_behavior(self):
        previous_snapshot = _previous_snapshot(
            "previous",
            [_semantic_contract(related_models=["fct_revenue"], related_columns=["net_revenue"])],
        )

        with _patched_non_semantic_detectors():
            incident = analyze_changed_models(
                [_lineage_model_spec("select net_revenue, true as debug_flag from stg_orders")],
                previous_snapshot=previous_snapshot,
                deployment_id="deploy-current",
            )

        kpi_signal = _signal(incident, "kpi_impact")

        self.assertEqual(kpi_signal.severity, Severity.LOW)
        self.assertEqual(kpi_signal.score, 0)
        self.assertEqual(kpi_signal.metadata["context_severity"], "HIGH")
        self.assertEqual(kpi_signal.metadata["fallback_reason"], "changed columns unavailable")

    def test_pr_analysis_preserves_not_evaluated_assumption_verification(self):
        with _patched_non_semantic_detectors():
            incident = analyze_changed_models(
                [_lineage_model_spec("select net_revenue from stg_orders")]
            )

        report = incident.metadata["assumption_verification"]

        self.assertTrue(report["checks"])
        self.assertTrue(all(not check["evaluated"] for check in report["checks"]))
        self.assertNotIn(
            "assumption_verification",
            [signal.component for signal in incident.signals],
        )

    def test_pr_analysis_evaluates_assumption_verification_when_connection_exists(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fct_revenue (net_revenue INTEGER, order_id TEXT)")
        conn.executemany(
            "INSERT INTO fct_revenue (net_revenue, order_id) VALUES (?, ?)",
            [(10, "o1"), (-2, None)],
        )

        model_spec = _lineage_model_spec(
            "select net_revenue, order_id from stg_orders",
            output_columns=["net_revenue", "order_id"],
        )
        model_spec["conn"] = conn

        try:
            with _patched_non_semantic_detectors():
                incident = analyze_changed_models([model_spec])
        finally:
            conn.close()

        checks = incident.metadata["assumption_verification"]["checks"]
        non_negative = next(
            check for check in checks
            if check["check_type"] == "non_negative" and check["column_name"] == "net_revenue"
        )
        not_null = next(
            check for check in checks
            if check["check_type"] == "not_null" and check["column_name"] == "order_id"
        )

        self.assertTrue(non_negative["evaluated"])
        self.assertEqual(non_negative["status"], "failed")
        self.assertEqual(non_negative["violation_count"], 1)
        self.assertEqual(not_null["status"], "failed")
        self.assertEqual(not_null["violation_count"], 1)

        assumption_signal = _signal(incident, "assumption_verification")

        self.assertEqual(assumption_signal.severity, Severity.HIGH)
        self.assertEqual(assumption_signal.score, -30)
        self.assertIn(
            "Revenue / GMV assumption failed: fct_revenue.net_revenue never negative (1 violation)",
            assumption_signal.reasons,
        )
        self.assertEqual(assumption_signal.metadata["failed_count"], 2)

    def test_pr_analysis_adds_low_signal_when_assumption_checks_pass(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fct_revenue (net_revenue INTEGER, order_id TEXT)")
        conn.execute(
            "INSERT INTO fct_revenue (net_revenue, order_id) VALUES (?, ?)",
            (12, "o1"),
        )

        model_spec = _lineage_model_spec(
            "select net_revenue, order_id from stg_orders",
            output_columns=["net_revenue", "order_id"],
        )
        model_spec["conn"] = conn

        try:
            with _patched_non_semantic_detectors():
                incident = analyze_changed_models([model_spec])
        finally:
            conn.close()

        assumption_signal = _signal(incident, "assumption_verification")

        self.assertEqual(assumption_signal.severity, Severity.LOW)
        self.assertEqual(assumption_signal.score, 0)
        self.assertEqual(assumption_signal.reasons, ["All evaluated assumption checks passed"])

    def test_missing_sql_skips_ast_and_records_unavailable_metadata(self):
        model_spec = {
            "model_name": "fct_revenue",
            "name": "fct_revenue",
            "unique_id": "model.analytics.fct_revenue",
            "path": "models/marts/fct_revenue.sql",
            "sql": None,
            "sql_available": False,
            "sql_source": "unavailable",
        }

        with _patched_detectors() as calls:
            incident = analyze_changed_models([model_spec])

        self.assertEqual(calls["ast"], [])
        self.assertNotIn("ast", [signal.component for signal in incident.signals])
        self.assertEqual(
            incident.metadata["sql_sources"],
            [
                {
                    "unique_id": "model.analytics.fct_revenue",
                    "name": "fct_revenue",
                    "original_file_path": None,
                    "path": "models/marts/fct_revenue.sql",
                    "sql_available": False,
                    "sql_source": "unavailable",
                    "ast_status": "skipped",
                }
            ],
        )

    def test_available_sql_records_evaluated_ast_metadata(self):
        model_spec = {
            "model_name": "fct_revenue",
            "name": "fct_revenue",
            "unique_id": "model.analytics.fct_revenue",
            "sql": "select customer_id from raw_orders",
            "sql_available": True,
            "sql_source": "compiled_code",
        }

        with _patched_detectors() as calls:
            incident = analyze_changed_models([model_spec])

        self.assertEqual(
            calls["ast"],
            [("select customer_id from raw_orders", "fct_revenue")],
        )
        self.assertEqual(
            incident.metadata["sql_sources"][0]["ast_status"],
            "evaluated",
        )
        self.assertEqual(
            incident.metadata["sql_sources"][0]["sql_source"],
            "compiled_code",
        )


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


class _patched_non_semantic_detectors:
    def __enter__(self):
        from unittest.mock import patch

        self.stack = ExitStack()
        self.stack.enter_context(
            patch("agent.pr_analysis.run_ast_analysis", return_value={"model_name": "fct_revenue"})
        )
        self.stack.enter_context(
            patch(
                "agent.pr_analysis.ast_to_signal",
                return_value=Signal("ast", Severity.LOW, 90, 0, metadata={}),
            )
        )
        self.stack.enter_context(
            patch("agent.pr_analysis.run_metadata_checks", return_value={"model_name": "fct_revenue"})
        )
        self.stack.enter_context(
            patch(
                "agent.pr_analysis.metadata_checks_to_signal",
                return_value=Signal("metadata_checks", Severity.LOW, 90, 0, metadata={}),
            )
        )
        self.stack.enter_context(
            patch("agent.pr_analysis.compare_last_run", return_value={"model_name": "fct_revenue"})
        )
        self.stack.enter_context(
            patch(
                "agent.pr_analysis.metadata_drift_to_signal",
                return_value=Signal("metadata_drift", Severity.LOW, 90, 0, metadata={}),
            )
        )
        self.stack.enter_context(
            patch("agent.pr_analysis.calculate_blast_radius", return_value={"changed_model": "fct_revenue"})
        )
        self.stack.enter_context(
            patch(
                "agent.pr_analysis.blast_radius_to_signal",
                return_value=Signal("blast_radius", Severity.LOW, 90, 0, metadata={}),
            )
        )
        self.stack.enter_context(
            patch("agent.pr_analysis.evaluate_history", return_value={"metadata": {}})
        )
        self.stack.enter_context(
            patch(
                "agent.pr_analysis.historical_reliability_to_signal",
                return_value=Signal("historical_reliability", Severity.LOW, 90, 0, metadata={}),
            )
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stack.close()


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


def _project_context():
    return {
        "model_names": ["stg_orders"],
        "column_names": ["gross_revenue"],
        "models": [{"name": "stg_orders"}],
        "metrics": [{"name": "Revenue / GMV", "model": "fct_orders"}],
    }


def _lineage_model_spec(sql, *, output_columns=None):
    columns = list(output_columns or ["debug_flag", "net_revenue"])
    return {
        "model_name": "fct_revenue",
        "sql": sql,
        "project_context": {
            "model_names": ["fct_revenue"],
            "column_names": ["debug_flag", "net_revenue", "revenue"],
            "models": [
                {
                    "name": "fct_revenue",
                    "columns": columns,
                    "sql": sql,
                }
            ],
            "metrics": [{"name": "Revenue", "model": "fct_revenue"}],
        },
    }


def _signal(incident, component):
    for signal in incident.signals:
        if signal.component == component:
            return signal
    raise AssertionError(f"{component!r} signal not found")


def _previous_snapshot(snapshot_id, contracts, *, column_lineage_graph=None):
    return DeploymentSnapshot(
        snapshot_id=snapshot_id,
        deployment_id=f"deploy-{snapshot_id}",
        created_at="2026-07-02T00:00:00+00:00",
        changed_models=["stg_orders"],
        semantic_context={
            "discovered_kpis": [{"name": contract["kpi_name"]} for contract in contracts],
            "knowledge_report": {"contracts": copy.deepcopy(contracts)},
            "metadata": {"kpi_count": len(contracts)},
            **(
                {"column_lineage_graph": copy.deepcopy(column_lineage_graph)}
                if column_lineage_graph is not None
                else {}
            ),
        },
        decision=None,
        incident_summary=None,
        metadata={"source": "unit-test"},
    )


def _outcome(outcome_id, deployment_id, decision, outcome):
    return DeploymentOutcome(
        outcome_id=outcome_id,
        deployment_id=deployment_id,
        decision=decision,
        outcome=outcome,
        created_at="2026-07-02T00:00:00+00:00",
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


def _column_graph(model_name, output_columns, dependencies):
    edges = []
    for to_column, upstream_columns in dependencies.items():
        for upstream in upstream_columns:
            if "." in upstream:
                from_model, from_column = upstream.rsplit(".", 1)
            else:
                from_model, from_column = None, upstream
            edges.append(
                {
                    "from_model": from_model,
                    "from_column": from_column,
                    "to_model": model_name,
                    "to_column": to_column,
                    "confidence": 0.95 if from_model else 0.7,
                    "reason": "unit-test",
                }
            )
    return {
        "models": {
            model_name: {
                "model_name": model_name,
                "output_columns": list(output_columns),
                "edges": edges,
                "unknown_columns": [],
                "metadata": {},
            }
        },
        "metadata": {"source": "unit-test"},
    }


if __name__ == "__main__":
    unittest.main()
