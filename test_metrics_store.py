import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class MetricsStoreTests(unittest.TestCase):
    def test_record_table_metrics_appends_to_project_history(self):
        from agent import metrics_store

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store_path = tmp_path / "relium_data"
            store_path.mkdir()
            metadata_db = tmp_path / "metadata_history.db"

            with patch.object(metrics_store, "STORE_PATH", store_path), patch.object(
                metrics_store, "METADATA_HISTORY_DB", metadata_db
            ):
                metrics_store.record_table_metrics(
                    "demo_project",
                    "raw_orders",
                    {
                        "row_count": 10,
                        "duplicate_rows": 1,
                        "null_rates": {"customer_id": 0.0},
                        "distinct_counts": {"customer_id": 10},
                        "numeric_stats": {"amount": {"min": 1, "max": 9, "avg": 5}},
                    },
                )
                metrics_store.record_table_metrics(
                    "demo_project",
                    "raw_orders",
                    {
                        "row_count": 12,
                        "duplicate_rows": 2,
                        "null_rates": {"customer_id": 8.33},
                        "distinct_counts": {"customer_id": 11},
                        "numeric_stats": {"amount": {"min": 1, "max": 12, "avg": 6}},
                    },
                )

                history = metrics_store.get_metric_history(
                    "demo_project", "raw_orders"
                )

        self.assertEqual([row["row_count"] for row in history], [10, 12])
        self.assertEqual([row["duplicate_rows"] for row in history], [1, 2])
        self.assertEqual(history[-1]["null_rates"], '{"customer_id": 8.33}')

    def test_history_command_prints_latest_quality_metrics_for_table(self):
        from agent import metrics_store
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store_path = tmp_path / "relium_data"
            store_path.mkdir()
            metadata_db = tmp_path / "metadata_history.db"

            with patch.object(metrics_store, "STORE_PATH", store_path), patch.object(
                metrics_store, "METADATA_HISTORY_DB", metadata_db
            ):
                metrics_store.record_table_metrics(
                    "demo_project",
                    "raw_orders",
                    {
                        "row_count": 12,
                        "duplicate_rows": 2,
                        "null_rates": {"customer_id": 8.33},
                        "distinct_counts": {"customer_id": 11},
                        "numeric_stats": {},
                    },
                )

                result = CliRunner().invoke(
                    cli,
                    [
                        "history",
                        "--project",
                        "demo_project",
                        "--table",
                        "raw_orders",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Latest quality metrics for 'raw_orders'", result.output)
        self.assertIn("Row count:       12", result.output)
        self.assertIn("Duplicate rows:  2", result.output)
        self.assertIn("customer_id: 8.33%", result.output)


if __name__ == "__main__":
    unittest.main()
