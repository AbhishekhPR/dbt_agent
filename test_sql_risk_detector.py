import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class SqlRiskDetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project_path = Path(self.tmp.name) / "demo_project"
        self.models_path = self.project_path / "models"
        self.models_path.mkdir(parents=True)
        self.metadata_db = Path(self.tmp.name) / "metadata_history.db"

    def tearDown(self):
        self.tmp.cleanup()

    def write_model(self, name: str, sql: str):
        path = self.models_path / f"{name}.sql"
        path.write_text(sql, encoding="utf-8")
        return path

    def test_detects_left_join_where_filter_on_right_alias(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model(
            "fct_customer_lifetime_value",
            """
            SELECT o.order_id, c.customer_id
            FROM raw_orders o
            LEFT JOIN raw_customers c ON o.customer_id = c.id
            WHERE c.is_deleted = 0
            """,
        )

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["risk_type"], "left_join_filter_risk")
        self.assertEqual(risks[0]["severity"], "high")
        self.assertEqual(risks[0]["evidence"], "WHERE c.is_deleted = [NUMBER_LITERAL]")

    def test_detects_supported_static_sql_risks(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model(
            "risky_model",
            """
            SELECT *, revenue / orders AS revenue_per_order
            FROM raw_orders o
            CROSS JOIN calendar c
            JOIN raw_customers r
            WHERE created_at >= '2024-01-01'
              AND status != 'cancelled'
            """,
        )

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        risk_types = [risk["risk_type"] for risk in risks]
        self.assertIn("select_star_risk", risk_types)
        self.assertIn("cross_join_risk", risk_types)
        self.assertIn("join_without_condition_risk", risk_types)
        self.assertIn("division_by_zero_risk", risk_types)
        self.assertIn("hardcoded_date_filter_risk", risk_types)
        self.assertIn("not_equal_filter_risk", risk_types)

    def test_protected_division_is_not_reported(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model(
            "safe_ratio",
            "SELECT SUM(order_total) / NULLIF(COUNT(order_id), 0) AS avg_order FROM raw_orders",
        )

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        self.assertNotIn("division_by_zero_risk", [risk["risk_type"] for risk in risks])

    def test_division_evidence_keeps_nested_denominator_parentheses(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model(
            "nested_ratio",
            "SELECT SUM(order_total) / COUNT(DISTINCT DATE(created_at)) AS daily_avg FROM raw_orders",
        )

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        division_risks = [risk for risk in risks if risk["risk_type"] == "division_by_zero_risk"]
        self.assertEqual(len(division_risks), 1)
        self.assertEqual(
            division_risks[0]["evidence"],
            "SUM(order_total) / COUNT(DISTINCT DATE(created_at))",
        )

    def test_sanitizes_literals_in_returned_and_stored_evidence(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model(
            "sensitive_filters",
            """
            SELECT COUNT(CASE WHEN email = 'abc@gmail.com' THEN 1 END) / COUNT(user_id) AS risky_ratio
            FROM raw_users u
            LEFT JOIN tokens t ON u.id = t.user_id
            WHERE created_at >= '2026-06-04 10:30:00'
              AND api_token = 'REDACTED_TEST_SECRET'
              AND t.api_token = 'REDACTED_TEST_SECRET'
              AND t.uuid = '550e8400-e29b-41d4-a716-446655440000'
              AND amount > 1000
              AND status <> 'cancelled'
              AND t.source_id = 42
            """,
        )

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        evidence = "\n".join(risk["evidence"] for risk in risks)
        self.assertIn("[EMAIL_LITERAL]", evidence)
        self.assertIn("[NUMBER_LITERAL]", evidence)
        self.assertIn("[DATETIME_LITERAL]", evidence)
        self.assertIn("[UUID_LITERAL]", evidence)
        self.assertIn("WHERE t.api_token = [STRING_LITERAL]", evidence)
        self.assertIn("status <> [STRING_LITERAL]", evidence)
        self.assertNotIn("abc@gmail.com", evidence)
        self.assertNotIn("2026-06-04 10:30:00", evidence)
        self.assertNotIn("REDACTED_TEST_SECRET", evidence)
        self.assertNotIn("550e8400-e29b-41d4-a716-446655440000", evidence)
        self.assertNotIn("cancelled", evidence)
        self.assertNotIn("1000", evidence)

        conn = sqlite3.connect(self.metadata_db)
        stored_evidence = "\n".join(
            row[0] for row in conn.execute("SELECT evidence FROM sql_risks").fetchall()
        )
        conn.close()
        self.assertEqual(evidence, stored_evidence)
        self.assertNotIn("abc@gmail.com", stored_evidence)
        self.assertNotIn("REDACTED_TEST_SECRET", stored_evidence)

    def test_print_sql_risks_uses_sanitized_evidence(self):
        from agent.sql_risk_detector import detect_sql_risks, print_sql_risks
        from io import StringIO
        import contextlib

        self.write_model("not_equal", "SELECT * FROM raw_orders WHERE status != 'cancelled'")

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            risks = detect_sql_risks(str(self.project_path))

        output = StringIO()
        with contextlib.redirect_stdout(output):
            print_sql_risks(risks)

        report = output.getvalue()
        self.assertIn("WHERE status != [STRING_LITERAL]", report)
        self.assertNotIn("cancelled", report)

    def test_stores_risks_and_clears_previous_project_results(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model("star_model", "SELECT * FROM raw_orders")

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            first = detect_sql_risks(str(self.project_path))
            second = detect_sql_risks(str(self.project_path))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        conn = sqlite3.connect(self.metadata_db)
        rows = conn.execute(
            "SELECT project_name, model_name, risk_type FROM sql_risks"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [("demo_project", "star_model", "select_star_risk")])

    def test_detector_does_not_execute_sql(self):
        from agent.sql_risk_detector import detect_sql_risks

        self.write_model("dangerous_text", "DROP TABLE raw_orders; SELECT * FROM raw_orders")

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db), patch(
            "sqlite3.connect", side_effect=AssertionError("sqlite should not be used")
        ):
            risks = detect_sql_risks(str(self.project_path), persist=False)

        self.assertEqual([risk["risk_type"] for risk in risks], ["select_star_risk"])

    def test_cli_sql_risks_prints_report(self):
        from agent.cli import cli

        self.write_model("star_model", "SELECT * FROM raw_orders")
        runner = CliRunner()

        with patch("agent.sql_risk_detector.METADATA_HISTORY_DB", self.metadata_db):
            result = runner.invoke(cli, ["sql_risks", "--project", str(self.project_path)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Scanning SQL models for risky transformation logic...", result.output)
        self.assertIn("1 risk(s) found.", result.output)
        self.assertIn("[MEDIUM] star_model", result.output)
        self.assertIn("SELECT * can cause downstream schema changes", result.output)


if __name__ == "__main__":
    unittest.main()
