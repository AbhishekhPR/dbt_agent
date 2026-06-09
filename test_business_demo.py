import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BusinessDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_generator_creates_deterministic_business_demo_database_and_models(self):
        from scripts.create_business_demo import create_business_demo

        counts = {
            "raw_customers": 20,
            "raw_products": 8,
            "raw_orders": 50,
            "raw_payments": 50,
            "raw_events": 75,
        }

        create_business_demo(self.base_path, counts)
        first_snapshot = self._sample_rows()

        create_business_demo(self.base_path, counts)
        second_snapshot = self._sample_rows()

        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(self._table_counts(), counts)
        self.assertEqual(
            {
                path.name for path in (
                    self.base_path / "quality_baselines"
                ).glob("raw_*.json")
            },
            {f"{table}.json" for table in counts},
        )

        expected_models = {
            "fct_revenue.sql",
            "fct_customer_lifetime_value.sql",
            "fct_product_performance.sql",
            "fct_daily_kpis.sql",
            "dashboard_executive_metrics.sql",
        }
        models_path = self.base_path / "business_demo" / "models"
        self.assertEqual(
            {path.name for path in models_path.glob("*.sql")},
            expected_models,
        )

    def test_demo_models_include_expected_left_join_risk(self):
        from scripts.create_business_demo import create_business_demo
        from agent.sql_risk_detector import detect_sql_risks

        create_business_demo(
            self.base_path,
            {
                "raw_customers": 20,
                "raw_products": 8,
                "raw_orders": 50,
                "raw_payments": 50,
                "raw_events": 75,
            },
        )

        metadata_db = self.base_path / "metadata_history.db"
        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", metadata_db):
            risks = detect_sql_risks(str(self.base_path / "business_demo"))

        self.assertEqual(
            [risk["risk_type"] for risk in risks],
            ["left_join_filter_risk"],
        )
        left_join_risks = [
            risk for risk in risks
            if risk["risk_type"] == "left_join_filter_risk"
        ]
        self.assertEqual(len(left_join_risks), 1)
        self.assertEqual(left_join_risks[0]["model"], "fct_customer_lifetime_value")
        self.assertEqual(left_join_risks[0]["severity"], "high")
        self.assertEqual(left_join_risks[0]["evidence"], "WHERE c.is_deleted = [NUMBER_LITERAL]")

    def test_raw_orders_allows_duplicate_business_keys_for_simulation(self):
        from scripts.create_business_demo import create_business_demo
        from agent import quality_checker
        from agent.simulator import run_simulation

        create_business_demo(
            self.base_path,
            {
                "raw_customers": 20,
                "raw_products": 8,
                "raw_orders": 50,
                "raw_payments": 50,
                "raw_events": 75,
            },
        )

        db_path = self.base_path / "business_demo" / "db" / "business.db"
        with patch.object(
            quality_checker,
            "BASELINE_PATH",
            self.base_path / "quality_baselines",
        ), patch("agent.simulator.run_quality_check"):
            run_simulation(
                "business_demo",
                str(db_path),
                "raw_orders",
                "duplicate_explosion",
            )

        self.assertEqual(self._table_counts()["raw_orders"], 50)

    def _table_counts(self):
        conn = sqlite3.connect(self.base_path / "business_demo" / "db" / "business.db")
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "raw_customers",
                    "raw_products",
                    "raw_orders",
                    "raw_payments",
                    "raw_events",
                )
            }
        finally:
            conn.close()

    def _sample_rows(self):
        conn = sqlite3.connect(self.base_path / "business_demo" / "db" / "business.db")
        try:
            return {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 5").fetchall()
                for table in (
                    "raw_customers",
                    "raw_products",
                    "raw_orders",
                    "raw_payments",
                    "raw_events",
                )
            }
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
