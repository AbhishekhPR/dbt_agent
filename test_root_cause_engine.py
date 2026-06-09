import sqlite3
import unittest
from click.testing import CliRunner
from unittest.mock import patch


def _insert_metric(conn, table, row_count, duplicate_rows=0, metrics=None, ts=None):
    conn.execute(
        """
        INSERT INTO table_metrics
        (timestamp, project_name, table_name, row_count, duplicate_rows, metrics_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            ts or "2026-06-01 00:00:00",
            "test_project",
            table,
            row_count,
            duplicate_rows,
            metrics or "{}",
        ),
    )


class RootCauseEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = __import__("tempfile").TemporaryDirectory()
        self.db_path = __import__("pathlib").Path(self.tmpdir.name) / "metadata_history.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_row_count_drop_message_wins_and_does_not_return_duplicate_ingestion_first(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        conn = sqlite3.connect(self.db_path)
        metrics_store._init_table_metrics_history(conn)
        _insert_metric(conn, "raw_orders", 1000, ts="2026-06-01 00:00:00")
        _insert_metric(conn, "raw_orders", 1000, ts="2026-06-02 00:00:00")
        conn.commit()
        conn.close()

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={
                "directly_affected": [{"model": "fct_revenue"}],
                "indirectly_affected": [{"model": "fct_customer_summary"}],
                "total_affected": 2,
            },
        ):
            report = analyze_root_cause(
                {
                    "type": "row_count_anomaly",
                    "table": "raw_orders",
                    "project": "test_project",
                    "project_path": "unused",
                    "message": "Row count dropped by 95%",
                }
            )

        self.assertEqual(
            report["likely_causes"][0],
            {
                "cause": "upstream ingestion failure",
                "confidence": 0.95,
                "reason": "Row count dropped by 95%",
            },
        )
        self.assertEqual(report["direction"], "dropped")
        self.assertEqual(report["change_pct"], 95)
        self.assertNotEqual(report["likely_causes"][0]["cause"], "duplicate ingestion")
        self.assertEqual(report["likely_causes"][1]["cause"], "accidental filter introduction")
        self.assertEqual(report["affected_models"], ["fct_revenue", "fct_customer_summary"])
        self.assertEqual(report["impact_count"], 2)
        self.assertIn("Check upstream ingestion job for raw_orders", report["recommended_actions"])

    def test_row_count_spike_message_returns_duplicate_ingestion(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={"directly_affected": [], "indirectly_affected": [], "total_affected": 0},
        ):
            report = analyze_root_cause(
                {
                    "type": "row_count_anomaly",
                    "table": "raw_orders",
                    "project_path": "unused",
                    "message": "Row count spiked by 95%",
                }
            )

        self.assertEqual(report["direction"], "spiked")
        self.assertEqual(report["change_pct"], 95)
        self.assertEqual(report["likely_causes"][0]["cause"], "duplicate ingestion")

    def test_row_count_no_change_history_returns_low_confidence_no_evidence(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        conn = sqlite3.connect(self.db_path)
        metrics_store._init_table_metrics_history(conn)
        _insert_metric(conn, "raw_orders", 1000, ts="2026-06-01 00:00:00")
        _insert_metric(conn, "raw_orders", 1000, ts="2026-06-02 00:00:00")
        conn.commit()
        conn.close()

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={"directly_affected": [], "indirectly_affected": [], "total_affected": 0},
        ):
            report = analyze_root_cause(
                {
                    "type": "row_count_anomaly",
                    "table": "raw_orders",
                    "project_path": "unused",
                    "message": "Row count anomaly detected",
                }
            )

        self.assertEqual(report["direction"], "unknown")
        self.assertEqual(report["change_pct"], 0)
        self.assertEqual(report["likely_causes"][0]["cause"], "no strong RCA evidence")
        self.assertLessEqual(report["likely_causes"][0]["confidence"], 0.25)
        self.assertNotEqual(report["likely_causes"][0]["cause"], "duplicate ingestion")

    def test_blast_radius_integration_includes_direct_and_indirect_models(self):
        from agent.root_cause_engine import analyze_root_cause

        report = analyze_root_cause(
            {
                "type": "row_count_anomaly",
                "table": "raw_orders",
                "project_path": "test_project",
                "message": "Row count dropped by 95%",
            }
        )

        self.assertEqual(
            report["affected_models"],
            [
                "fct_customer_lifetime_value",
                "fct_revenue",
                "fct_customer_summary",
            ],
        )
        self.assertEqual(report["impact_count"], 3)

    def test_null_explosion_uses_metadata_json_for_column_reason(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        conn = sqlite3.connect(self.db_path)
        metrics_store._init_table_metrics_history(conn)
        _insert_metric(
            conn,
            "raw_orders",
            1000,
            metrics='{"null_rates": {"customer_id": 1.0}}',
            ts="2026-06-01 00:00:00",
        )
        _insert_metric(
            conn,
            "raw_orders",
            1000,
            metrics='{"null_rates": {"customer_id": 46.0}}',
            ts="2026-06-02 00:00:00",
        )
        conn.commit()
        conn.close()

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={"directly_affected": [], "indirectly_affected": [], "total_affected": 0},
        ):
            report = analyze_root_cause(
                {
                    "type": "null_explosion",
                    "table": "raw_orders",
                    "project_path": "unused",
                    "message": "Null rate on customer_id jumped by 45%",
                }
            )

        self.assertEqual(report["likely_causes"][0]["cause"], "source column missing")
        self.assertEqual(report["likely_causes"][0]["confidence"], 0.9)
        self.assertIn(
            "Null rate on customer_id jumped by 45%",
            report["likely_causes"][0]["reason"],
        )
        self.assertIn("Validate join keys", report["recommended_actions"])

    def test_duplicate_explosion_message_drives_causes_without_history(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={"directly_affected": [], "indirectly_affected": [], "total_affected": 0},
        ):
            report = analyze_root_cause(
                {
                    "type": "duplicate_explosion",
                    "table": "raw_orders",
                    "project_path": "unused",
                    "message": "Duplicate rows increased from 0 to 5",
                }
            )

        self.assertEqual(
            [item["cause"] for item in report["likely_causes"]],
            [
                "duplicate ingestion",
                "bad join",
                "missing deduplication",
                "retry/replay of source load",
            ],
        )
        self.assertEqual(
            report["likely_causes"][0]["reason"],
            "Duplicate rows increased from 0 to 5",
        )
        self.assertIn("Inspect recent joins for fan-out", report["recommended_actions"])

    def test_duplicate_explosion_message_wins_over_metric_history(self):
        from agent import metrics_store
        from agent.root_cause_engine import analyze_root_cause

        conn = sqlite3.connect(self.db_path)
        metrics_store._init_table_metrics_history(conn)
        _insert_metric(conn, "raw_orders", 8, duplicate_rows=0, ts="2026-06-01 00:00:00")
        _insert_metric(conn, "raw_orders", 13, duplicate_rows=5, ts="2026-06-02 00:00:00")
        conn.commit()
        conn.close()

        with patch.object(metrics_store, "METADATA_HISTORY_DB", self.db_path), patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={"directly_affected": [], "indirectly_affected": [], "total_affected": 0},
        ):
            report = analyze_root_cause(
                {
                    "type": "duplicate_explosion",
                    "table": "raw_orders",
                    "project_path": "unused",
                    "message": "Duplicate rows increased from 0 to 10",
                }
            )

        self.assertTrue(report["likely_causes"])
        for cause in report["likely_causes"]:
            self.assertEqual(cause["reason"], "Duplicate rows increased from 0 to 10")

    def test_freshness_anomaly_returns_metadata_only_rca(self):
        from agent.root_cause_engine import analyze_root_cause

        with patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={
                "directly_affected": [{"model": "fct_revenue"}],
                "indirectly_affected": [{"model": "fct_customer_summary"}],
                "total_affected": 2,
            },
        ):
            report = analyze_root_cause(
                {
                    "type": "freshness_anomaly",
                    "table": "raw_orders",
                    "project_path": "test_project",
                    "message": "Table is stale by 8.0 hours",
                    "detail": "Latest created_at value is 2026-06-04 02:00:00",
                }
            )

        self.assertEqual(report["likely_causes"][0]["cause"], "upstream ingestion delay")
        self.assertEqual(
            report["likely_causes"][0]["reason"],
            "Table is stale by 8.0 hours",
        )
        self.assertIn("failed scheduled load", [c["cause"] for c in report["likely_causes"]])
        self.assertEqual(report["affected_models"], ["fct_revenue", "fct_customer_summary"])
        self.assertIn(
            "Check whether the scheduled ingestion job for raw_orders ran successfully",
            report["recommended_actions"],
        )
        self.assertIn(
            "Validate the expected freshness SLA for this table",
            report["recommended_actions"],
        )

    def test_schema_drift_uses_column_level_blast_radius(self):
        from agent.root_cause_engine import analyze_root_cause

        with patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={
                "directly_affected": [{"model": "fct_customer_lifetime_value"}],
                "indirectly_affected": [],
                "total_affected": 1,
            },
        ) as blast:
            report = analyze_root_cause(
                {
                    "type": "schema_drift",
                    "table": "raw_customers",
                    "project_path": "test_project",
                    "message": "Schema drift detected: column 'customer_segment' was removed",
                    "schema_change": {
                        "change_type": "removed_column",
                        "column": "customer_segment",
                    },
                }
            )

        blast.assert_called_once_with(
            "test_project",
            "raw_customers",
            changed_columns=["customer_segment"],
        )
        self.assertEqual(report["likely_causes"][0]["cause"], "upstream schema change")
        self.assertEqual(
            report["likely_causes"][0]["reason"],
            "Schema drift detected: column 'customer_segment' was removed",
        )
        self.assertEqual(report["affected_models"], ["fct_customer_lifetime_value"])
        self.assertIn("Check recent upstream schema changes", report["recommended_actions"])

    def test_root_cause_cli_accepts_message_and_prints_dropped_report(self):
        from agent.cli import cli

        runner = CliRunner()
        with patch(
            "agent.root_cause_engine.calculate_blast_radius",
            return_value={
                "directly_affected": [
                    {"model": "fct_customer_lifetime_value"},
                    {"model": "fct_revenue"},
                ],
                "indirectly_affected": [{"model": "fct_customer_summary"}],
                "total_affected": 3,
            },
        ):
            result = runner.invoke(
                cli,
                [
                    "root_cause",
                    "--project",
                    "test_project",
                    "--table",
                    "raw_orders",
                    "--anomaly",
                    "row_count_anomaly",
                    "--message",
                    "Row count dropped by 95%",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Direction:\n", result.output)
        self.assertIn("dropped", result.output)
        self.assertIn("Change:\n95.0%", result.output)
        self.assertIn("1. upstream ingestion failure", result.output)
        self.assertIn("Confidence: 0.95", result.output)
        self.assertIn("Reason: Row count dropped by 95%", result.output)
        self.assertIn("- fct_customer_lifetime_value", result.output)
        self.assertIn("- fct_revenue", result.output)
        self.assertIn("- fct_customer_summary", result.output)


if __name__ == "__main__":
    unittest.main()
