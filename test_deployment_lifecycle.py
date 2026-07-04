import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.decision_engine import DeploymentDecision
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_lifecycle import DeploymentReviewResult, review_deployment
from agent.incident import Incident
from agent.signals import Severity


class DeploymentLifecycleTests(unittest.TestCase):
    def test_review_deployment_returns_incident(self):
        incident = _incident(DeploymentDecision.ALLOW)

        with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
            result = review_deployment(
                changed_models=[_model()],
                project_context=_project_context(),
            )

        self.assertIsInstance(result, DeploymentReviewResult)
        self.assertEqual(result.incident, incident)

    def test_review_deployment_returns_current_snapshot(self):
        current_snapshot = _snapshot("snap-current", "deploy-current")
        incident = _incident(DeploymentDecision.ALLOW, current_snapshot=current_snapshot)

        with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
            result = review_deployment(
                changed_models=[_model()],
                project_context=_project_context(),
            )

        self.assertEqual(result.current_snapshot, current_snapshot)

    def test_previous_snapshot_loaded_is_true_when_history_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            previous_snapshot = _snapshot("snap-previous", "deploy-previous")
            store.save_snapshot(previous_snapshot)
            incident = _incident(
                DeploymentDecision.ALLOW,
                current_snapshot=_snapshot("snap-current", "deploy-current"),
                previous_snapshot_loaded=True,
                previous_snapshot_id="snap-previous",
            )

            with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
                result = review_deployment(
                    changed_models=[_model()],
                    project_context=_project_context(),
                    history_store=store,
                )

        self.assertTrue(result.previous_snapshot_loaded)
        self.assertEqual(result.previous_snapshot, previous_snapshot)

    def test_previous_snapshot_loaded_is_false_when_history_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            incident = _incident(DeploymentDecision.ALLOW)

            with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
                result = review_deployment(
                    changed_models=[_model()],
                    project_context=_project_context(),
                    history_store=store,
                )

        self.assertFalse(result.previous_snapshot_loaded)
        self.assertIsNone(result.previous_snapshot)

    def test_auto_record_false_does_not_save_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeploymentHistoryStore(Path(tmp) / "history.json")
            store.save_snapshot(_snapshot("snap-previous", "deploy-previous"))
            incident = _incident(
                DeploymentDecision.ALLOW,
                current_snapshot=_snapshot("snap-current", "deploy-current"),
                previous_snapshot_loaded=True,
                previous_snapshot_id="snap-previous",
            )

            with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
                result = review_deployment(
                    changed_models=[_model()],
                    project_context=_project_context(),
                    history_store=store,
                    auto_record=False,
                )

            snapshots = store.list_snapshots()

        self.assertIsNone(result.saved_snapshot_id)
        self.assertEqual([snapshot["snapshot_id"] for snapshot in snapshots], ["snap-previous"])

    def test_auto_record_true_saves_allow_deployment_snapshot(self):
        result, latest_snapshot, _ = _review_with_recorded_decision(DeploymentDecision.ALLOW)

        self.assertEqual(result.saved_snapshot_id, "snap-current")
        self.assertEqual(latest_snapshot["snapshot_id"], "snap-current")

    def test_auto_record_true_saves_warn_deployment_snapshot(self):
        result, latest_snapshot, _ = _review_with_recorded_decision(DeploymentDecision.WARN)

        self.assertEqual(result.saved_snapshot_id, "snap-current")
        self.assertEqual(latest_snapshot["snapshot_id"], "snap-current")

    def test_auto_record_true_does_not_save_block_by_default(self):
        result, _, snapshot_ids = _review_with_recorded_decision(DeploymentDecision.BLOCK)

        self.assertIsNone(result.saved_snapshot_id)
        self.assertEqual(snapshot_ids, ["snap-previous"])

    def test_allow_blocked_recording_true_saves_block_snapshot(self):
        result, latest_snapshot, _ = _review_with_recorded_decision(
            DeploymentDecision.BLOCK,
            allow_blocked_recording=True,
        )

        self.assertEqual(result.saved_snapshot_id, "snap-current")
        self.assertEqual(latest_snapshot["snapshot_id"], "snap-current")

    def test_project_context_is_not_mutated(self):
        project_context = _project_context()
        original = copy.deepcopy(project_context)

        def mutate_received_context(**kwargs):
            kwargs["project_context"]["model_names"].append("mutated")
            return _incident(DeploymentDecision.ALLOW)

        with patch("agent.deployment_lifecycle.analyze_pr_with_history", side_effect=mutate_received_context):
            review_deployment(
                changed_models=[_model()],
                project_context=project_context,
            )

        self.assertEqual(project_context, original)

    def test_result_serializes_to_dict_and_json(self):
        current_snapshot = _snapshot("snap-current", "deploy-current")
        incident = _incident(DeploymentDecision.ALLOW, current_snapshot=current_snapshot)

        with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
            result = review_deployment(
                changed_models=[_model()],
                project_context=_project_context(),
                metadata={"source": "unit-test"},
            )

        payload = result.to_dict()
        serialized = json.dumps(payload)

        self.assertIsInstance(serialized, str)
        self.assertEqual(payload["current_snapshot"]["snapshot_id"], "snap-current")
        self.assertEqual(payload["metadata"]["request_metadata"], {"source": "unit-test"})

    def test_initialize_production_baseline_saves_loadable_snapshot(self):
        from agent.baseline import initialize_production_baseline

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_baseline_manifest(tmp)
            history_path = Path(tmp) / "history.json"

            result = initialize_production_baseline(
                dbt_manifest_path=str(manifest_path),
                history_path=str(history_path),
                deployment_id="production-baseline",
            )
            loaded_snapshot = DeploymentHistoryStore(history_path).load_latest_snapshot()

        self.assertEqual(loaded_snapshot["snapshot_id"], result.snapshot_id)
        self.assertEqual(loaded_snapshot["deployment_id"], "production-baseline")
        self.assertEqual(
            loaded_snapshot["changed_models"],
            ["fct_orders", "stg_orders"],
        )
        self.assertIn("semantic_context", loaded_snapshot)
        self.assertEqual(
            loaded_snapshot["semantic_context"]["metadata"]["changed_models"],
            ["fct_orders", "stg_orders"],
        )
        self.assertEqual(result.model_count, 2)
        self.assertEqual(result.kpi_count, 1)

    def test_initialize_production_baseline_rejects_manifest_with_no_models(self):
        from agent.baseline import initialize_production_baseline

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            history_path = Path(tmp) / "history.json"
            manifest_path.write_text(
                json.dumps({"metadata": {"project_name": "empty"}, "nodes": {}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "No dbt models found"):
                initialize_production_baseline(
                    dbt_manifest_path=str(manifest_path),
                    history_path=str(history_path),
                )

            self.assertFalse(history_path.exists())


def _review_with_recorded_decision(decision, *, allow_blocked_recording=False):
    with tempfile.TemporaryDirectory() as tmp:
        store = DeploymentHistoryStore(Path(tmp) / "history.json")
        store.save_snapshot(_snapshot("snap-previous", "deploy-previous"))
        incident = _incident(
            decision,
            current_snapshot=_snapshot("snap-current", "deploy-current"),
            previous_snapshot_loaded=True,
            previous_snapshot_id="snap-previous",
        )

        with patch("agent.deployment_lifecycle.analyze_pr_with_history", return_value=incident):
            result = review_deployment(
                changed_models=[_model()],
                project_context=_project_context(),
                history_store=store,
                auto_record=True,
                allow_blocked_recording=allow_blocked_recording,
            )
        latest_snapshot = store.load_latest_snapshot()
        snapshot_ids = [snapshot["snapshot_id"] for snapshot in store.list_snapshots()]
    return result, latest_snapshot, snapshot_ids


def _incident(
    decision,
    *,
    current_snapshot=None,
    previous_snapshot_loaded=False,
    previous_snapshot_id=None,
):
    metadata = {
        "history_enabled": previous_snapshot_loaded,
        "previous_snapshot_loaded": previous_snapshot_loaded,
    }
    if current_snapshot is not None:
        metadata["current_snapshot"] = copy.deepcopy(current_snapshot)
    if previous_snapshot_id is not None:
        metadata["previous_snapshot_id"] = previous_snapshot_id
    return Incident(
        incident_id="INC-LIFE",
        health=100 if decision == DeploymentDecision.ALLOW else 80,
        decision=decision,
        severity=Severity.LOW,
        confidence=90,
        root_cause="",
        recommendation="",
        metadata=metadata,
    )


def _snapshot(snapshot_id, deployment_id):
    return {
        "snapshot_id": snapshot_id,
        "deployment_id": deployment_id,
        "created_at": "2026-07-02T00:00:00+00:00",
        "changed_models": ["stg_orders"],
        "semantic_context": {
            "discovered_kpis": [{"name": "Revenue"}],
            "knowledge_report": {"contracts": [{"kpi_name": "Revenue"}]},
        },
        "decision": {"decision": "ALLOW", "health": 100},
        "incident_summary": {"incident_id": "INC-LIFE", "decision": "ALLOW"},
        "metadata": {"source": "unit-test"},
    }


def _model():
    return {"model_name": "stg_orders", "sql": "select 1"}


def _project_context():
    return {
        "model_names": ["stg_orders"],
        "column_names": ["gross_revenue"],
        "metrics": [{"name": "Revenue", "model": "fct_orders"}],
    }


def _write_baseline_manifest(tmp):
    path = Path(tmp) / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "jaffle_shop",
                    "dbt_version": "1.8.0",
                },
                "nodes": {
                    "model.jaffle_shop.stg_orders": {
                        "resource_type": "model",
                        "name": "stg_orders",
                        "unique_id": "model.jaffle_shop.stg_orders",
                        "original_file_path": "models/staging/stg_orders.sql",
                        "columns": {
                            "order_id": {"name": "order_id"},
                            "payment_amount": {"name": "payment_amount"},
                        },
                    },
                    "model.jaffle_shop.fct_orders": {
                        "resource_type": "model",
                        "name": "fct_orders",
                        "unique_id": "model.jaffle_shop.fct_orders",
                        "original_file_path": "models/marts/fct_orders.sql",
                        "columns": {
                            "order_id": {"name": "order_id"},
                            "gross_revenue": {"name": "gross_revenue"},
                        },
                        "depends_on": {
                            "nodes": ["model.jaffle_shop.stg_orders"],
                        },
                    },
                },
                "metrics": {
                    "metric.jaffle_shop.revenue": {
                        "name": "Revenue / GMV",
                        "label": "Revenue",
                        "type": "simple",
                        "model": "ref('fct_orders')",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
