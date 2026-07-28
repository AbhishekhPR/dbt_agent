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

    def test_to_signal_converts_high_blast_radius_to_high_signal(self):
        from agent.blast_radius import to_signal
        from agent.signals import Severity, Signal

        result = {
            "changed_table": "raw_customers",
            "risk_level": "HIGH",
            "directly_affected": [
                {"model": "fct_customer_lifetime_value", "risk": "high"}
            ],
            "indirectly_affected": [
                {
                    "model": "dashboard_executive_metrics",
                    "risk": "medium",
                    "dependency_path": [
                        "raw_customers",
                        "fct_customer_lifetime_value",
                        "dashboard_executive_metrics",
                    ],
                }
            ],
            "total_affected": 2,
            "dashboard_count": 1,
            "blast_radius_score": 80,
            "dependency_depth": 2,
        }

        signal = to_signal(result)

        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.component, "blast_radius")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, 95)
        self.assertEqual(signal.score, -25)
        self.assertIn("Downstream models affected", signal.reasons)
        self.assertIn("Executive dashboard affected", signal.reasons)

    def test_to_signal_treats_no_downstream_models_as_neutral(self):
        from agent.blast_radius import to_signal
        from agent.signals import Severity

        result = {
            "changed_table": "raw_customers",
            "risk_level": "LOW",
            "directly_affected": [],
            "indirectly_affected": [],
            "total_affected": 0,
        }

        signal = to_signal(result)

        self.assertEqual(signal.component, "blast_radius")
        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.confidence, 75)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])

    def test_to_signal_preserves_a_genuine_low_blast_radius_finding(self):
        from agent.blast_radius import to_signal
        from agent.signals import Severity

        signal = to_signal(
            {
                "changed_table": "raw_customers",
                "risk_level": "LOW",
                "directly_affected": [
                    {
                        "model": "dim_customers",
                        "risk": "low",
                    }
                ],
                "indirectly_affected": [],
                "total_affected": 1,
            }
        )

        self.assertEqual(signal.severity, Severity.LOW)
        self.assertEqual(signal.score, -5)
        self.assertEqual(signal.reasons, ["Downstream models affected"])

    def test_to_signal_preserves_affected_models(self):
        from agent.blast_radius import to_signal

        result = {
            "changed_table": "raw_customers",
            "risk_level": "HIGH",
            "directly_affected": [{"model": "fct_customer_lifetime_value"}],
            "indirectly_affected": [{"model": "dashboard_executive_metrics"}],
            "total_affected": 2,
        }

        signal = to_signal(result)

        self.assertEqual(
            signal.metadata["affected_models"],
            ["fct_customer_lifetime_value", "dashboard_executive_metrics"],
        )

    def test_to_signal_preserves_metadata(self):
        from agent.blast_radius import to_signal

        result = {
            "changed_table": "raw_customers",
            "changed_model": "raw_customers",
            "risk_level": "MEDIUM",
            "directly_affected": [{"model": "fct_customer_lifetime_value"}],
            "indirectly_affected": [],
            "total_affected": 1,
            "dashboard_count": 0,
            "blast_radius_score": 45,
            "dependency_depth": 1,
        }

        signal = to_signal(result)

        self.assertEqual(
            signal.metadata,
            {
                "changed_model": "raw_customers",
                "affected_models": ["fct_customer_lifetime_value"],
                "downstream_model_count": 1,
                "dashboard_count": 0,
                "blast_radius_score": 45,
                "dependency_depth": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
