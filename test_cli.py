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

    def test_existing_pr_review_demo_command_behavior_is_unchanged(self):
        result, output = _invoke(["pr-review-demo"])

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium AI Deployment Review", output)
        self.assertIn("Deployment Decision", output)


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
