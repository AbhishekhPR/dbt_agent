import tempfile
import unittest
from pathlib import Path


class BlastRadiusTests(unittest.TestCase):
    def test_calculate_blast_radius_traverses_all_downstream_hops(self):
        from agent.blast_radius import calculate_blast_radius

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            models = project / "models"
            models.mkdir()
            (models / "fct_customer_lifetime_value.sql").write_text(
                "select * from raw_customers",
                encoding="utf-8",
            )
            (models / "fct_daily_kpis.sql").write_text(
                "select * from fct_customer_lifetime_value",
                encoding="utf-8",
            )
            (models / "dashboard_executive_metrics.sql").write_text(
                "select * from fct_daily_kpis",
                encoding="utf-8",
            )

            report = calculate_blast_radius(str(project), "raw_customers")

        self.assertEqual(
            [item["model"] for item in report["directly_affected"]],
            ["fct_customer_lifetime_value"],
        )
        self.assertEqual(
            [item["model"] for item in report["indirectly_affected"]],
            ["fct_daily_kpis", "dashboard_executive_metrics"],
        )
        reasons = {
            item["model"]: item["reason"]
            for item in report["indirectly_affected"]
        }
        self.assertEqual(
            reasons["fct_daily_kpis"],
            "Depends on fct_customer_lifetime_value which depends on raw_customers",
        )
        self.assertEqual(
            reasons["dashboard_executive_metrics"],
            (
                "Depends on fct_daily_kpis which depends on "
                "fct_customer_lifetime_value which depends on raw_customers"
            ),
        )
        self.assertEqual(report["total_affected"], 3)

    def test_calculate_blast_radius_uses_visited_set_for_cycles(self):
        from agent.blast_radius import calculate_blast_radius

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            models = project / "models"
            models.mkdir()
            (models / "model_a.sql").write_text(
                "select * from raw_customers",
                encoding="utf-8",
            )
            (models / "model_b.sql").write_text(
                "select * from model_a",
                encoding="utf-8",
            )
            (models / "model_a_cycle.sql").write_text(
                "select * from model_b join model_a on 1 = 1",
                encoding="utf-8",
            )
            (models / "model_a.sql").write_text(
                "select * from raw_customers join model_a_cycle on 1 = 1",
                encoding="utf-8",
            )

            report = calculate_blast_radius(str(project), "raw_customers")

        models_seen = [
            item["model"]
            for item in report["directly_affected"] + report["indirectly_affected"]
        ]
        self.assertEqual(len(models_seen), len(set(models_seen)))
        self.assertIn("model_b", models_seen)


if __name__ == "__main__":
    unittest.main()
