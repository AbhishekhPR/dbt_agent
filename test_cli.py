import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agent.cli import cli
from agent.decision_engine import DeploymentDecision
from agent.deployment_history import DeploymentHistoryStore
from agent.incident import Incident
from agent.signals import Severity, Signal


class CliTests(unittest.TestCase):
    def test_review_deployment_accepts_dbt_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["project_context"] = kwargs["project_context"]
                return _fake_analyzer()(**kwargs)

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=analyze,
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--dbt-manifest",
                        str(manifest_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium Deployment Decision", output)
        self.assertEqual(captured["project_context"]["model_names"], ["fct_orders", "stg_orders"])
        self.assertEqual(captured["project_context"]["metadata"]["source"], "dbt_manifest")

    def test_dbt_manifest_path_produces_successful_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--dbt-manifest",
                        str(manifest_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Previous Snapshot Loaded: NO", output)

    def test_review_deployment_command_exits_zero_with_valid_project_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium Deployment Decision", output)
        self.assertIn("Previous Snapshot Loaded: NO", output)

    def test_review_deployment_command_loads_history_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            store = DeploymentHistoryStore(history_path)
            store.save_snapshot(_snapshot("snap-previous", "deploy-previous"))

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Previous Snapshot Loaded: YES", output)
        self.assertIn("Previous Snapshot: snap-previous", output)

    def test_review_deployment_command_renders_previous_snapshot_loaded_no_when_history_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Previous Snapshot Loaded: NO", output)
        self.assertNotIn("Previous Snapshot: snap-previous", output)

    def test_review_deployment_command_writes_output_file_when_output_is_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            output_path = Path(tmp) / "review.txt"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                        "--output",
                        str(output_path),
                    ]
                )

            written = output_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Deployment review written to", output)
        self.assertIn("Relium Deployment Decision", written)
        self.assertIn("Previous Snapshot Loaded: NO", written)

    def test_review_deployment_markdown_format_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                        "--format",
                        "markdown",
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("# Relium Deployment Decision", output)
        self.assertIn("**Previous Snapshot Loaded:** NO", output)

    def test_review_deployment_json_format_works_and_is_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        payload = json.loads(output)
        serialized = json.dumps(payload)
        self.assertIsInstance(serialized, str)
        self.assertEqual(payload["deployment_lifecycle"]["previous_snapshot_loaded"], False)
        self.assertEqual(payload["decision"], "ALLOW")

    def test_review_deployment_auto_record_saves_allowed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(decision=DeploymentDecision.ALLOW),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                        "--auto-record",
                    ]
                )

            latest_snapshot = DeploymentHistoryStore(history_path).load_latest_snapshot()

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(latest_snapshot["snapshot_id"], "snap-current")
        self.assertIn("Saved Snapshot: snap-current", output)

    def test_review_deployment_blocked_snapshot_is_not_saved_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=_fake_analyzer(decision=DeploymentDecision.BLOCK),
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                        "--auto-record",
                    ]
                )

            snapshots = DeploymentHistoryStore(history_path).list_snapshots()

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(snapshots, [])
        self.assertNotIn("Saved Snapshot:", output)

    def test_review_deployment_invalid_project_context_path_exits_nonzero(self):
        result, output = _invoke(
            [
                "review-deployment",
                "--project-context",
                "missing.json",
                "--changed-model",
                "stg_orders",
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Project context file not found", output)

    def test_review_deployment_invalid_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = Path(tmp) / "project_context.json"
            context_path.write_text("{not json", encoding="utf-8")

            result, output = _invoke(
                [
                    "review-deployment",
                    "--project-context",
                    str(context_path),
                    "--changed-model",
                    "stg_orders",
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid project context JSON", output)

    def test_review_deployment_missing_changed_models_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)

            result, output = _invoke(
                [
                    "review-deployment",
                    "--project-context",
                    str(context_path),
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("At least one --changed-model is required", output)

    def test_missing_manifest_path_exits_nonzero(self):
        result, output = _invoke(
            [
                "review-deployment",
                "--dbt-manifest",
                "missing-manifest.json",
                "--changed-model",
                "stg_orders",
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Manifest file not found", output)

    def test_invalid_manifest_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{not json", encoding="utf-8")

            result, output = _invoke(
                [
                    "review-deployment",
                    "--dbt-manifest",
                    str(manifest_path),
                    "--changed-model",
                    "stg_orders",
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid manifest JSON", output)

    def test_neither_project_context_nor_dbt_manifest_exits_nonzero(self):
        result, output = _invoke(
            [
                "review-deployment",
                "--changed-model",
                "stg_orders",
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Exactly one of --project-context or --dbt-manifest is required", output)

    def test_both_project_context_and_dbt_manifest_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            manifest_path = _write_dbt_manifest(tmp)

            result, output = _invoke(
                [
                    "review-deployment",
                    "--project-context",
                    str(context_path),
                    "--dbt-manifest",
                    str(manifest_path),
                    "--changed-model",
                    "stg_orders",
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Exactly one of --project-context or --dbt-manifest is required", output)

    def test_existing_project_context_behavior_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["project_context"] = kwargs["project_context"]
                return _fake_analyzer()(**kwargs)

            with patch(
                "agent.deployment_lifecycle.analyze_pr_with_history",
                side_effect=analyze,
            ):
                result, output = _invoke(
                    [
                        "review-deployment",
                        "--project-context",
                        str(context_path),
                        "--changed-model",
                        "stg_orders",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(captured["project_context"]["model_names"], ["stg_orders", "fct_orders"])
        self.assertIn("Relium Deployment Decision", output)

    def test_existing_pr_review_demo_command_behavior_is_unchanged(self):
        result, output = _invoke(["pr-review-demo"])

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium AI Deployment Review", output)
        self.assertIn("Deployment Decision", output)

    def test_existing_cli_commands_unchanged(self):
        result, output = _invoke(["pr-review-demo"])

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium AI Deployment Review", output)


def _invoke(args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = CliRunner().invoke(cli, args)
    output = result.output + stdout.getvalue() + stderr.getvalue()
    return result, output


def _fake_analyzer(*, decision=DeploymentDecision.ALLOW):
    def analyze(**kwargs):
        history_store = kwargs.get("history_store")
        previous_snapshot = (
            history_store.load_latest_snapshot()
            if history_store is not None
            else None
        )
        current_snapshot = _snapshot(
            "snap-current",
            kwargs.get("deployment_id") or "deploy-current",
            decision=decision.value,
        )
        metadata = {
            "current_snapshot": current_snapshot,
            "history_enabled": history_store is not None,
            "previous_snapshot_loaded": previous_snapshot is not None,
        }
        if previous_snapshot is not None:
            metadata["previous_snapshot_id"] = previous_snapshot["snapshot_id"]
        return _incident(decision, metadata=metadata)

    return analyze


def _incident(decision, *, metadata):
    health = {
        DeploymentDecision.ALLOW: 100,
        DeploymentDecision.WARN: 80,
        DeploymentDecision.BLOCK: 40,
    }[decision]
    severity = Severity.HIGH if decision == DeploymentDecision.BLOCK else Severity.LOW
    return Incident(
        incident_id="INC-CLI",
        health=health,
        decision=decision,
        severity=severity,
        confidence=90,
        root_cause="",
        recommendation="Deploy normally.",
        affected_models=["stg_orders"],
        signals=[
            Signal(
                "metadata_checks",
                severity,
                90,
                0,
                reasons=["No blocking metadata issues"],
                metadata={"model_name": "stg_orders"},
            )
        ],
        metadata=metadata,
    )


def _write_project_context(tmp):
    path = Path(tmp) / "project_context.json"
    path.write_text(
        json.dumps(
            {
                "model_names": ["stg_orders", "fct_orders"],
                "column_names": ["order_id", "gross_revenue"],
                "metrics": [{"name": "Revenue", "model": "fct_orders"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_dbt_manifest(tmp):
    path = Path(tmp) / "manifest.json"
    path.write_text(json.dumps(_dbt_manifest()), encoding="utf-8")
    return path


def _dbt_manifest():
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
                    "payment_amount": {"name": "payment_amount"},
                },
                "depends_on": {
                    "nodes": ["source.jaffle_shop.raw_shop.orders"],
                },
                "config": {"materialized": "view"},
            },
            "model.jaffle_shop.fct_orders": {
                "resource_type": "model",
                "name": "fct_orders",
                "unique_id": "model.jaffle_shop.fct_orders",
                "path": "marts/fct_orders.sql",
                "columns": {
                    "order_id": {"name": "order_id"},
                    "gross_revenue": {"name": "gross_revenue"},
                },
                "depends_on": {
                    "nodes": ["model.jaffle_shop.stg_orders"],
                },
                "config": {"materialized": "table"},
            },
        },
        "sources": {
            "source.jaffle_shop.raw_shop.orders": {
                "resource_type": "source",
                "name": "orders",
                "source_name": "raw_shop",
                "table_name": "orders",
                "unique_id": "source.jaffle_shop.raw_shop.orders",
            }
        },
        "metrics": {
            "metric.jaffle_shop.revenue": {
                "name": "Revenue / GMV",
                "label": "Revenue",
                "type": "simple",
                "description": "Completed customer payment volume.",
                "model": "ref('fct_orders')",
            }
        },
    }


def _snapshot(snapshot_id, deployment_id, *, decision="ALLOW"):
    return {
        "snapshot_id": snapshot_id,
        "deployment_id": deployment_id,
        "created_at": "2026-07-02T00:00:00+00:00",
        "changed_models": ["stg_orders"],
        "semantic_context": {
            "discovered_kpis": [{"name": "Revenue"}],
            "knowledge_report": {"contracts": [{"kpi_name": "Revenue"}]},
        },
        "decision": {"decision": decision, "health": 100},
        "incident_summary": {"incident_id": "INC-CLI", "decision": decision},
        "metadata": {"source": "unit-test"},
    }


if __name__ == "__main__":
    unittest.main()
