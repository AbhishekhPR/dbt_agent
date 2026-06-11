import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner


class PrGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tmpdir.name) / "demo_project"
        models = self.project / "models"
        models.mkdir(parents=True)
        (models / "fct_customer_lifetime_value.sql").write_text(
            """
            SELECT
                o.customer_id,
                c.customer_segment,
                SUM(o.order_total) AS lifetime_value
            FROM raw_orders o
            LEFT JOIN raw_customers c
                ON o.customer_id = c.customer_id
            WHERE c.is_deleted = 0
            GROUP BY o.customer_id, c.customer_segment
            """,
            encoding="utf-8",
        )
        (models / "fct_daily_kpis.sql").write_text(
            """
            SELECT
                COUNT(*) AS active_customers,
                SUM(lifetime_value) AS total_ltv
            FROM fct_customer_lifetime_value
            """,
            encoding="utf-8",
        )
        self.report_path = self.project / ".relium" / "pr_guard_report.md"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_pr_guard_fails_on_high_risk_and_writes_report(self):
        from agent.cli import cli

        result = CliRunner().invoke(
            cli,
            [
                "pr_guard",
                "--project",
                str(self.project),
                "--output",
                str(self.report_path),
            ],
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Relium PR Guard", result.output)
        self.assertIn("Files scanned: 2", result.output)
        self.assertIn("Risks found: 1", result.output)
        self.assertIn("Highest severity: HIGH", result.output)
        self.assertIn("Safe to merge: NO", result.output)
        self.assertIn(f"Report written to {self.report_path}", result.output)

        report = self.report_path.read_text(encoding="utf-8")
        self.assertIn("# Relium PR Guard Report", report)
        self.assertIn("Safe to merge: NO", report)
        self.assertIn("### [HIGH] fct_customer_lifetime_value", report)
        self.assertIn("File: models/fct_customer_lifetime_value.sql", report)
        self.assertIn(
            "LEFT JOIN may behave like INNER JOIN because WHERE filters the right-side table.",
            report,
        )
        self.assertIn("Evidence: WHERE c.is_deleted = [NUMBER_LITERAL]", report)
        self.assertIn(
            "```sql\n"
            "LEFT JOIN raw_customers c\n"
            "    ON o.customer_id = c.customer_id\n"
            "   AND c.is_deleted = 0\n"
            "```",
            report,
        )
        self.assertIn("- fct_daily_kpis", report)

    def test_pr_guard_changed_files_scans_only_selected_files(self):
        from agent.cli import cli

        result = CliRunner().invoke(
            cli,
            [
                "pr_guard",
                "--project",
                str(self.project),
                "--changed-files",
                str(self.project / "models" / "fct_daily_kpis.sql"),
                "--output",
                str(self.report_path),
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Files scanned: 1", result.output)
        self.assertIn("Risks found: 0", result.output)
        self.assertIn("Safe to merge: YES", result.output)


if __name__ == "__main__":
    unittest.main()
