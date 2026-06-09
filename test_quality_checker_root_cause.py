import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


class QualityCheckerRootCauseTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.db_path = self.tmp_path / "warehouse.db"
        self.baseline_path = self.tmp_path / "baselines"
        self.baseline_path.mkdir()

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE raw_orders (order_id INTEGER)")
        conn.executemany(
            "INSERT INTO raw_orders (order_id) VALUES (?)",
            [(idx,) for idx in range(50)],
        )
        conn.commit()
        conn.close()

        baseline = {
            "table": "raw_orders",
            "row_count": 1000,
            "columns": ["order_id"],
            "null_rates": {"order_id": 0.0},
            "duplicate_rows": 0,
            "distinct_counts": {"order_id": 1000},
        }
        (self.baseline_path / "raw_orders.json").write_text(json.dumps(baseline))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_quality_check_uses_deterministic_rca_for_printing_and_slack(self):
        from agent import quality_checker

        rca_report = {
            "likely_causes": [
                {
                    "cause": "upstream ingestion failure",
                    "confidence": 0.95,
                    "reason": "row count dropped by 95%",
                }
            ],
            "affected_models": ["fct_revenue", "fct_customer_summary"],
            "recommended_actions": [
                "Check upstream ingestion job for raw_orders",
            ],
            "impact_count": 2,
        }

        self.assertNotIn("agent.optional_ai_explainer", sys.modules)

        with patch.object(quality_checker, "BASELINE_PATH", self.baseline_path), patch(
            "agent.quality_checker.analyze_root_cause",
            return_value=rca_report,
        ) as analyze, patch(
            "agent.quality_checker.record_table_metrics",
        ), patch(
            "agent.quality_checker.create_incident_report",
            return_value="incidents/test_project_raw_orders_row_count_anomaly_20260603_143200.md",
        ) as create_report, patch(
            "agent.quality_checker.send_slack_alert",
        ) as send_slack, patch(
            "builtins.print",
        ) as printed:
            quality_checker.run_quality_check("test_project", str(self.db_path))

        analyze.assert_called_once()
        analyzed_anomaly = analyze.call_args.args[0]
        self.assertEqual(analyzed_anomaly["type"], "row_count")
        self.assertEqual(analyzed_anomaly["table"], "raw_orders")
        self.assertEqual(analyzed_anomaly["project_path"], "test_project")
        self.assertEqual(analyzed_anomaly["message"], "Row count dropped by 95.0%")

        create_report.assert_called_once()
        reported_anomaly = create_report.call_args.args[1]
        self.assertIn("root_cause_analysis", reported_anomaly)
        self.assertEqual(
            reported_anomaly["incident_report_path"],
            "incidents/test_project_raw_orders_row_count_anomaly_20260603_143200.md",
        )

        send_slack.assert_called_once()
        diagnosis = send_slack.call_args.args[1]
        self.assertEqual(diagnosis["root_cause"], "upstream ingestion failure")
        self.assertEqual(diagnosis["affected_file"], "raw_orders")
        self.assertEqual(diagnosis["affected_line"], "row_count")
        self.assertIn("Anomaly: Row count dropped by 95.0%", diagnosis["explanation"])
        self.assertIn("Evidence: Expected ~1000 rows, observed 50 rows.", diagnosis["explanation"])
        self.assertEqual(diagnosis["impact_count"], 2)
        self.assertEqual(diagnosis["affected_models"], ["fct_revenue", "fct_customer_summary"])
        self.assertEqual(diagnosis["suggested_fix"], "Check upstream ingestion job for raw_orders")
        self.assertEqual(
            diagnosis["incident_report"],
            "incidents/test_project_raw_orders_row_count_anomaly_20260603_143200.md",
        )
        self.assertNotIn("agent.optional_ai_explainer", sys.modules)

        output = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("Root Cause Analysis", output)
        self.assertIn("upstream ingestion failure", output)
        self.assertIn("fct_revenue", output)
        self.assertIn("Check upstream ingestion job for raw_orders", output)

    def test_get_table_metrics_counts_duplicate_business_keys_without_row_values(self):
        from agent.quality_checker import get_table_metrics

        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM raw_orders")
        conn.executemany(
            "INSERT INTO raw_orders (order_id) VALUES (?)",
            [(1,), (2,), (2,), (3,), (3,), (3,)],
        )
        conn.commit()
        conn.close()

        metrics = get_table_metrics(str(self.db_path), "raw_orders")

        self.assertEqual(metrics["duplicate_rows"], 3)
        self.assertEqual(metrics["duplicate_rate"], 50.0)
        self.assertEqual(metrics["duplicate_check_method"], "key:order_id")
        self.assertEqual(metrics["duplicate_key_columns"], ["order_id"])
        self.assertNotIn("duplicate_values", metrics)
        self.assertNotIn("duplicate_records", metrics)

    def test_get_table_metrics_falls_back_to_full_row_duplicates(self):
        from agent.quality_checker import get_table_metrics

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE raw_events (event_name TEXT, amount INTEGER)")
        conn.executemany(
            "INSERT INTO raw_events (event_name, amount) VALUES (?, ?)",
            [
                ("created", 10),
                ("created", 10),
                ("updated", 20),
                ("updated", 20),
                ("updated", 20),
            ],
        )
        conn.commit()
        conn.close()

        metrics = get_table_metrics(str(self.db_path), "raw_events")

        self.assertEqual(metrics["duplicate_rows"], 3)
        self.assertEqual(metrics["duplicate_rate"], 60.0)
        self.assertEqual(metrics["duplicate_check_method"], "full_row")
        self.assertEqual(metrics["duplicate_key_columns"], ["event_name", "amount"])

    def test_detect_anomalies_flags_small_duplicate_rate_spikes(self):
        from agent.quality_checker import detect_anomalies

        anomalies = detect_anomalies(
            {
                "table": "raw_orders",
                "row_count": 13,
                "duplicate_rows": 5,
                "duplicate_rate": 38.46,
                "duplicate_check_method": "key:order_id",
                "duplicate_key_columns": ["order_id"],
                "null_rates": {},
                "distinct_counts": {},
            },
            {
                "table": "raw_orders",
                "row_count": 13,
                "duplicate_rows": 0,
                "duplicate_rate": 0,
                "null_rates": {},
                "distinct_counts": {},
            },
        )

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "duplicate_explosion")
        self.assertEqual(anomalies[0]["severity"], "critical")
        self.assertEqual(anomalies[0]["message"], "Duplicate rows increased from 0 to 5")
        self.assertEqual(anomalies[0]["detail"], "Duplicate rate is 38.46% using key: order_id")
        self.assertEqual(
            anomalies[0]["impact"],
            "Duplicate records may inflate COUNT, SUM, revenue, and downstream metrics.",
        )

    def test_infer_freshness_column_prefers_created_at(self):
        from agent.quality_checker import infer_freshness_column

        self.assertEqual(
            infer_freshness_column(["order_id", "created_at"]),
            "created_at",
        )

    def test_get_table_metrics_collects_freshness_metadata(self):
        from agent.quality_checker import get_table_metrics

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE events (id INTEGER, created_at TEXT)")
        conn.executemany(
            "INSERT INTO events (id, created_at) VALUES (?, ?)",
            [
                (1, "2026-06-04 10:00:00"),
                (2, "2026-06-04 10:30:00"),
            ],
        )
        conn.commit()
        conn.close()

        with patch("agent.quality_checker.datetime") as clock:
            clock.utcnow.return_value = datetime(2026, 6, 4, 12, 30, 0)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            metrics = get_table_metrics(str(self.db_path), "events")

        self.assertEqual(metrics["freshness_column"], "created_at")
        self.assertEqual(metrics["last_updated"], "2026-06-04 10:30:00")
        self.assertEqual(metrics["freshness_minutes"], 120)

    def test_detect_anomalies_flags_freshness_threshold_breach(self):
        from agent.quality_checker import detect_anomalies

        anomalies = detect_anomalies(
            {
                "table": "raw_orders",
                "row_count": 10,
                "freshness_column": "created_at",
                "last_updated": "2026-06-04 02:00:00",
                "freshness_minutes": 480,
                "null_rates": {},
                "distinct_counts": {},
                "duplicate_rows": 0,
            },
            {
                "table": "raw_orders",
                "row_count": 10,
                "freshness_threshold_minutes": 60,
                "null_rates": {},
                "distinct_counts": {},
                "duplicate_rows": 0,
            },
        )

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "freshness_anomaly")
        self.assertEqual(anomalies[0]["severity"], "high")
        self.assertEqual(anomalies[0]["message"], "Table is stale by 8.0 hours")
        self.assertEqual(
            anomalies[0]["detail"],
            "Latest created_at value is 2026-06-04 02:00:00",
        )
        self.assertEqual(
            anomalies[0]["impact"],
            "Downstream models may be using outdated data",
        )

    def test_get_table_metrics_collects_schema_metadata_and_hash(self):
        from agent.quality_checker import get_table_metrics

        metrics = get_table_metrics(str(self.db_path), "raw_orders")

        self.assertIn("schema", metrics)
        self.assertIn("schema_hash", metrics)
        self.assertEqual(
            metrics["schema"],
            [
                {
                    "name": "order_id",
                    "data_type": "INTEGER",
                    "nullable": True,
                    "primary_key": False,
                }
            ],
        )
        self.assertEqual(len(metrics["schema_hash"]), 64)

    def test_detect_anomalies_flags_added_removed_and_type_schema_drift(self):
        from agent.quality_checker import detect_anomalies

        baseline = {
            "table": "raw_orders",
            "row_count": 10,
            "schema": [
                {"name": "order_id", "data_type": "INTEGER", "nullable": True, "primary_key": False},
                {"name": "customer_segment", "data_type": "TEXT", "nullable": True, "primary_key": False},
                {"name": "order_total", "data_type": "TEXT", "nullable": True, "primary_key": False},
            ],
            "null_rates": {},
            "distinct_counts": {},
            "duplicate_rows": 0,
        }
        current = {
            "table": "raw_orders",
            "row_count": 10,
            "schema": [
                {"name": "order_id", "data_type": "INTEGER", "nullable": True, "primary_key": False},
                {"name": "order_total", "data_type": "REAL", "nullable": True, "primary_key": False},
                {"name": "new_column", "data_type": "TEXT", "nullable": True, "primary_key": False},
            ],
            "null_rates": {},
            "distinct_counts": {},
            "duplicate_rows": 0,
        }

        anomalies = detect_anomalies(current, baseline)
        schema_anomalies = [a for a in anomalies if a["type"] == "schema_drift"]

        self.assertEqual(
            [a["schema_change"]["change_type"] for a in schema_anomalies],
            ["removed_column", "added_column", "type_change"],
        )
        self.assertEqual(schema_anomalies[0]["severity"], "critical")
        self.assertEqual(
            schema_anomalies[0]["message"],
            "Schema drift detected: column 'customer_segment' was removed",
        )
        self.assertEqual(schema_anomalies[1]["severity"], "medium")
        self.assertEqual(schema_anomalies[2]["severity"], "high")
        self.assertEqual(schema_anomalies[2]["schema_change"]["old_type"], "TEXT")
        self.assertEqual(schema_anomalies[2]["schema_change"]["new_type"], "REAL")

if __name__ == "__main__":
    unittest.main()
