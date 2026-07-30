import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


def invoke_cli(runner, cli, args):
    escaped_stdout = io.StringIO()
    escaped_stderr = io.StringIO()
    with redirect_stdout(escaped_stdout), redirect_stderr(escaped_stderr):
        result = runner.invoke(cli, args)
    output = result.output
    escaped_output = escaped_stdout.getvalue() + escaped_stderr.getvalue()
    if escaped_output:
        output += escaped_output
    return result, output


def _artifact_paths(root):
    root = Path(root)
    return {
        "metadata_db_path": root / "relium_metadata.db",
        "warehouse_db_path": root / "demo_pipeline.db",
        "markdown_report_path": root / "pipeline_validation_report.md",
        "json_report_path": root / "pipeline_validation_report.json",
    }


def _run_service(module, root, scenario="normal"):
    return module.run_demo_pipeline(
        scenario=scenario,
        **_artifact_paths(root),
    )


def _cli_args(workspace, scenario="normal", decision=False):
    args = [
        "demo-pipeline",
        "--workspace",
        str(workspace),
        "--scenario",
        scenario,
    ]
    if decision:
        args.append("--decision")
    return args


class DemoPipelineTests(unittest.TestCase):
    def test_risky_left_join_model_produces_high_risk(self):
        from agent.ast_analyzer import run_ast_analysis
        from agent.demo_pipeline import RISKY_MODEL_SQL

        report = run_ast_analysis(RISKY_MODEL_SQL, "fct_customer_lifetime_value")

        self.assertEqual(report["overall_risk"], "high")
        self.assertFalse(report["safe_to_run"])

    def test_demo_pipeline_command_completes_locally(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            result, output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace),
            )

            self.assertEqual(result.exit_code, 0, output)
            self.assertNotIn("Slack", output)
            self.assertIn("Relium Demo Pipeline", output)
            self.assertIn("Scenario: normal", output)
            self.assertIn("Raw rows loaded:", output)
            self.assertIn("Model built: fct_customer_lifetime_value", output)
            self.assertIn("AST risk found: HIGH", output)
            self.assertIn("Safe to continue: NO", output)
            self.assertIn("Metadata stored: YES", output)

            connection = sqlite3.connect(workspace / "relium_metadata.db")
            try:
                scan_runs = connection.execute(
                    "SELECT COUNT(*) FROM relium_scan_runs"
                ).fetchone()[0]
                metrics = connection.execute(
                    "SELECT COUNT(*) FROM relium_model_metrics"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(scan_runs, 1)
        self.assertEqual(metrics, 1)

    def test_run_demo_pipeline_returns_result_without_printing_report(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp, patch(
            "builtins.print"
        ) as printed:
            result = _run_service(demo_pipeline, tmp)

        printed.assert_not_called()
        self.assertEqual(result["scenario"], "normal")
        self.assertIn("Raw rows loaded:", result["report_text"])

    def test_demo_pipeline_result_includes_internal_incident(self):
        from agent import demo_pipeline
        from agent.incident import Incident

        with tempfile.TemporaryDirectory() as tmp:
            result = _run_service(demo_pipeline, tmp)

        self.assertIn("incident", result)
        self.assertIn("incident_summary", result)
        self.assertIsInstance(result["incident"], Incident)

    def test_demo_pipeline_previous_result_fields_are_unchanged(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            result = _run_service(demo_pipeline, tmp)

        expected = {
            "project_name": "relium_demo",
            "scenario": "normal",
            "raw_row_count": 7,
            "model_name": "fct_customer_lifetime_value",
            "changed_model": "fct_customer_lifetime_value",
            "severity": "HIGH",
            "static_analysis_text": "Potential LEFT JOIN nullification detected.",
            "affected_models": [],
            "row_count": 2,
            "null_count": 0,
            "duplicate_count": 1,
            "freshness_timestamp": "2026-06-21T12:00:00",
            "schema_column_count": 6,
            "safe_to_continue": False,
            "metadata_stored": True,
        }
        for key, value in expected.items():
            self.assertEqual(result[key], value, key)
        self.assertNotIn("slack_sent", result)

    def test_demo_pipeline_visible_output_remains_identical(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            result = _run_service(demo_pipeline, tmp)

        self.assertEqual(
            result["report_text"],
            "\n".join(
                [
                    "Relium Demo Pipeline",
                    "",
                    "Scenario: normal",
                    "Raw rows loaded: 7",
                    "Model built: fct_customer_lifetime_value",
                    "AST risk found: HIGH",
                    "Row count: 2",
                    "Null count: 0",
                    "Duplicate customer_id count: 1",
                    "Freshness timestamp: 2026-06-21T12:00:00",
                    "Schema columns: 6",
                    "Safe to continue: NO",
                    "Metadata stored: YES",
                ]
            ),
        )

    def test_demo_pipeline_incident_contains_all_available_signals(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            first = _run_service(demo_pipeline, tmp)
            second = _run_service(demo_pipeline, tmp, "duplicate-spike")

        self.assertEqual(
            [signal.component for signal in first["incident"].signals],
            ["ast", "metadata_checks"],
        )
        self.assertEqual(
            [signal.component for signal in second["incident"].signals],
            ["ast", "metadata_checks", "metadata_drift"],
        )
        self.assertEqual(
            second["incident_summary"]["signal_components"],
            ["ast", "metadata_checks", "metadata_drift"],
        )

    def test_demo_pipeline_command_does_not_call_webhook_when_configured(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/test"},
        ), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("unexpected webhook"),
        ) as urlopen:
            workspace = Path(tmp) / "workspace"
            result, output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace),
            )

        self.assertEqual(result.exit_code, 0, output)
        urlopen.assert_not_called()
        self.assertNotIn("Slack", output)
        self.assertIn("Relium Demo Pipeline", output)

    def test_demo_pipeline_command_emits_only_through_click_output(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "builtins.print"
        ) as printed:
            result, output = invoke_cli(
                runner,
                cli,
                _cli_args(Path(tmp) / "workspace"),
            )

        self.assertEqual(result.exit_code, 0, output)
        printed.assert_not_called()
        self.assertIn("Relium Demo Pipeline", output)
        self.assertIn("Scenario: normal", output)

    def test_demo_pipeline_command_output_without_decision_is_local_report(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            result, output = invoke_cli(runner, cli, _cli_args(workspace))

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn(f"Demo workspace: {workspace.resolve()}", output)
        self.assertIn("Relium Demo Pipeline", output)
        self.assertNotIn("Relium Deployment Decision", output)
        self.assertNotIn("Slack", output)

    def test_demo_pipeline_command_decision_flag_adds_decision_view(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result, output = invoke_cli(
                runner,
                cli,
                _cli_args(Path(tmp) / "workspace", decision=True),
            )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium Demo Pipeline", output)
        self.assertIn("Metadata stored: YES\n\nRelium Deployment Decision", output)
        self.assertIn("Pipeline Health:", output)
        self.assertIn("Deployment Decision:", output)
        self.assertIn("Severity:", output)
        self.assertIn("Signals Considered:", output)
        self.assertIn("- ast", output)
        self.assertIn("- metadata_checks", output)

    def test_demo_pipeline_command_decision_flag_supports_demo_scenarios(self):
        from agent.cli import cli

        runner = CliRunner()
        scenarios = [
            "normal",
            "row-drop",
            "duplicate-spike",
            "freshness-regression",
        ]

        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                result, output = invoke_cli(
                    runner,
                    cli,
                    _cli_args(Path(tmp) / "workspace", scenario, decision=True),
                )

                self.assertEqual(result.exit_code, 0, output)
                self.assertIn(f"Scenario: {scenario}", output)
                self.assertIn("Relium Deployment Decision", output)
                self.assertIn("Signals Considered:", output)

    def test_demo_pipeline_command_accepts_deterministic_scenarios(self):
        from agent.cli import cli

        runner = CliRunner()
        scenarios = {
            "normal": (2, 1, "2026-06-21T12:00:00"),
            "row-drop": (1, 0, "2026-06-20T12:00:00"),
            "duplicate-spike": (6, 5, "2026-06-24T12:00:00"),
            "freshness-regression": (2, 1, "2026-06-19T12:00:00"),
        }

        for scenario, expected in scenarios.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                result, output = invoke_cli(
                    runner,
                    cli,
                    _cli_args(Path(tmp) / "workspace", scenario),
                )

                self.assertEqual(result.exit_code, 0, output)
                self.assertNotIn("Slack", output)
                self.assertIn(f"Scenario: {scenario}", output)
                self.assertIn(f"Row count: {expected[0]}", output)
                self.assertIn(
                    f"Duplicate customer_id count: {expected[1]}",
                    output,
                )
                self.assertIn(
                    f"Freshness timestamp: {expected[2]}",
                    output,
                )
                self.assertIn("Metadata stored: YES", output)

    def test_duplicate_spike_renders_high_drift_locally(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("unexpected webhook"),
        ):
            workspace = Path(tmp) / "workspace"
            baseline, baseline_output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace),
            )
            drift_run, drift_output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace, "duplicate-spike", decision=True),
            )

        self.assertEqual(baseline.exit_code, 0, baseline_output)
        self.assertEqual(drift_run.exit_code, 0, drift_output)
        self.assertIn("Metadata Drift", drift_output)
        self.assertIn("- metadata_drift", drift_output)
        self.assertNotIn("Slack", drift_output)

    def test_freshness_regression_renders_high_drift_locally(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("unexpected webhook"),
        ):
            workspace = Path(tmp) / "workspace"
            baseline, baseline_output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace),
            )
            drift_run, drift_output = invoke_cli(
                runner,
                cli,
                _cli_args(workspace, "freshness-regression", decision=True),
            )

        self.assertEqual(baseline.exit_code, 0, baseline_output)
        self.assertEqual(drift_run.exit_code, 0, drift_output)
        self.assertIn("- metadata_drift", drift_output)
        self.assertNotIn("Slack", drift_output)


if __name__ == "__main__":
    unittest.main()
