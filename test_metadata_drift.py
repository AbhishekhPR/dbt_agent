import io
import tempfile
import unittest
import sqlite3
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


class MetadataDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "relium_metadata.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_two_runs(self):
        from agent.metadata_store import (
            ModelMetricRecord,
            ScanRunRecord,
            insert_model_metrics,
            insert_scan_run,
        )

        previous_scan_id = insert_scan_run(
            self.db_path,
            ScanRunRecord(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                risk_level="LOW",
                safe_to_merge=True,
                report_text="previous",
                timestamp="2026-06-24T10:00:00+00:00",
            ),
        )
        insert_model_metrics(
            self.db_path,
            ModelMetricRecord(
                scan_id=previous_scan_id,
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                row_count=10,
                null_count=2,
                duplicate_count=1,
                freshness_timestamp="2026-06-24T10:00:00",
                schema_column_count=5,
                timestamp="2026-06-24T10:00:00+00:00",
            ),
        )

        current_scan_id = insert_scan_run(
            self.db_path,
            ScanRunRecord(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                risk_level="HIGH",
                safe_to_merge=False,
                report_text="current",
                timestamp="2026-06-24T11:00:00+00:00",
            ),
        )
        insert_model_metrics(
            self.db_path,
            ModelMetricRecord(
                scan_id=current_scan_id,
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                row_count=5,
                null_count=2,
                duplicate_count=2,
                freshness_timestamp="2026-06-24T09:00:00",
                schema_column_count=6,
                timestamp="2026-06-24T11:00:00+00:00",
            ),
        )

    def _seed_second_run_for_demo_default_db(self):
        from agent.metadata_store import (
            ModelMetricRecord,
            ScanRunRecord,
            insert_model_metrics,
            insert_scan_run,
        )

        second_scan_id = insert_scan_run(
            self.db_path,
            ScanRunRecord(
                project_name="relium_demo",
                model_name="fct_customer_lifetime_value",
                risk_level="HIGH",
                safe_to_merge=False,
                report_text="second demo run",
                timestamp="2026-06-24T12:00:00+00:00",
            ),
        )
        insert_model_metrics(
            self.db_path,
            ModelMetricRecord(
                scan_id=second_scan_id,
                project_name="relium_demo",
                model_name="fct_customer_lifetime_value",
                row_count=1,
                null_count=0,
                duplicate_count=0,
                freshness_timestamp="2026-06-24T11:00:00",
                schema_column_count=6,
                timestamp="2026-06-24T12:00:00+00:00",
            ),
        )

    def test_compare_last_run_calculates_drift_read_only(self):
        from agent.metadata_drift import compare_last_run

        self._seed_two_runs()

        result = compare_last_run(
            db_path=self.db_path,
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
        )

        self.assertEqual(result["row_count_change_pct"], -50.0)
        self.assertEqual(result["null_count_change_pct"], 0.0)
        self.assertEqual(result["duplicate_count_change_pct"], 100.0)
        self.assertEqual(result["schema_column_count_change"], 1)
        self.assertTrue(result["freshness_regressed"])
        self.assertEqual(result["drift_level"], "HIGH")
        self.assertIn("Row count change: -50%", result["report_text"])
        self.assertIn("Duplicate count change: +100%", result["report_text"])
        self.assertIn("Metadata Drift: HIGH", result["report_text"])

    def test_to_signal_converts_high_drift_to_high_signal(self):
        from agent.metadata_drift import to_signal
        from agent.signals import Severity, Signal

        result = {
            "row_count_change_pct": 200.0,
            "null_count_change_pct": 0.0,
            "duplicate_count_change_pct": 400.0,
            "schema_column_count_change": 0,
            "freshness_regressed": False,
            "drift_level": "HIGH",
            "report_text": (
                "Row count change: +200%\n"
                "Duplicate count change: +400%\n"
                "\n"
                "Metadata Drift: HIGH"
            ),
        }

        signal = to_signal(result)

        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.component, "metadata_drift")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, 95)
        self.assertEqual(signal.score, -35)
        self.assertIn("Row count change: +200%", signal.reasons)
        self.assertIn("Duplicate count change: +400%", signal.reasons)

    def test_to_signal_converts_low_drift_to_low_signal(self):
        from agent.metadata_drift import to_signal
        from agent.signals import Severity

        result = {
            "row_count_change_pct": 5.0,
            "null_count_change_pct": 0.0,
            "duplicate_count_change_pct": 0.0,
            "schema_column_count_change": 0,
            "freshness_regressed": False,
            "drift_level": "LOW",
            "report_text": "Metadata Drift: LOW",
        }

        signal = to_signal(result)

        self.assertEqual(signal.component, "metadata_drift")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 75)
        self.assertEqual(signal.score, -5)

    def test_no_history_available_is_neutral(self):
        from agent.metadata_drift import to_signal

        signal = to_signal(
            {
                "comparison_status": "unavailable",
                "drift_level": "LOW",
                "report_text": "Metadata drift was not evaluated.",
            }
        )

        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])
        self.assertEqual(signal.metadata["comparison_status"], "unavailable")

    def test_evaluated_no_drift_is_neutral(self):
        from agent.metadata_drift import to_signal

        signal = to_signal(
            {
                "comparison_status": "evaluated",
                "row_count_change_pct": 0.0,
                "null_count_change_pct": 0.0,
                "duplicate_count_change_pct": 0.0,
                "schema_column_count_change": 0,
                "freshness_regressed": False,
                "drift_level": "LOW",
                "report_text": "Metadata Drift: LOW",
            }
        )

        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])

    def test_to_signal_preserves_metadata_fields(self):
        from agent.metadata_drift import to_signal

        result = {
            "row_count_change_pct": -50.0,
            "null_count_change_pct": 25.0,
            "duplicate_count_change_pct": 100.0,
            "schema_column_count_change": 1,
            "freshness_regressed": True,
            "drift_level": "HIGH",
            "report_text": "Metadata Drift: HIGH",
        }

        signal = to_signal(result)

        self.assertEqual(
            signal.metadata,
            {
                "row_count_change_pct": -50.0,
                "null_count_change_pct": 25.0,
                "duplicate_count_change_pct": 100.0,
                "schema_column_count_change": 1,
                "freshness_regressed": True,
            },
        )

    def test_compare_last_run_cli_prints_report(self):
        from agent.cli import cli
        from agent import metadata_drift

        self._seed_two_runs()

        runner = CliRunner()
        result, output = invoke_cli(
            runner,
            cli,
            [
                "compare-last-run",
                "--db",
                str(self.db_path),
                "--project",
                "demo_project",
                "--model",
                "fct_customer_lifetime_value",
            ],
        )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Row count change: -50%", output)
        self.assertIn("Duplicate count change: +100%", output)
        self.assertIn("Metadata Drift: HIGH", output)

    def test_compare_last_run_defaults_to_latest_available_model(self):
        from agent.metadata_drift import compare_last_run

        self._seed_two_runs()

        result = compare_last_run(db_path=self.db_path)

        self.assertEqual(result["project_name"], "demo_project")
        self.assertEqual(result["model_name"], "fct_customer_lifetime_value")

    def test_compare_last_run_cli_uses_explicit_demo_metadata_database(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            metadata_db = workspace / "relium_metadata.db"
            first, first_output = invoke_cli(
                runner,
                cli,
                ["demo-pipeline", "--workspace", str(workspace)],
            )
            self.assertEqual(first.exit_code, 0, first_output)
            self.db_path = metadata_db
            self._seed_second_run_for_demo_default_db()
            result, output = invoke_cli(
                runner,
                cli,
                ["compare-last-run", "--db", str(metadata_db)],
            )

            self.assertEqual(result.exit_code, 0, output)
            self.assertIn("Metadata Drift:", output)

    def test_compare_last_run_cli_accepts_explicit_db(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result, output = invoke_cli(
            runner,
            cli,
            ["compare-last-run", "--db", str(self.db_path)],
        )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Metadata Drift: HIGH", output)

    def test_compare_last_run_cli_accepts_explicit_project(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result, output = invoke_cli(
            runner,
            cli,
            [
                "compare-last-run",
                "--db",
                str(self.db_path),
                "--project",
                "demo_project",
            ],
        )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Metadata Drift: HIGH", output)

    def test_compare_last_run_cli_accepts_explicit_model(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result, output = invoke_cli(
            runner,
            cli,
            [
                "compare-last-run",
                "--db",
                str(self.db_path),
                "--model",
                "fct_customer_lifetime_value",
            ],
        )

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Metadata Drift: HIGH", output)

    def test_demo_pipeline_scenarios_produce_high_drift_without_recording_it(self):
        from agent.cli import cli

        runner = CliRunner()
        expectations = {
            "row-drop": {
                "row_count_change": "-50%",
                "duplicate_count_change": "-100%",
                "freshness_regression": "YES",
            },
            "duplicate-spike": {
                "row_count_change": "+200%",
                "duplicate_count_change": "+400%",
                "freshness_regression": "NO",
            },
            "freshness-regression": {
                "row_count_change": "+0%",
                "duplicate_count_change": "+0%",
                "freshness_regression": "YES",
            },
        }

        for scenario, expected in expectations.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "workspace"
                metadata_db = workspace / "relium_metadata.db"
                first, first_output = invoke_cli(
                    runner,
                    cli,
                    [
                        "demo-pipeline",
                        "--workspace",
                        str(workspace),
                        "--scenario",
                        "normal",
                    ],
                )
                self.assertEqual(first.exit_code, 0, first_output)

                second, second_output = invoke_cli(
                    runner,
                    cli,
                    [
                        "demo-pipeline",
                        "--workspace",
                        str(workspace),
                        "--scenario",
                        scenario,
                    ],
                )
                self.assertEqual(second.exit_code, 0, second_output)

                compare, compare_output = invoke_cli(
                    runner,
                    cli,
                    ["compare-last-run", "--db", str(metadata_db)],
                )

                self.assertEqual(compare.exit_code, 0, compare_output)
                self.assertIn(
                    f"Row count change: {expected['row_count_change']}",
                    compare_output,
                )
                self.assertIn(
                    f"Freshness regression: {expected['freshness_regression']}",
                    compare_output,
                )
                self.assertIn("Metadata Drift: HIGH", compare_output)

                conn = sqlite3.connect(metadata_db)
                drift_rows = conn.execute(
                    "SELECT drift_level, report_text FROM relium_metric_drifts"
                ).fetchall()
                conn.close()

                self.assertEqual(drift_rows, [])


if __name__ == "__main__":
    unittest.main()
