import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
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

    def test_freshness_prefers_source_watermark_over_model_build_time(self):
        from agent.metadata_checks import get_freshness_timestamp

        self.conn.execute(
            """
            CREATE TABLE watermark_model (
                source_max_updated_at TEXT,
                model_built_at TEXT,
                updated_at TEXT
            )
            """
        )
        self.conn.executemany(
            "INSERT INTO watermark_model VALUES (?, ?, ?)",
            [
                (
                    "2026-06-20T08:00:00+00:00",
                    "2026-06-24T12:00:00+00:00",
                    "2026-06-24T12:00:00+00:00",
                ),
                (
                    "2026-06-21T08:00:00+00:00",
                    "2026-06-25T12:00:00+00:00",
                    "2026-06-25T12:00:00+00:00",
                ),
            ],
        )
        self.conn.commit()

        self.assertEqual(
            get_freshness_timestamp(self.conn, "watermark_model"),
            "2026-06-21T08:00:00+00:00",
        )

    def test_absolute_freshness_sla_evaluates_ok_stale_and_critical(self):
        from agent.metadata_checks import evaluate_freshness_sla

        now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            evaluate_freshness_sla(
                "2026-06-24T10:00:00Z",
                now=now,
                stale_after_hours=6,
                critical_after_hours=24,
            )["status"],
            "ok",
        )
        self.assertEqual(
            evaluate_freshness_sla(
                "2026-06-24T00:00:00Z",
                now=now,
                stale_after_hours=6,
                critical_after_hours=24,
            )["status"],
            "stale",
        )
        critical = evaluate_freshness_sla(
            "2026-06-23T00:00:00Z",
            now=now,
            stale_after_hours=6,
            critical_after_hours=24,
        )
        self.assertEqual(critical["status"], "critical")
        self.assertEqual(critical["age_hours"], 36.0)

    def test_run_metadata_checks_reports_absolute_freshness_breach(self):
        from agent.metadata_checks import run_metadata_checks

        result = run_metadata_checks(
            self.conn,
            "sample_model",
            ["customer_id"],
            now=datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc),
            stale_after_hours=6,
            critical_after_hours=24,
        )

        self.assertEqual(result.freshness_status, "critical")
        self.assertEqual(result.freshness_age_hours, 54.0)
        self.assertEqual(result.freshness_column, "updated_at")
        self.assertIn("Freshness SLA breached: critical", result.anomalies)

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

    def test_to_signal_treats_clean_evaluated_metadata_as_neutral(self):
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
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])

    def test_to_signal_treats_unavailable_metadata_checks_as_neutral(self):
        from agent.metadata_checks import to_signal

        signal = to_signal(
            {
                "model_name": "sample_model",
                "evaluation_status": "not_evaluated",
                "anomalies": [],
            }
        )

        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])
        self.assertEqual(signal.metadata["evaluation_status"], "not_evaluated")

    def test_to_signal_preserves_a_genuine_evaluated_low_finding(self):
        from agent.metadata_checks import to_signal
        from agent.signals import Severity

        signal = to_signal(
            {
                "model_name": "sample_model",
                "severity": "LOW",
                "evaluation_status": "evaluated",
                "freshness_timestamp": "2026-07-27T00:00:00",
                "anomalies": ["Freshness is close to its warning boundary"],
            }
        )

        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.score, -5)
        self.assertEqual(
            signal.reasons,
            ["Freshness is close to its warning boundary"],
        )

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
