import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class PipelineValidationReportTests(unittest.TestCase):
    def test_report_contains_all_major_sections(self):
        from agent.pipeline_validation_report import format_pipeline_validation_report

        report = format_pipeline_validation_report(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "stg_orders",
                "scan_id": "scan-123",
                "severity": "HIGH",
                "safe_to_continue": False,
                "static_analysis_text": "Potential LEFT JOIN nullification detected.",
                "affected_models": ["orders", "order_items", "customers"],
                "recommendation": "Review the SQL transformation before deployment.",
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
                "drift_result": {
                    "previous_run_timestamp": "2026-06-24T10:00:00+00:00",
                    "current_run_timestamp": "2026-06-24T12:00:00+00:00",
                    "row_count_change_pct": 200.0,
                    "null_count_change_pct": 0.0,
                    "duplicate_count_change_pct": 400.0,
                    "schema_column_count_change": 0,
                    "freshness_regressed": False,
                    "drift_level": "HIGH",
                },
            }
        )

        self.assertIn("# Relium Pipeline Validation Report", report)
        self.assertIn("## Executive Summary", report)
        self.assertIn("## Static Analysis", report)
        self.assertIn("## Metadata Validation", report)
        self.assertIn("## Historical Drift", report)
        self.assertIn("## Final Decision", report)
        self.assertIn("## Recommended Actions", report)

    def test_high_drift_report_has_investor_grade_summary(self):
        from agent.pipeline_validation_report import format_pipeline_validation_report

        report = format_pipeline_validation_report(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "stg_orders",
                "severity": "HIGH",
                "safe_to_continue": False,
                "affected_models": ["orders", "order_items", "customers"],
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
                "drift_result": {
                    "previous_run_timestamp": "2026-06-24T10:00:00+00:00",
                    "current_run_timestamp": "2026-06-24T12:00:00+00:00",
                    "row_count_change_pct": 200.0,
                    "null_count_change_pct": 0.0,
                    "duplicate_count_change_pct": 400.0,
                    "schema_column_count_change": 0,
                    "freshness_regressed": False,
                    "drift_level": "HIGH",
                },
            }
        )

        self.assertIn("Decision: ❌ BLOCK DEPLOYMENT", report)
        self.assertIn("Risk Level: HIGH", report)
        self.assertIn("Changed Model:\nstg_orders", report)
        self.assertIn("Downstream Impact:\norders, order_items, customers", report)
        self.assertIn("Metadata Drift:\nHIGH", report)
        self.assertIn(
            "Primary Reason:\nDuplicate customer_id count increased by +400%.",
            report,
        )
        self.assertIn("Duplicate count change: +400%", report)

    def test_no_history_report_renders_clear_drift_message(self):
        from agent.pipeline_validation_report import format_pipeline_validation_report

        report = format_pipeline_validation_report(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "fct_customer_lifetime_value",
                "severity": "HIGH",
                "safe_to_continue": False,
                "row_count": 2,
                "null_count": 0,
                "duplicate_count": 1,
                "freshness_timestamp": "2026-06-21T12:00:00",
                "schema_column_count": 6,
                "drift_result": None,
            }
        )

        self.assertIn("Metadata Drift:\nNot enough historical runs yet.", report)
        self.assertIn("Not enough historical runs yet.", report)
        self.assertIn("SAFE TO MERGE:\nNO", report)

    def test_demo_pipeline_command_writes_pipeline_validation_report(self):
        from agent import demo_pipeline
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata_db = tmp_path / "relium_metadata.db"
            warehouse_db = tmp_path / "demo_warehouse.db"

            with runner.isolated_filesystem(temp_dir=tmp), patch.object(
                demo_pipeline, "DEFAULT_METADATA_DB_PATH", metadata_db
            ), patch.object(
                demo_pipeline, "DEFAULT_WAREHOUSE_DB_PATH", warehouse_db
            ), patch(
                "agent.slack_alerts.send_validation_alert",
                return_value=False,
            ):
                result = runner.invoke(
                    cli,
                    ["demo-pipeline", "--scenario", "normal"],
                )
                report_path = Path("pipeline_validation_report.md")
                report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("# Relium Pipeline Validation Report", report_text)
        self.assertIn("## Executive Summary", report_text)
        self.assertIn("Project: relium_demo", report_text)
        self.assertIn("Model: fct_customer_lifetime_value", report_text)


if __name__ == "__main__":
    unittest.main()
