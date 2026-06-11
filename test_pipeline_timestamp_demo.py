import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


class PipelineTimestampDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_freshness_column_prefers_source_watermark_over_model_build_time(self):
        from agent.quality_checker import infer_freshness_column

        self.assertEqual(
            infer_freshness_column(
                [
                    "metric_date",
                    "model_built_at",
                    "source_max_event_time",
                    "source_max_ingested_at",
                    "source_max_updated_at",
                ]
            ),
            "source_max_updated_at",
        )

    def test_pipeline_models_propagate_source_freshness_watermarks(self):
        from scripts.create_pipeline_timestamp_demo import create_pipeline_timestamp_demo
        from scripts.run_pipeline_timestamp_models import run_pipeline_timestamp_models
        from scripts.simulate_pipeline_timestamp_stale_data import (
            simulate_pipeline_timestamp_stale_data,
        )

        db_path = create_pipeline_timestamp_demo(self.base_path, row_count=25)
        run_pipeline_timestamp_models(self.base_path)

        self.assertEqual(
            self._tables(),
            ["dashboard_metrics", "fct_revenue", "raw_orders", "stg_orders"],
        )
        self.assertEqual(self._count("raw_orders"), 25)
        self.assertEqual(self._count("stg_orders"), 25)

        raw_max = self._scalar("SELECT MAX(updated_at) FROM raw_orders")
        stg_max = self._scalar("SELECT MAX(source_max_updated_at) FROM stg_orders")
        fct_max = self._scalar("SELECT MAX(source_max_updated_at) FROM fct_revenue")
        dash_max = self._scalar(
            "SELECT MAX(source_max_updated_at) FROM dashboard_metrics"
        )

        self.assertEqual(stg_max, raw_max)
        self.assertEqual(fct_max, raw_max)
        self.assertEqual(dash_max, raw_max)

        simulate_pipeline_timestamp_stale_data(self.base_path, stale_hours=48)
        run_pipeline_timestamp_models(self.base_path)

        stale_raw_max = self._scalar("SELECT MAX(updated_at) FROM raw_orders")
        stale_dash_max = self._scalar(
            "SELECT MAX(source_max_updated_at) FROM dashboard_metrics"
        )
        parsed = datetime.strptime(stale_dash_max, "%Y-%m-%d %H:%M:%S")

        self.assertEqual(stale_dash_max, stale_raw_max)
        self.assertGreaterEqual(datetime.utcnow() - parsed, timedelta(hours=47))

    def _tables(self):
        conn = sqlite3.connect(self.base_path / "pipeline_timestamp_demo" / "db" / "pipeline_timestamp.db")
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
        finally:
            conn.close()

    def _count(self, table):
        return self._scalar(f"SELECT COUNT(*) FROM {table}")

    def _scalar(self, sql):
        conn = sqlite3.connect(self.base_path / "pipeline_timestamp_demo" / "db" / "pipeline_timestamp.db")
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
