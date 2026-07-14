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
from agent.deployment_outcomes import DeploymentOutcome, DeploymentOutcomeStore
from agent.incident import Incident
from agent.signals import Severity, Signal


DEMO_DIR = Path(__file__).parent / "demo" / "history_aware"


class CliTests(unittest.TestCase):
    def test_review_deployment_accepts_dbt_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["project_context"] = kwargs["project_context"]
                captured["changed_models"] = kwargs["changed_models"]
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
        self.assertEqual(
            captured["changed_models"][0]["sql"],
            "select order_id, payment_amount from raw_shop.orders",
        )
        self.assertEqual(captured["changed_models"][0]["sql_source"], "compiled_code")
        self.assertNotEqual(captured["changed_models"][0]["sql"], "select * from stg_orders")

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

    def test_init_baseline_creates_deployment_history_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"

            result, output = _invoke(
                [
                    "init-baseline",
                    "--dbt-manifest",
                    str(manifest_path),
                    "--history-path",
                    str(history_path),
                    "--deployment-id",
                    "production-baseline",
                ]
            )

            history_exists = history_path.exists()
            latest_snapshot = DeploymentHistoryStore(history_path).load_latest_snapshot()

        self.assertEqual(result.exit_code, 0, output)
        self.assertTrue(history_exists)
        self.assertIsNotNone(latest_snapshot)
        self.assertIn("Baseline snapshot initialized", output)

    def test_init_baseline_output_includes_snapshot_id_and_history_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"

            result, output = _invoke(
                [
                    "init-baseline",
                    "--dbt-manifest",
                    str(manifest_path),
                    "--history-path",
                    str(history_path),
                ]
            )

            latest_snapshot = DeploymentHistoryStore(history_path).load_latest_snapshot()

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn(latest_snapshot["snapshot_id"], output)
        self.assertIn(str(history_path), output)
        self.assertIn("Deployment ID: production-baseline", output)
        self.assertIn("Models: 2", output)
        self.assertIn("Discovered KPIs: 1", output)

    def test_init_baseline_missing_manifest_exits_nonzero(self):
        result, output = _invoke(
            [
                "init-baseline",
                "--dbt-manifest",
                "missing-manifest.json",
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Manifest file not found", output)

    def test_init_baseline_invalid_manifest_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text("{not json", encoding="utf-8")

            result, output = _invoke(
                [
                    "init-baseline",
                    "--dbt-manifest",
                    str(manifest_path),
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid manifest JSON", output)

    def test_init_baseline_empty_project_context_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_empty_dbt_manifest(tmp)

            result, output = _invoke(
                [
                    "init-baseline",
                    "--dbt-manifest",
                    str(manifest_path),
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No dbt models found", output)

    def test_review_deployment_accepts_changed_file_with_dbt_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["changed_models"] = kwargs["changed_models"]
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
                        "--changed-file",
                        "models/staging/stg_orders.sql",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium Deployment Decision", output)
        self.assertEqual(
            [model["model_name"] for model in captured["changed_models"]],
            ["stg_orders"],
        )

    def test_changed_file_infers_model_and_produces_successful_review(self):
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
                        "--changed-file",
                        "models/marts/fct_orders.sql",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Previous Snapshot Loaded: NO", output)

    def test_multiple_changed_files_deduplicate_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["changed_models"] = kwargs["changed_models"]
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
                        "--changed-file",
                        "models/staging/stg_orders.sql",
                        "--changed-file",
                        "./models/staging/stg_orders.sql",
                        "--changed-file",
                        "models/marts/fct_orders.sql",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(
            [model["model_name"] for model in captured["changed_models"]],
            ["stg_orders", "fct_orders"],
        )

    def test_explicit_changed_model_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["changed_models"] = kwargs["changed_models"]
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
        self.assertEqual(
            [model["model_name"] for model in captured["changed_models"]],
            ["stg_orders"],
        )

    def test_changed_file_and_changed_model_combine_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)
            history_path = Path(tmp) / "history.json"
            captured = {}

            def analyze(**kwargs):
                captured["changed_models"] = kwargs["changed_models"]
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
                        "fct_orders",
                        "--changed-file",
                        "models/staging/stg_orders.sql",
                        "--changed-file",
                        "models/marts/fct_orders.sql",
                        "--history-path",
                        str(history_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(
            [model["model_name"] for model in captured["changed_models"]],
            ["fct_orders", "stg_orders"],
        )

    def test_changed_file_only_review_uses_column_level_semantic_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_manifest = _write_lineage_manifest(
                tmp,
                "manifest_previous.json",
                columns=["net_revenue"],
                compiled_code="select order_total as net_revenue from stg_orders",
            )
            current_manifest = _write_lineage_manifest(
                tmp,
                "manifest_current.json",
                columns=["net_revenue", "debug_flag"],
                compiled_code=(
                    "select order_total as net_revenue, "
                    "true as debug_flag from stg_orders"
                ),
            )
            history_path = Path(tmp) / "history.json"

            baseline_result, baseline_output = _invoke(
                [
                    "init-baseline",
                    "--dbt-manifest",
                    str(previous_manifest),
                    "--history-path",
                    str(history_path),
                ]
            )
            review_result, review_output = _invoke(
                [
                    "review-deployment",
                    "--dbt-manifest",
                    str(current_manifest),
                    "--changed-file",
                    "models/marts/fct_revenue.sql",
                    "--history-path",
                    str(history_path),
                    "--format",
                    "markdown",
                ]
            )

        self.assertEqual(baseline_result.exit_code, 0, baseline_output)
        self.assertEqual(review_result.exit_code, 0, review_output)
        self.assertIn("Column-Level Lineage", review_output)
        self.assertIn("fct_revenue.debug_flag output column was added", review_output)
        self.assertIn("Revenue does not read fct_revenue.debug_flag", review_output)

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

    def test_review_deployment_loads_outcomes_file_and_includes_outcome_memory_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            outcomes_path = Path(tmp) / "deployment_outcomes.json"
            DeploymentOutcomeStore(outcomes_path).save_outcome(
                DeploymentOutcome(
                    outcome_id="out-1",
                    deployment_id="deploy-previous",
                    decision=DeploymentDecision.ALLOW,
                    outcome="incident_occurred",
                    created_at="2026-07-02T00:00:00+00:00",
                )
            )

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
                        "--outcomes-path",
                        str(outcomes_path),
                        "--format",
                        "markdown",
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("- deployment_outcomes", output)
        self.assertIn("## Deployment Outcome Memory", output)
        self.assertIn(
            "Previous allowed or warned deployments were followed by incidents",
            output,
        )

    def test_review_deployment_behavior_unchanged_when_no_outcomes_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)
            history_path = Path(tmp) / "history.json"
            outcomes_path = Path(tmp) / "missing_outcomes.json"
            captured = {}

            def analyze(**kwargs):
                captured.update(kwargs)
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
                        "--outcomes-path",
                        str(outcomes_path),
                    ]
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertNotIn("outcomes", captured)
        self.assertNotIn("deployment_outcomes", output)
        self.assertNotIn("Deployment Outcome Memory", output)

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

    def test_changed_file_without_dbt_manifest_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            context_path = _write_project_context(tmp)

            result, output = _invoke(
                [
                    "review-deployment",
                    "--project-context",
                    str(context_path),
                    "--changed-file",
                    "models/staging/stg_orders.sql",
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--changed-file requires --dbt-manifest", output)

    def test_unmapped_changed_file_exits_nonzero_when_no_changed_model_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = _write_dbt_manifest(tmp)

            result, output = _invoke(
                [
                    "review-deployment",
                    "--dbt-manifest",
                    str(manifest_path),
                    "--changed-file",
                    "README.md",
                ]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("At least one changed model is required", output)

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

    def test_history_aware_demo_current_manifest_produces_successful_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "deployment_history.json"

            result, output = _invoke(
                [
                    "review-deployment",
                    "--dbt-manifest",
                    str(DEMO_DIR / "manifest_current.json"),
                    "--changed-model",
                    "fct_revenue",
                    "--history-path",
                    str(history_path),
                    "--deployment-id",
                    "current_deploy",
                    "--format",
                    "markdown",
                ]
            )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Deployment Decision", output)
        self.assertIn("Pipeline Health", output)

    def test_history_aware_demo_loads_previous_snapshot_and_shows_semantic_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "deployment_history.json"
            history_path.write_text(
                (DEMO_DIR / ".relium" / "deployment_history.json").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            current_result, current_output = _invoke(
                [
                    "review-deployment",
                    "--dbt-manifest",
                    str(DEMO_DIR / "manifest_current.json"),
                    "--changed-model",
                    "fct_revenue",
                    "--history-path",
                    str(history_path),
                    "--deployment-id",
                    "current_deploy",
                    "--format",
                    "markdown",
                ]
            )

        self.assertEqual(current_result.exit_code, 0, current_output)
        self.assertIn("**Previous Snapshot Loaded:** YES", current_output)
        self.assertIn("### Historical Semantic Change", current_output)
        self.assertIn("Revenue gained upstream dependency refunds", current_output)

    def test_history_aware_demo_prebuilt_history_fixture_supports_one_command_review(self):
        result, output = _invoke(
            [
                "review-deployment",
                "--dbt-manifest",
                str(DEMO_DIR / "manifest_current.json"),
                "--changed-model",
                "fct_revenue",
                "--history-path",
                str(DEMO_DIR / ".relium" / "deployment_history.json"),
                "--deployment-id",
                "current_deploy",
                "--format",
                "markdown",
            ]
        )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("**Previous Snapshot Loaded:** YES", output)
        self.assertIn("Revenue gained upstream dependency refunds", output)

    def test_backtest_deployment_uses_baseline_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_manifest = _write_dbt_manifest(tmp)
            current_manifest = Path(tmp) / "manifest_current.json"
            current_manifest.write_text(
                baseline_manifest.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            result, output = _invoke(
                [
                    "backtest-deployment",
                    "--baseline-manifest",
                    str(baseline_manifest),
                    "--dbt-manifest",
                    str(current_manifest),
                    "--changed-model",
                    "stg_orders",
                    "--deployment-id",
                    "historical-42",
                    "--format",
                    "markdown",
                ]
            )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("# Relium Backtest Result", output)
        self.assertIn("## Historical Deployment\nhistorical-42", output)
        self.assertIn("## Would Have Decided\nWOULD", output)
        self.assertIn("## What Relium Would Have Caught", output)
        self.assertNotIn("# Relium Deployment Decision", output)

    def test_backtest_deployment_json_includes_backtest_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline_manifest = _write_dbt_manifest(tmp)
            current_manifest = Path(tmp) / "manifest_current.json"
            current_manifest.write_text(
                baseline_manifest.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            result, output = _invoke(
                [
                    "backtest-deployment",
                    "--baseline-manifest",
                    str(baseline_manifest),
                    "--dbt-manifest",
                    str(current_manifest),
                    "--changed-model",
                    "stg_orders",
                    "--deployment-id",
                    "historical-42",
                    "--format",
                    "json",
                ]
            )

        payload = json.loads(output)

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(payload["backtest"]["historical_deployment_id"], "historical-42")
        self.assertIn(payload["backtest"]["would_have_decision"], ["ALLOW", "WARN", "BLOCK"])
        self.assertTrue(payload["backtest"]["previous_snapshot_loaded"])

    def test_backtest_deployment_requires_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_manifest = _write_dbt_manifest(tmp)

            result, output = _invoke(
                [
                    "backtest-deployment",
                    "--dbt-manifest",
                    str(current_manifest),
                    "--changed-model",
                    "stg_orders",
                    "--history-path",
                    str(Path(tmp) / "missing-history.json"),
                ]
            )

        self.assertNotEqual(result.exit_code, 0, output)
        self.assertIn("Backtest requires a previous production snapshot", output)

    def test_record_outcome_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes_path = Path(tmp) / "deployment_outcomes.json"

            result, output = _invoke(
                [
                    "record-outcome",
                    "--deployment-id",
                    "deploy-1",
                    "--decision",
                    "BLOCK",
                    "--outcome",
                    "false_positive",
                    "--snapshot-id",
                    "snap-1",
                    "--notes",
                    "Engineer reviewed and approved",
                    "--metadata",
                    '{"reviewer":"analytics"}',
                    "--outcomes-path",
                    str(outcomes_path),
                ]
            )

            payload = json.loads(outcomes_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Outcome recorded", output)
        self.assertEqual(payload["outcomes"][0]["deployment_id"], "deploy-1")
        self.assertEqual(payload["outcomes"][0]["decision"], "BLOCK")
        self.assertEqual(payload["outcomes"][0]["outcome"], "false_positive")
        self.assertEqual(payload["outcomes"][0]["snapshot_id"], "snap-1")
        self.assertEqual(payload["outcomes"][0]["notes"], "Engineer reviewed and approved")
        self.assertEqual(payload["outcomes"][0]["metadata"], {"reviewer": "analytics"})

    def test_outcome_summary_prints_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes_path = Path(tmp) / "deployment_outcomes.json"
            record_result, record_output = _invoke(
                [
                    "record-outcome",
                    "--deployment-id",
                    "deploy-1",
                    "--decision",
                    "ALLOW",
                    "--outcome",
                    "incident_occurred",
                    "--outcomes-path",
                    str(outcomes_path),
                ]
            )

            result, output = _invoke(
                [
                    "outcome-summary",
                    "--outcomes-path",
                    str(outcomes_path),
                ]
            )

        self.assertEqual(record_result.exit_code, 0, record_output)
        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Deployment Outcome Summary", output)
        self.assertIn("Total outcomes: 1", output)
        self.assertIn("Incidents after ALLOW: 1", output)
        self.assertIn("False positives: 0", output)


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
        incident = _incident(decision, metadata=metadata)
        if kwargs.get("outcomes"):
            incident.signals.append(
                Signal(
                    "deployment_outcomes",
                    Severity.MEDIUM,
                    75,
                    -10,
                    reasons=[
                        "Previous allowed or warned deployments were followed by incidents"
                    ],
                    metadata={"total_outcomes": len(kwargs["outcomes"])},
                )
            )
        return incident

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


def _write_empty_dbt_manifest(tmp):
    path = Path(tmp) / "manifest.json"
    payload = {
        "metadata": {
            "project_name": "empty_project",
            "dbt_version": "1.8.0",
        },
        "nodes": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_lineage_manifest(tmp, filename, *, columns, compiled_code):
    path = Path(tmp) / filename
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "jaffle_shop",
                    "dbt_version": "1.8.0",
                },
                "nodes": {
                    "model.jaffle_shop.fct_revenue": {
                        "resource_type": "model",
                        "name": "fct_revenue",
                        "unique_id": "model.jaffle_shop.fct_revenue",
                        "original_file_path": "models/marts/fct_revenue.sql",
                        "compiled_code": compiled_code,
                        "columns": {
                            column: {"name": column}
                            for column in columns
                        },
                    }
                },
                "metrics": {
                    "metric.jaffle_shop.revenue": {
                        "name": "Revenue",
                        "label": "Revenue",
                        "type": "simple",
                        "model": "ref('fct_revenue')",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
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
                "compiled_code": "select order_id, payment_amount from raw_shop.orders",
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
                "compiled_code": "select order_id, payment_amount as gross_revenue from stg_orders",
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
