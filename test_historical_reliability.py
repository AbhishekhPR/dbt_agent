import unittest

from agent.historical_reliability import evaluate_history, to_signal
from agent.signals import Severity, Signal


class HistoricalReliabilityTests(unittest.TestCase):
    def test_healthy_history_scores_low_severity(self):
        result = evaluate_history(
            {
                "deployment_count": 20,
                "incident_count": 0,
                "rollback_count": 0,
                "average_health_score": 95,
            }
        )

        self.assertEqual(result["score"], 100)
        self.assertEqual(result["severity"], "LOW")
        self.assertEqual(result["confidence"], 95)
        self.assertEqual(result["reasons"], ["Historical reliability is strong"])

    def test_repeated_incidents_score_high_severity(self):
        result = evaluate_history(
            {
                "deployment_count": 6,
                "incident_count": 4,
                "rollback_count": 0,
                "average_health_score": 92,
            }
        )

        self.assertEqual(result["severity"], "HIGH")
        self.assertIn("Repeated incidents detected", result["reasons"])

    def test_repeated_rollbacks_score_high_severity(self):
        result = evaluate_history(
            {
                "deployment_count": 8,
                "incident_count": 0,
                "rollback_count": 3,
                "average_health_score": 90,
            }
        )

        self.assertEqual(result["severity"], "HIGH")
        self.assertIn("Repeated rollbacks detected", result["reasons"])

    def test_score_calculation(self):
        result = evaluate_history(
            {
                "deployment_count": 8,
                "incident_count": 2,
                "rollback_count": 1,
                "average_health_score": 88,
            }
        )

        self.assertEqual(result["score"], 55)

    def test_severity_thresholds(self):
        self.assertEqual(
            evaluate_history(
                {
                    "deployment_count": 0,
                    "incident_count": 0,
                    "rollback_count": 0,
                    "average_health_score": 85,
                }
            )["severity"],
            "LOW",
        )
        self.assertEqual(
            evaluate_history(
                {
                    "deployment_count": 0,
                    "incident_count": 0,
                    "rollback_count": 0,
                    "average_health_score": 70,
                }
            )["severity"],
            "MEDIUM",
        )
        self.assertEqual(
            evaluate_history(
                {
                    "deployment_count": 0,
                    "incident_count": 0,
                    "rollback_count": 0,
                    "average_health_score": 69,
                }
            )["severity"],
            "HIGH",
        )

    def test_metadata_preservation(self):
        result = evaluate_history(
            {
                "deployment_count": 10,
                "incident_count": 2,
                "rollback_count": 1,
                "average_health_score": 90,
                "model_name": "fct_orders",
            }
        )

        self.assertEqual(
            result["metadata"],
            {
                "deployment_count": 10,
                "incident_count": 2,
                "rollback_count": 1,
                "average_health_score": 90,
                "incident_rate": 0.2,
                "rollback_rate": 0.1,
                "model_name": "fct_orders",
            },
        )

    def test_to_signal_converts_evaluation_result(self):
        result = evaluate_history(
            {
                "deployment_count": 8,
                "incident_count": 2,
                "rollback_count": 1,
                "average_health_score": 88,
            }
        )

        signal = to_signal(result)

        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.component, "historical_reliability")
        self.assertEqual(signal.severity, Severity.HIGH)
        self.assertEqual(signal.confidence, result["confidence"])
        self.assertEqual(signal.score, -45)
        self.assertEqual(signal.reasons, result["reasons"])
        self.assertEqual(signal.metadata, result["metadata"])


if __name__ == "__main__":
    unittest.main()
