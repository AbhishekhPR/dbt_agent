import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


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

    def test_compare_last_run_calculates_drift_and_stores_it(self):
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

    def test_compare_last_run_cli_prints_report(self):
        from agent.cli import cli
        from agent import metadata_drift

        self._seed_two_runs()

        runner = CliRunner()
        result = runner.invoke(
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

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Row count change: -50%", result.output)
        self.assertIn("Duplicate count change: +100%", result.output)
        self.assertIn("Metadata Drift: HIGH", result.output)

    def test_compare_last_run_defaults_to_latest_available_model(self):
        from agent.metadata_drift import compare_last_run

        self._seed_two_runs()

        result = compare_last_run(db_path=self.db_path)

        self.assertEqual(result["project_name"], "demo_project")
        self.assertEqual(result["model_name"], "fct_customer_lifetime_value")

    def test_compare_last_run_cli_succeeds_with_no_arguments_after_demo_pipeline(self):
        from agent import demo_pipeline
        from agent import metadata_drift
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
            ), patch.object(
                metadata_drift, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                first = runner.invoke(cli, ["demo-pipeline"])
                self.assertEqual(first.exit_code, 0, first.output)
                self.db_path = metadata_db
                self._seed_second_run_for_demo_default_db()
                result = runner.invoke(cli, ["compare-last-run"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Metadata Drift:", result.output)

    def test_compare_last_run_cli_accepts_explicit_db(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["compare-last-run", "--db", str(self.db_path)],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Metadata Drift: HIGH", result.output)

    def test_compare_last_run_cli_accepts_explicit_project(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "compare-last-run",
                "--db",
                str(self.db_path),
                "--project",
                "demo_project",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Metadata Drift: HIGH", result.output)

    def test_compare_last_run_cli_accepts_explicit_model(self):
        from agent.cli import cli

        self._seed_two_runs()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "compare-last-run",
                "--db",
                str(self.db_path),
                "--model",
                "fct_customer_lifetime_value",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Metadata Drift: HIGH", result.output)

    def test_demo_pipeline_scenarios_produce_high_drift_and_store_records(self):
        from agent import demo_pipeline
        from agent import metadata_drift
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
                tmp_path = Path(tmp)
                metadata_db = tmp_path / "relium_metadata.db"
                warehouse_db = tmp_path / "demo_warehouse.db"

                with patch.object(
                    demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
                ), patch.object(
                    demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
                ), patch.object(
                    metadata_drift, "DEFAULT_METADATA_DB_PATH", metadata_db
                ), patch(
                    "agent.slack_alerts.send_validation_alert",
                    return_value=False,
                ):
                    first = runner.invoke(
                        cli,
                        ["demo-pipeline", "--scenario", "normal"],
                    )
                    self.assertEqual(first.exit_code, 0, first.output)

                    second = runner.invoke(
                        cli,
                        ["demo-pipeline", "--scenario", scenario],
                    )
                    self.assertEqual(second.exit_code, 0, second.output)

                    compare = runner.invoke(cli, ["compare-last-run"])

                self.assertEqual(compare.exit_code, 0, compare.output)
                self.assertIn(
                    f"Row count change: {expected['row_count_change']}",
                    compare.output,
                )
                self.assertIn(
                    f"Freshness regression: {expected['freshness_regression']}",
                    compare.output,
                )
                self.assertIn("Metadata Drift: HIGH", compare.output)

                conn = sqlite3.connect(metadata_db)
                drift_rows = conn.execute(
                    "SELECT drift_level, report_text FROM relium_metric_drifts"
                ).fetchall()
                conn.close()

                self.assertEqual(len(drift_rows), 1)
                self.assertEqual(drift_rows[0][0], "HIGH")
                self.assertIn(
                    f"Row count change: {expected['row_count_change']}",
                    drift_rows[0][1],
                )


if __name__ == "__main__":
    unittest.main()
