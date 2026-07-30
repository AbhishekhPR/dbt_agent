import json
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class DemoPipelineTests(unittest.TestCase):
    def test_risky_left_join_model_produces_high_risk(self):
        from agent.demo_pipeline import RISKY_MODEL_SQL
        from agent.ast_analyzer import run_ast_analysis

        report = run_ast_analysis(RISKY_MODEL_SQL, "fct_customer_lifetime_value")

        self.assertEqual(report["overall_risk"], "high")
        self.assertFalse(report["safe_to_run"])

    def test_demo_pipeline_command_completes_locally(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result, output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )

            self.assertEqual(result.exit_code, 0, output)
            self.assertIn("Slack alert sent: NO", output)
            self.assertIn("Relium Demo Pipeline", output)
            self.assertIn("Scenario: normal", output)
            self.assertIn("Raw rows loaded:", output)
            self.assertIn("Model built: fct_customer_lifetime_value", output)
            self.assertIn("AST risk found: HIGH", output)
            self.assertIn("Safe to continue: NO", output)
            self.assertIn("Metadata stored: YES", output)

            conn = sqlite3.connect(metadata_db)
            scan_runs = conn.execute(
                "SELECT COUNT(*) FROM relium_scan_runs"
            ).fetchone()[0]
            metrics = conn.execute(
                "SELECT COUNT(*) FROM relium_model_metrics"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(scan_runs, 1)
        self.assertEqual(metrics, 1)

    def test_run_demo_pipeline_returns_result_without_printing_report(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch.dict(
                "os.environ",
                {},
                clear=True,
            ), patch("builtins.print") as printed:
                result = demo_pipeline.run_demo_pipeline(scenario="normal")

        printed.assert_not_called()
        self.assertEqual(result["scenario"], "normal")
        self.assertIn("Raw rows loaded:", result["report_text"])

    def test_demo_pipeline_result_includes_internal_incident(self):
        from agent import demo_pipeline
        from agent.incident import Incident

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result = demo_pipeline.run_demo_pipeline(scenario="normal")

        self.assertIn("incident", result)
        self.assertIn("incident_summary", result)
        self.assertIsInstance(result["incident"], Incident)

    def test_demo_pipeline_previous_result_fields_are_unchanged(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result = demo_pipeline.run_demo_pipeline(scenario="normal")

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
            "slack_sent": False,
        }
        for key, value in expected.items():
            self.assertEqual(result[key], value, key)

    def test_demo_pipeline_visible_output_remains_identical(self):
        from agent import demo_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result = demo_pipeline.run_demo_pipeline(scenario="normal")

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
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                first = demo_pipeline.run_demo_pipeline(scenario="normal")
                second = demo_pipeline.run_demo_pipeline(scenario="duplicate-spike")

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

    def test_demo_pipeline_command_does_not_call_webhook_when_slack_is_mocked(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch.dict(
                "os.environ",
                {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/test"},
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ), patch("urllib.request.urlopen") as urlopen:
                result, output = invoke_cli(runner, cli, ["demo-pipeline"])

        self.assertEqual(result.exit_code, 0, output)
        urlopen.assert_not_called()
        self.assertIn("Slack alert sent: NO", output)
        self.assertIn("Relium Demo Pipeline", output)

    def test_demo_pipeline_command_emits_only_through_click_output(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ), patch("builtins.print") as printed:
                result, output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )

        self.assertEqual(result.exit_code, 0, output)
        printed.assert_not_called()
        self.assertIn("Slack alert sent: NO", output)
        self.assertIn("Relium Demo Pipeline", output)
        self.assertIn("Scenario: normal", output)

    def test_demo_pipeline_command_output_is_unchanged_without_decision_flag(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result, output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )

        self.assertEqual(result.exit_code, 0, output)
        self.assertEqual(
            output,
            "\n".join(
                [
                    "Slack alert sent: NO",
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
                    "",
                ]
            ),
        )
        self.assertNotIn("Relium Deployment Decision", output)

    def test_demo_pipeline_command_decision_flag_adds_decision_view(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result, output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal", "--decision"],
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
        from agent import demo_pipeline
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
                tmp_path = Path(tmp)
                metadata_db = tmp_path / "relium_metadata.db"
                warehouse_db = tmp_path / "demo_warehouse.db"

                with patch.object(
                    demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
                ), patch.object(
                    demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
                ), patch(
                    "agent.slack_alerts.send_validation_alert",
                    return_value=False,
                ):
                    result, output = invoke_cli(
                        runner,
                        cli,
                        ["demo-pipeline", "--scenario", scenario, "--decision"],
                    )

            self.assertEqual(result.exit_code, 0, output)
            self.assertIn(f"Scenario: {scenario}", output)
            self.assertIn("Relium Deployment Decision", output)
            self.assertIn("Signals Considered:", output)

    def test_demo_pipeline_command_accepts_deterministic_scenarios(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        scenarios = {
            "normal": {
                "row_count": 2,
                "duplicate_count": 1,
                "freshness_timestamp": "2026-06-21T12:00:00",
            },
            "row-drop": {
                "row_count": 1,
                "duplicate_count": 0,
                "freshness_timestamp": "2026-06-20T12:00:00",
            },
            "duplicate-spike": {
                "row_count": 6,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
            },
            "freshness-regression": {
                "row_count": 2,
                "duplicate_count": 1,
                "freshness_timestamp": "2026-06-19T12:00:00",
            },
        }

        for scenario, expected in scenarios.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                metadata_db = tmp_path / "relium_metadata.db"
                warehouse_db = tmp_path / "demo_warehouse.db"

                with patch.object(
                    demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
                ), patch.object(
                    demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
                ), patch(
                    "agent.slack_alerts.send_validation_alert",
                    return_value=False,
                ):
                    result, output = invoke_cli(
                        runner,
                        cli,
                        ["demo-pipeline", "--scenario", scenario],
                    )

                self.assertEqual(result.exit_code, 0, output)
                self.assertIn("Slack alert sent: NO", output)
                self.assertIn("Relium Demo Pipeline", output)
                self.assertIn(f"Scenario: {scenario}", output)
                self.assertIn("Raw rows loaded:", output)
                self.assertIn("Model built: fct_customer_lifetime_value", output)
                self.assertIn("AST risk found: HIGH", output)
                self.assertIn(f"Row count: {expected['row_count']}", output)
                self.assertIn(
                    f"Duplicate customer_id count: {expected['duplicate_count']}",
                    output,
                )
                self.assertIn(
                    f"Freshness timestamp: {expected['freshness_timestamp']}",
                    output,
                )
                self.assertIn("Metadata stored: YES", output)

    def test_duplicate_spike_sends_clear_high_drift_alert_text(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"
            sent_payloads = []

            def capture_payload(request):
                sent_payloads.append(json.loads(request.data.decode("utf-8")))
                response = MagicMock()
                response.status = 200
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                return response

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://example.test/hook"}), patch(
                "urllib.request.urlopen",
                side_effect=capture_payload,
            ):
                baseline, baseline_output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )
                self.assertEqual(baseline.exit_code, 0, baseline_output)
                drift_run, drift_output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "duplicate-spike"],
                )

            self.assertEqual(drift_run.exit_code, 0, drift_output)
            self.assertEqual(len(sent_payloads), 2)
            alert_text = json.dumps(sent_payloads[-1])
            payload_text = sent_payloads[-1]["text"]
            self.assertIn("Relium Pipeline Risk Alert", alert_text)
            self.assertIn("Project: relium_demo", alert_text)
            self.assertIn("Model: fct_customer_lifetime_value", alert_text)
            self.assertIn("Risk: HIGH", alert_text)
            self.assertIn("Safe to continue: NO", alert_text)
            self.assertIn("Static analysis", alert_text)
            self.assertIn("Metadata checks", alert_text)
            self.assertIn("Duplicate customer_id count: 5", alert_text)
            self.assertIn("Drift detection", alert_text)
            self.assertIn("Row count change: +200%", alert_text)
            self.assertIn("Duplicate count change: +400%", alert_text)
            self.assertIn("Metadata Drift: HIGH", alert_text)
            self.assertIn("\n\nStatic analysis:\n", payload_text)
            self.assertIn("\n\nMetadata checks:\n", payload_text)
            self.assertIn("\n\nDrift detection:\n", payload_text)
            self.assertIn("\n\nRecommendation:\n", payload_text)

    def test_freshness_regression_sends_clear_high_drift_alert_text(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"
            sent_payloads = []

            def capture_payload(request):
                sent_payloads.append(json.loads(request.data.decode("utf-8")))
                response = MagicMock()
                response.status = 200
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                return response

            with patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://example.test/hook"}), patch(
                "urllib.request.urlopen",
                side_effect=capture_payload,
            ):
                baseline, baseline_output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )
                self.assertEqual(baseline.exit_code, 0, baseline_output)
                drift_run, drift_output = invoke_cli(
                    runner,
                    cli,
                    ["demo-pipeline", "--scenario", "freshness-regression"],
                )

            self.assertEqual(drift_run.exit_code, 0, drift_output)
            self.assertEqual(len(sent_payloads), 2)
            alert_text = json.dumps(sent_payloads[-1])
            self.assertIn("Relium Pipeline Risk Alert", alert_text)
            self.assertIn("Drift detection", alert_text)
            self.assertIn("Freshness regression: YES", alert_text)
            self.assertIn("Metadata Drift: HIGH", alert_text)


if __name__ == "__main__":
    unittest.main()
