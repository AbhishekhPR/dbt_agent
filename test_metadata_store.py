import sqlite3
import tempfile
import unittest
from pathlib import Path


class MetadataStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "relium_metadata.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_db_tables_are_created(self):
        from agent.metadata_store import initialize_metadata_db

        initialize_metadata_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        conn.close()

        self.assertIn("relium_scan_runs", tables)
        self.assertIn("relium_model_metrics", tables)

    def test_scan_run_is_inserted(self):
        from agent.metadata_store import (
            ScanRunRecord,
            fetch_scan_run,
            initialize_metadata_db,
            insert_scan_run,
        )

        initialize_metadata_db(self.db_path)
        scan_id = insert_scan_run(
            self.db_path,
            ScanRunRecord(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                risk_level="HIGH",
                safe_to_merge=False,
                affected_models=["dim_customers", "mart_revenue"],
                report_text="AST risk plus metadata anomalies found.",
            ),
        )

        saved = fetch_scan_run(self.db_path, scan_id)
        self.assertEqual(saved["scan_id"], scan_id)
        self.assertEqual(saved["project_name"], "demo_project")
        self.assertEqual(saved["model_name"], "fct_customer_lifetime_value")
        self.assertEqual(saved["risk_level"], "HIGH")
        self.assertEqual(saved["safe_to_merge"], 0)

    def test_model_metrics_are_inserted(self):
        from agent.metadata_store import (
            ModelMetricRecord,
            ScanRunRecord,
            fetch_model_metrics,
            initialize_metadata_db,
            insert_model_metrics,
            insert_scan_run,
        )

        initialize_metadata_db(self.db_path)
        scan_id = insert_scan_run(
            self.db_path,
            ScanRunRecord(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                risk_level="HIGH",
                safe_to_merge=False,
                affected_models=["dim_customers"],
                report_text="report",
            ),
        )

        insert_model_metrics(
            self.db_path,
            ModelMetricRecord(
                scan_id=scan_id,
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                row_count=3,
                null_count=1,
                duplicate_count=1,
                freshness_timestamp="2026-06-24T10:00:00",
                schema_column_count=5,
            ),
        )

        rows = fetch_model_metrics(self.db_path, scan_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_name"], "demo_project")
        self.assertEqual(rows[0]["row_count"], 3)
        self.assertEqual(rows[0]["null_count"], 1)
        self.assertEqual(rows[0]["duplicate_count"], 1)
        self.assertEqual(rows[0]["schema_column_count"], 5)

    def test_metric_drift_rows_are_inserted(self):
        from agent.metadata_store import (
            DriftRecord,
            insert_metric_drift,
            fetch_metric_drifts,
            initialize_metadata_db,
        )

        initialize_metadata_db(self.db_path)
        insert_metric_drift(
            self.db_path,
            DriftRecord(
                project_name="demo_project",
                model_name="fct_customer_lifetime_value",
                current_scan_id="scan-current",
                previous_scan_id="scan-previous",
                row_count_change_pct=-50.0,
                null_count_change_pct=0.0,
                duplicate_count_change_pct=100.0,
                schema_column_count_change=1,
                freshness_regressed=True,
                drift_level="HIGH",
                report_text="Row count change: -50%\nDuplicate count change: +100%\n\nMetadata Drift: HIGH",
            ),
        )

        rows = fetch_metric_drifts(
            self.db_path,
            project_name="demo_project",
            model_name="fct_customer_lifetime_value",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["drift_level"], "HIGH")
        self.assertEqual(rows[0]["row_count_change_pct"], -50.0)
        self.assertEqual(rows[0]["duplicate_count_change_pct"], 100.0)
        self.assertEqual(rows[0]["schema_column_count_change"], 1)
        self.assertEqual(rows[0]["freshness_regressed"], 1)


if __name__ == "__main__":
    unittest.main()
