import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class IncidentReporterTests(unittest.TestCase):
    def test_create_incident_report_writes_markdown_with_rca_details(self):
        from agent.incident_reporter import create_incident_report

        anomaly = {
            "type": "row_count_anomaly",
            "table": "raw_orders",
            "severity": "critical",
            "message": "Row count dropped by 96.0%",
            "detail": "Expected ~200 rows, got 8",
            "impact": "Possible data loss or duplication in pipeline",
        }
        rca_report = {
            "likely_causes": [
                {
                    "cause": "upstream ingestion failure",
                    "confidence": 0.95,
                    "reason": "row count dropped by 96%",
                },
                {
                    "cause": "accidental filter introduction",
                    "confidence": 0.85,
                    "reason": "a restrictive filter can remove records before downstream models run",
                },
                {
                    "cause": "source table truncation",
                    "confidence": 0.80,
                    "reason": "large row-count drops can indicate partial loads or truncation",
                },
                {
                    "cause": "join removing records",
                    "confidence": 0.70,
                    "reason": "downstream joins may remove unmatched rows",
                },
            ],
            "affected_models": [
                "fct_customer_lifetime_value",
                "fct_revenue",
                "fct_customer_summary",
            ],
            "impact_count": 3,
            "recommended_actions": [
                "Check upstream ingestion job for raw_orders",
                "Compare latest row count with previous successful run",
                "Review recent WHERE clause/filter changes",
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent.incident_reporter.INCIDENTS_DIR",
            Path(tmpdir) / "incidents",
        ):
            path = create_incident_report("test_project", anomaly, rca_report)
            report_path = Path(path)
            content = report_path.read_text()

        self.assertTrue(report_path.name.startswith("test_project_raw_orders_row_count_anomaly_"))
        self.assertEqual(report_path.suffix, ".md")
        self.assertNotIn("\\", path)
        self.assertIn("# Relium Incident Report", content)
        self.assertIn("## Incident Summary", content)
        self.assertIn("Project: test_project", content)
        self.assertIn("Table: raw_orders", content)
        self.assertIn("Anomaly Type: row_count_anomaly", content)
        self.assertIn("Severity: critical", content)
        self.assertIn("Data Loss Risk: yes", content)
        self.assertIn("Generated At:", content)
        self.assertIn("## Executive Summary", content)
        self.assertIn("Relium detected a critical row-count anomaly in raw_orders.", content)
        self.assertIn("raw_orders dropped from ~200 expected rows to 8 observed rows, a 96.0% decrease.", content)
        self.assertIn("Primary hypothesis: upstream ingestion failure.", content)
        self.assertIn("3 downstream model(s) may be affected.", content)
        self.assertIn("Expected rows: ~200", content)
        self.assertIn("Observed rows: 8", content)
        self.assertIn("Change: -96.0%", content)
        self.assertIn("Anomaly message:\nRow count dropped by 96.0%", content)
        self.assertIn("Primary hypothesis:\nUpstream ingestion failure", content)
        self.assertIn("Confidence:\n0.95", content)
        self.assertIn("Status:\nPrimary hypothesis based on metadata evidence. Not yet confirmed by source system logs.", content)
        self.assertIn("Since this table is a raw/source-level dependency", content)
        self.assertIn("1. Accidental filter introduction", content)
        self.assertIn("Reason: A restrictive filter can remove records before downstream models run.", content)
        self.assertIn("Total affected models: 3", content)
        self.assertIn("- fct_customer_lifetime_value", content)
        self.assertIn("- fct_customer_summary", content)
        self.assertIn("These models either directly or indirectly depend on raw_orders.", content)
        self.assertIn("## Recommended Investigation Steps", content)
        self.assertIn("1. Check the upstream ingestion job for raw_orders.", content)
        self.assertIn("5. Inspect downstream joins only if raw_orders appears healthy.", content)
        self.assertIn("## Suggested Owner Action", content)
        self.assertIn(
            "First action: Verify whether the upstream ingestion job for raw_orders completed successfully and loaded the expected number of rows.",
            content,
        )
        self.assertIn(
            "Investigation priority: Start at raw_orders before debugging downstream models, because the affected models appear to inherit the anomaly from the raw table layer.",
            content,
        )
        self.assertIn("This report was generated using metadata only.", content)
        self.assertIn("Relium did not access customer records", content)
        self.assertIn("- row counts", content)
        self.assertIn("- dependency graph", content)

    def test_freshness_incident_report_uses_freshness_specific_actions(self):
        from agent.incident_reporter import create_incident_report

        anomaly = {
            "type": "freshness_anomaly",
            "table": "raw_customers",
            "severity": "critical",
            "message": "Table is stale by 8.0 hours",
            "detail": "Latest updated_at value is 2026-06-04 02:00:00",
            "impact": "Downstream models may be using outdated data",
        }
        rca_report = {
            "likely_causes": [
                {
                    "cause": "upstream ingestion delay",
                    "confidence": 0.85,
                    "reason": "Table is stale by 8.0 hours",
                }
            ],
            "affected_models": [
                "fct_customer_lifetime_value",
                "fct_daily_kpis",
                "dashboard_executive_metrics",
            ],
            "impact_count": 3,
            "recommended_actions": [
                "Check whether the scheduled ingestion job for raw_customers ran successfully",
                "Verify the latest source sync timestamp",
                "Check whether the source connector is paused or delayed",
                "Review orchestration logs for failed, skipped, or delayed jobs",
                "Confirm whether the source system is producing new records",
                "Validate the expected freshness SLA for this table",
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent.incident_reporter.INCIDENTS_DIR",
            Path(tmpdir) / "incidents",
        ):
            path = create_incident_report("business_demo", anomaly, rca_report)
            content = Path(path).read_text()

        self.assertIn(
            "If raw_customers is stale, downstream models may be using outdated data.",
            content,
        )
        self.assertIn(
            "1. Check whether the scheduled ingestion job for raw_customers ran successfully.",
            content,
        )
        self.assertIn("2. Verify the latest source sync timestamp.", content)
        self.assertIn("6. Validate the expected freshness SLA for this table.", content)
        self.assertIn(
            (
                "First action: Verify whether the scheduled ingestion job for raw_customers "
                "completed successfully and updated the table within the expected freshness window."
            ),
            content,
        )
        self.assertIn(
            (
                "Investigation priority: Start with ingestion schedule, source connector status, "
                "and orchestration logs before debugging downstream models."
            ),
            content,
        )
        self.assertNotIn("Compare the latest raw_customers row count", content)
        self.assertNotIn("WHERE clause", content)


if __name__ == "__main__":
    unittest.main()
