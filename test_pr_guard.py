import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        (models / "dashboard_executive_metrics.sql").write_text(
            """
            SELECT
                SUM(total_ltv) AS dashboard_ltv,
                SUM(active_customers) AS dashboard_customers
            FROM fct_daily_kpis
            """,
            encoding="utf-8",
        )
        (models / "test_risky_customer_orders.sql").write_text(
            """
            SELECT
                o.order_id,
                c.customer_segment
            FROM raw_orders o
            LEFT JOIN raw_customers c
                ON o.customer_id = c.customer_id
            WHERE c.is_deleted = 0
            """,
            encoding="utf-8",
        )
        self.report_path = self.project / ".relium" / "pr_guard_report.md"
        self.comment_path = self.project / ".relium" / "pr_guard_comment.md"

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
        self.assertIn("Files scanned: 4", result.output)
        self.assertIn("Risks found: 2", result.output)
        self.assertIn("Highest severity: HIGH", result.output)
        self.assertIn("Safe to merge: NO", result.output)
        self.assertIn(f"Report written to {self.report_path}", result.output)

        report = self.report_path.read_text(encoding="utf-8")
        self.assertIn("# Relium PR Guard Report", report)
        self.assertIn("Safe to merge: NO", report)
        self.assertIn("Merge decision: Blocked because HIGH risk transformation logic was detected.", report)
        self.assertIn("### [HIGH] fct_customer_lifetime_value", report)
        self.assertIn("File: models/fct_customer_lifetime_value.sql", report)
        self.assertIn(
            "LEFT JOIN may behave like INNER JOIN because WHERE filters the right-side table.",
            report,
        )
        self.assertIn("Confidence: 95%", report)
        self.assertIn("Impact Level: HIGH", report)
        self.assertIn("Blast Radius Score: 9/10", report)
        self.assertIn("Evidence: WHERE c.is_deleted = [NUMBER_LITERAL]", report)
        self.assertIn("Business impact:", report)
        self.assertIn(
            "This change may silently remove valid rows from the left-side table. "
            "Metrics such as customer lifetime value, revenue, order counts, daily KPIs, and "
            "dashboard totals may become undercounted.",
            report,
        )
        self.assertIn(
            "Recommended Action: Fix before merge. This risky transformation may silently remove records "
            "and affect downstream business models.",
            report,
        )
        self.assertNotIn("This risk may affect downstream models:", report)
        self.assertNotIn("right-side tablein", report)
        self.assertNotIn("such ascustomer", report)
        self.assertNotIn("maybecome", report)
        self.assertIn(
            "```sql\n"
            "LEFT JOIN raw_customers c\n"
            "    ON o.customer_id = c.customer_id\n"
            "   AND c.is_deleted = 0\n"
            "```",
            report,
        )
        self.assertIn("- fct_daily_kpis", report)
        self.assertIn("- dashboard_executive_metrics", report)

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

    def test_pr_guard_github_comment_local_mode_writes_comment(self):
        from agent.cli import cli

        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                cli,
                [
                    "pr_guard",
                    "--project",
                    str(self.project),
                    "--output",
                    str(self.report_path),
                    "--github-comment",
                    "--comment-output",
                    str(self.comment_path),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("GitHub environment not detected. Comment markdown written locally.", result.output)
        self.assertTrue(self.report_path.exists())
        self.assertTrue(self.comment_path.exists())

        comment = self.comment_path.read_text(encoding="utf-8")
        self.assertIn("<!-- relium-pr-guard -->", comment)
        self.assertIn("## Relium PR Guard", comment)
        self.assertIn("Safe to merge: NO", comment)
        self.assertIn("Project: " + str(self.project), comment)
        self.assertIn("Merge decision:\nBlocked because HIGH risk transformation logic was detected.", comment)
        self.assertIn("Files scanned: 4", comment)
        self.assertIn("Risks found: 2", comment)
        self.assertIn("Highest severity: HIGH", comment)
        self.assertIn("### High risk transformation logic found", comment)
        self.assertIn("#### fct_customer_lifetime_value", comment)
        self.assertIn("Confidence:\n95%", comment)
        self.assertIn("Impact Level:\nHIGH", comment)
        self.assertIn("Blast Radius Score:\n9/10", comment)
        self.assertIn("`WHERE c.is_deleted = [NUMBER_LITERAL]`", comment)
        self.assertIn("Business impact:", comment)
        self.assertIn(
            "This change may silently remove valid rows from the left-side table.",
            comment,
        )
        self.assertIn(
            "A LEFT JOIN should preserve rows from the left table. Filtering the "
            "right-side table in the WHERE clause can remove unmatched rows and "
            "silently change the business meaning of the model.",
            comment,
        )
        self.assertIn(
            "Metrics such as customer lifetime value, revenue, order counts, daily KPIs, "
            "and dashboard totals may become undercounted.",
            comment,
        )
        self.assertIn(
            "Recommended Action:\n"
            "Fix before merge. This risky transformation may silently remove records "
            "and affect downstream business models.",
            comment,
        )
        self.assertNotIn("This risk may affect downstream models:", comment)
        self.assertNotIn("right-side tablein", comment)
        self.assertNotIn("such ascustomer", comment)
        self.assertNotIn("maybecome", comment)
        self.assertIn(
            "```sql\n"
            "LEFT JOIN raw_customers c\n"
            "    ON o.customer_id = c.customer_id\n"
            "   AND c.is_deleted = 0\n"
            "```",
            comment,
        )
        self.assertIn("* fct_daily_kpis", comment)
        self.assertIn("* dashboard_executive_metrics", comment)

    def test_pr_guard_github_comment_respects_critical_fail_threshold(self):
        from agent.cli import cli

        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                cli,
                [
                    "pr_guard",
                    "--project",
                    str(self.project),
                    "--output",
                    str(self.report_path),
                    "--fail-on",
                    "critical",
                    "--github-comment",
                    "--comment-output",
                    str(self.comment_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.comment_path.exists())
        comment = self.comment_path.read_text(encoding="utf-8")
        self.assertIn("Safe to merge: YES", comment)
        self.assertIn("Merge decision:\nAllowed because no CRITICAL risks were detected.", comment)
        self.assertIn("Risks found: 2", comment)
        self.assertIn("Highest severity: HIGH", comment)

    def test_pr_guard_changed_existing_model_file_uses_model_blast_radius(self):
        from agent.cli import cli

        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                cli,
                [
                    "pr_guard",
                    "--project",
                    str(self.project),
                    "--changed-files",
                    f"{self.project.name}/models/fct_customer_lifetime_value.sql",
                    "--output",
                    str(self.report_path),
                    "--github-comment",
                    "--comment-output",
                    str(self.comment_path),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        comment = self.comment_path.read_text(encoding="utf-8")
        self.assertIn("Files scanned: 1", comment)
        self.assertIn("Risks found: 1", comment)
        self.assertIn("Safe to merge: NO", comment)
        self.assertIn("Confidence:\n95%", comment)
        self.assertIn("Impact Level:\nHIGH", comment)
        self.assertIn("Blast Radius Score:\n9/10", comment)
        self.assertIn(
            "Recommended Action:\n"
            "Fix before merge. This risky transformation may silently remove records "
            "and affect downstream business models.",
            comment,
        )
        self.assertIn("Business impact:", comment)
        self.assertIn("* fct_daily_kpis", comment)
        self.assertIn("* dashboard_executive_metrics", comment)

    def test_pr_guard_new_risky_file_without_lineage_has_no_downstream_models(self):
        from agent.cli import cli

        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                cli,
                [
                    "pr_guard",
                    "--project",
                    str(self.project),
                    "--changed-files",
                    str(self.project / "models" / "test_risky_customer_orders.sql"),
                    "--output",
                    str(self.report_path),
                    "--github-comment",
                    "--comment-output",
                    str(self.comment_path),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        comment = self.comment_path.read_text(encoding="utf-8")
        self.assertIn("Files scanned: 1", comment)
        self.assertIn("Risks found: 1", comment)
        self.assertIn("Safe to merge: NO", comment)
        self.assertIn("Confidence:\n95%", comment)
        self.assertIn("Impact Level:\nLOW", comment)
        self.assertIn("Blast Radius Score:\n1/10", comment)
        self.assertIn(
            "Recommended Action:\n"
            "Review before merge. No downstream models were found, but this SQL pattern can silently "
            "change row preservation behavior.",
            comment,
        )
        self.assertIn("Business impact:", comment)
        self.assertIn("* None found", comment)
        self.assertNotIn("* fct_daily_kpis", comment)

    def test_pr_guard_impact_scoring_thresholds_and_cap(self):
        from agent.pr_guard import _blast_radius_score, _impact_level

        high_risk = {"severity": "high"}

        self.assertEqual(_blast_radius_score([], high_risk), 1)
        self.assertEqual(_impact_level(1), "LOW")
        self.assertEqual(_impact_level(3), "LOW")
        self.assertEqual(_blast_radius_score(["model_a", "dashboard_sales"], high_risk), 7)
        self.assertEqual(_impact_level(4), "MEDIUM")
        self.assertEqual(_impact_level(6), "MEDIUM")
        self.assertEqual(_impact_level(7), "HIGH")
        self.assertEqual(_impact_level(9), "HIGH")
        self.assertEqual(_blast_radius_score(["a", "b", "c", "d", "e"], high_risk), 10)
        self.assertEqual(_impact_level(10), "CRITICAL")


if __name__ == "__main__":
    unittest.main()
