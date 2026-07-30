import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from agent.decision_engine import DeploymentDecision
from agent.incident import Incident
from agent.signals import Severity, Signal


def make_report_incident(affected_models=None):
    return Incident(
        incident_id="INC-0042",
        health=60,
        decision=DeploymentDecision.BLOCK,
        severity=Severity.HIGH,
        confidence=95,
        root_cause="LEFT JOIN nullification detected",
        recommendation="Review the flagged pipeline signals before deployment.",
        affected_models=list(affected_models or []),
        signals=[
            Signal(
                component="ast",
                severity=Severity.HIGH,
                confidence=95,
                score=-40,
                reasons=["LEFT JOIN nullification detected"],
            ),
            Signal(
                component="metadata_checks",
                severity=Severity.HIGH,
                confidence=95,
                score=-30,
                reasons=["Duplicate count increased"],
            ),
        ],
    )


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
                "incident": make_report_incident(
                    ["orders", "order_items", "customers"]
                ),
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
        self.assertIn("# Relium Deployment Decision", report)
        self.assertIn("## Recommended Actions", report)

    def test_markdown_report_contains_rendered_incident_section(self):
        from agent.pipeline_validation_report import format_pipeline_validation_report

        report = format_pipeline_validation_report(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "stg_orders",
                "severity": "HIGH",
                "safe_to_continue": False,
                "affected_models": ["orders"],
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
                "drift_result": None,
                "incident": make_report_incident(["orders"]),
            }
        )

        self.assertIn("# Relium Deployment Decision", report)
        self.assertIn("## Pipeline Health\n60 / 100", report)
        self.assertIn("## Deployment Decision\nBLOCK", report)
        self.assertIn("## Severity\nHIGH", report)
        self.assertIn("## Confidence\n95%", report)
        self.assertIn("## Primary Root Cause", report)
        self.assertIn("LEFT JOIN nullification detected", report)
        self.assertIn("## Recommendation", report)
        self.assertIn("## Signals Considered", report)
        self.assertIn("- ast", report)
        self.assertIn("- metadata_checks", report)
        self.assertIn("## Affected Models", report)
        self.assertIn("- orders", report)
        self.assertIn("## Top Reasons", report)

    def test_report_preserves_non_decision_sections_with_incident(self):
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
                "affected_models": ["orders", "customers"],
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
                "incident": make_report_incident(["orders", "customers"]),
            }
        )

        self.assertIn("# Relium Pipeline Validation Report", report)
        self.assertIn("## Executive Summary", report)
        self.assertIn("Project: relium_demo", report)
        self.assertIn("Model: stg_orders", report)
        self.assertIn("Run ID: scan-123", report)
        self.assertIn("## Static Analysis", report)
        self.assertIn(
            "- Potential LEFT JOIN nullification detected.",
            report,
        )
        self.assertIn("## Metadata Validation", report)
        self.assertIn("Row count: 6", report)
        self.assertIn("Duplicate count: 5", report)
        self.assertIn("## Historical Drift", report)
        self.assertIn("Duplicate count change: +400%", report)
        self.assertIn("## Recommended Actions", report)

    def test_markdown_report_handles_empty_incident_affected_models(self):
        from agent.pipeline_validation_report import format_pipeline_validation_report

        report = format_pipeline_validation_report(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "stg_orders",
                "severity": "HIGH",
                "safe_to_continue": False,
                "affected_models": [],
                "row_count": 6,
                "null_count": 0,
                "duplicate_count": 5,
                "freshness_timestamp": "2026-06-24T12:00:00",
                "schema_column_count": 6,
                "drift_result": None,
                "incident": make_report_incident(),
            }
        )

        self.assertIn("# Relium Deployment Decision", report)
        self.assertIn("## Signals Considered", report)
        self.assertNotIn("## Affected Models", report)

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
                "incident": make_report_incident(
                    ["orders", "order_items", "customers"]
                ),
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
                "incident": make_report_incident(),
            }
        )

        self.assertIn("Metadata Drift:\nNot enough historical runs yet.", report)
        self.assertIn("Not enough historical runs yet.", report)
        self.assertIn("# Relium Deployment Decision", report)

    def test_demo_pipeline_command_writes_pipeline_validation_report(self):
        from agent.cli import cli

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"

            with runner.isolated_filesystem(temp_dir=tmp):
                result = runner.invoke(
                    cli,
                    [
                        "demo-pipeline",
                        "--workspace",
                        str(workspace),
                        "--scenario",
                        "normal",
                    ],
                )
                report_path = workspace / "pipeline_validation_report.md"
                report_text = report_path.read_text(encoding="utf-8")
                json_path = workspace / "pipeline_validation_report.json"
                report_json = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("# Relium Pipeline Validation Report", report_text)
        self.assertIn("## Executive Summary", report_text)
        self.assertIn("Project: relium_demo", report_text)
        self.assertIn("Model: fct_customer_lifetime_value", report_text)
        self.assertEqual(report_json["project"], "relium_demo")
        self.assertEqual(report_json["model"], "fct_customer_lifetime_value")
        self.assertEqual(report_json["scenario"], "normal")

    def test_json_report_contains_metadata_metrics(self):
        from agent.pipeline_validation_report import format_pipeline_validation_json

        report = format_pipeline_validation_json(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "fct_customer_lifetime_value",
                "scenario": "normal",
                "scan_id": "run-123",
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

        self.assertEqual(report["run_id"], "run-123")
        self.assertEqual(report["metadata_checks"]["row_count"], 2)
        self.assertEqual(report["metadata_checks"]["null_count"], 0)
        self.assertEqual(report["metadata_checks"]["duplicate_count"], 1)
        self.assertEqual(
            report["metadata_checks"]["freshness_timestamp"],
            "2026-06-21T12:00:00",
        )
        self.assertEqual(report["metadata_checks"]["schema_column_count"], 6)

    def test_json_report_contains_drift_fields_when_available(self):
        from agent.pipeline_validation_report import format_pipeline_validation_json

        report = format_pipeline_validation_json(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "fct_customer_lifetime_value",
                "scenario": "duplicate-spike",
                "scan_id": "run-456",
                "severity": "HIGH",
                "safe_to_continue": False,
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

        drift = report["drift_detection"]
        self.assertEqual(drift["status"], "available")
        self.assertEqual(drift["row_count_change_pct"], 200.0)
        self.assertEqual(drift["duplicate_count_change_pct"], 400.0)
        self.assertEqual(drift["overall_metadata_drift"], "HIGH")

    def test_json_report_says_not_enough_history_when_drift_unavailable(self):
        from agent.pipeline_validation_report import format_pipeline_validation_json

        report = format_pipeline_validation_json(
            {
                "generated_timestamp": "2026-06-26 14:32 UTC",
                "project_name": "relium_demo",
                "model_name": "fct_customer_lifetime_value",
                "scenario": "normal",
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

        self.assertEqual(report["drift_detection"]["status"], "not_enough_history")
        self.assertIsNone(report["drift_detection"]["overall_metadata_drift"])


if __name__ == "__main__":
    unittest.main()
