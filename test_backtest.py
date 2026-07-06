import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.backtest import backtest_deployment
from agent.decision_engine import DeploymentDecision
from agent.deployment_history import DeploymentHistoryStore
from agent.incident import Incident
from agent.signals import Severity, Signal


class BacktestTests(unittest.TestCase):
    def test_backtest_uses_baseline_manifest_as_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_manifest = _write_manifest(tmp, "baseline_manifest.json")
            current_manifest = _write_manifest(tmp, "current_manifest.json")
            captured = {}

            def analyze(**kwargs):
                captured["changed_models"] = list(kwargs["changed_models"])
                captured["project_context"] = dict(kwargs["project_context"])
                captured["previous_snapshot"] = (
                    kwargs["history_store"].load_latest_snapshot()
                )
                return _incident(decision=DeploymentDecision.WARN)

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=analyze,
            ):
                result = backtest_deployment(
                    dbt_manifest_path=str(current_manifest),
                    baseline_manifest_path=str(baseline_manifest),
                    changed_models=["stg_orders"],
                    deployment_id="historical-42",
                )

        self.assertEqual(result.historical_deployment_id, "historical-42")
        self.assertEqual(result.would_have_decision, "WARN")
        self.assertEqual(
            captured["changed_models"],
            [{"model_name": "stg_orders", "sql": "select * from stg_orders"}],
        )
        self.assertEqual(captured["project_context"]["metadata"]["source"], "dbt_manifest")
        self.assertIsNotNone(captured["previous_snapshot"])
        self.assertEqual(
            captured["previous_snapshot"]["metadata"]["source"],
            "backtest_baseline",
        )

    def test_backtest_requires_previous_snapshot_when_no_baseline_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_manifest = _write_manifest(tmp, "current_manifest.json")

            with self.assertRaisesRegex(
                ValueError,
                "Backtest requires a previous production snapshot",
            ):
                backtest_deployment(
                    dbt_manifest_path=str(current_manifest),
                    history_path=str(Path(tmp) / "history.json"),
                    changed_models=["stg_orders"],
                )

    def test_backtest_does_not_record_current_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_manifest = _write_manifest(tmp, "current_manifest.json")
            history_path = Path(tmp) / "history.json"
            store = DeploymentHistoryStore(history_path)
            store.save_snapshot(_snapshot("baseline"))

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                return_value=_incident(decision=DeploymentDecision.ALLOW),
            ):
                result = backtest_deployment(
                    dbt_manifest_path=str(current_manifest),
                    history_path=str(history_path),
                    changed_models=["stg_orders"],
                    deployment_id="historical-42",
                )

            snapshots = DeploymentHistoryStore(history_path).list_snapshots()

        self.assertEqual(result.would_have_decision, "ALLOW")
        self.assertEqual(
            [snapshot["snapshot_id"] for snapshot in snapshots],
            ["baseline"],
        )

    def test_backtest_result_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_manifest = _write_manifest(tmp, "baseline_manifest.json")
            current_manifest = _write_manifest(tmp, "current_manifest.json")

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                return_value=_incident(decision=DeploymentDecision.BLOCK),
            ):
                result = backtest_deployment(
                    dbt_manifest_path=str(current_manifest),
                    baseline_manifest_path=str(baseline_manifest),
                    changed_models=["stg_orders"],
                )

        payload = result.to_dict()

        self.assertEqual(payload["would_have_decision"], "BLOCK")
        self.assertEqual(payload["baseline_source"], "baseline_manifest")
        json.dumps(payload)


def _write_manifest(tmp, filename):
    path = Path(tmp) / filename
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    return path


def _manifest():
    return {
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
                    "gross_revenue": {"name": "gross_revenue"},
                },
            }
        },
        "metrics": {
            "metric.jaffle_shop.revenue": {
                "name": "Revenue",
                "label": "Revenue",
                "type": "simple",
                "model": "ref('stg_orders')",
            }
        },
    }


def _incident(*, decision):
    return Incident(
        incident_id="INC-BACKTEST",
        health={
            DeploymentDecision.ALLOW: 96,
            DeploymentDecision.WARN: 82,
            DeploymentDecision.BLOCK: 40,
        }[decision],
        decision=decision,
        severity=Severity.HIGH if decision == DeploymentDecision.BLOCK else Severity.LOW,
        confidence=90,
        root_cause="Revenue changed",
        recommendation="Review the historical deployment.",
        affected_models=["stg_orders"],
        signals=[
            Signal(
                "semantic_diff",
                Severity.HIGH,
                90,
                -30,
                reasons=["Revenue gained upstream dependency refunds"],
            )
        ],
        metadata={
            "previous_snapshot_loaded": True,
            "current_snapshot": _snapshot("current"),
            "current_snapshot_id": "current",
            "previous_snapshot_id": "baseline",
        },
    )


def _snapshot(snapshot_id):
    return {
        "snapshot_id": snapshot_id,
        "deployment_id": f"deploy-{snapshot_id}",
        "created_at": "2026-07-02T00:00:00+00:00",
        "changed_models": ["stg_orders"],
        "semantic_context": {
            "discovered_kpis": [{"name": "Revenue"}],
            "knowledge_report": {"contracts": [{"kpi_name": "Revenue"}]},
        },
        "decision": {"decision": "ALLOW", "health": 100},
        "incident_summary": {"incident_id": "INC-SNAPSHOT", "decision": "ALLOW"},
        "metadata": {"source": "unit-test"},
    }


if __name__ == "__main__":
    unittest.main()
