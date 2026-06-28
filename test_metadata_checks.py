import sqlite3
import tempfile
import unittest
from pathlib import Path


class MetadataChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "demo.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE sample_model (
                customer_id INTEGER,
                order_total REAL,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        self.conn.executemany(
            "INSERT INTO sample_model VALUES (?, ?, ?, ?)",
            [
                (1, 10.0, "2026-06-20T10:00:00", "2026-06-20T12:00:00"),
                (1, 20.0, "2026-06-21T10:00:00", "2026-06-21T12:00:00"),
                (None, 30.0, "2026-06-22T10:00:00", "2026-06-22T12:00:00"),
                (2, 40.0, "2026-06-23T10:00:00", "2026-06-23T12:00:00"),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_row_count_check_works(self):
        from agent.metadata_checks import get_row_count

        self.assertEqual(get_row_count(self.conn, "sample_model"), 4)

    def test_null_count_check_works(self):
        from agent.metadata_checks import get_null_count

        self.assertEqual(get_null_count(self.conn, "sample_model", ["customer_id"]), 1)

    def test_duplicate_count_check_works(self):
        from agent.metadata_checks import get_duplicate_count

        self.assertEqual(
            get_duplicate_count(self.conn, "sample_model", ["customer_id"]),
            1,
        )

    def test_freshness_check_works(self):
        from agent.metadata_checks import get_freshness_timestamp

        self.assertEqual(
            get_freshness_timestamp(self.conn, "sample_model"),
            "2026-06-23T12:00:00",
        )

    def test_schema_column_count_works(self):
        from agent.metadata_checks import get_schema_column_count

        self.assertEqual(get_schema_column_count(self.conn, "sample_model"), 4)

    def test_to_signal_converts_high_metadata_result_to_high_signal(self):
        from agent.metadata_checks import MetadataCheckResult, to_signal
        from agent.signals import Severity, Signal

        result = MetadataCheckResult(
            model_name="sample_model",
            row_count=4,
            null_count=1,
            duplicate_count=1,
            freshness_timestamp="2026-06-23T12:00:00",
            schema_column_count=4,
            anomalies=[
                "1 key-column nulls detected",
                "1 duplicate key rows detected",
            ],
        )

        signal = to_signal(result)

        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.component, "metadata_checks")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, 95)
        self.assertEqual(signal.score, -30)
        self.assertIn("Null rate increased", signal.reasons)
        self.assertIn("Duplicate count increased", signal.reasons)

    def test_to_signal_converts_low_metadata_result_to_low_signal(self):
        from agent.metadata_checks import MetadataCheckResult, to_signal
        from agent.signals import Severity

        result = MetadataCheckResult(
            model_name="sample_model",
            row_count=4,
            null_count=0,
            duplicate_count=0,
            freshness_timestamp="2026-06-23T12:00:00",
            schema_column_count=4,
            anomalies=[],
        )

        signal = to_signal(result)

        self.assertEqual(signal.component, "metadata_checks")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 75)
        self.assertEqual(signal.score, -5)

    def test_to_signal_preserves_metadata_fields(self):
        from agent.metadata_checks import MetadataCheckResult, to_signal

        result = MetadataCheckResult(
            model_name="sample_model",
            row_count=4,
            null_count=1,
            duplicate_count=1,
            freshness_timestamp="2026-06-23T12:00:00",
            schema_column_count=4,
            anomalies=["1 duplicate key rows detected"],
        )

        signal = to_signal(result, safe_to_continue=False)

        self.assertEqual(
            signal.metadata,
            {
                "row_count": 4,
                "null_count": 1,
                "duplicate_count": 1,
                "freshness_timestamp": "2026-06-23T12:00:00",
                "schema_columns": 4,
                "safe_to_continue": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
