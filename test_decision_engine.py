import unittest

from agent.decision_engine import Decision, DeploymentDecision, evaluate
from agent.signals import Severity, Signal


class DecisionEngineTests(unittest.TestCase):
    def test_evaluate_exists(self):
        self.assertTrue(callable(evaluate))

    def test_health_calculation_adds_signal_scores_and_clamps(self):
        signals = [
            Signal(
                component="ast",
                severity="HIGH",
                confidence=90,
                score=-40,
                reasons=["LEFT JOIN nullification detected"],
            ),
            Signal(
                component="metadata",
                severity="MEDIUM",
                confidence=80,
                score=-150,
                reasons=["Row count dropped"],
            ),
        ]

        decision = evaluate(signals)

        self.assertIsInstance(decision, Decision)
        self.assertEqual(decision.health, 0)

    def test_decision_thresholds(self):
        self.assertEqual(evaluate([]).decision, DeploymentDecision.ALLOW)
        self.assertEqual(
            evaluate([
                Signal("ast", Severity.LOW, 90, -10),
            ]).decision,
            DeploymentDecision.ALLOW,
        )
        self.assertEqual(
            evaluate([
                Signal("ast", Severity.MEDIUM, 90, -30),
            ]).decision,
            DeploymentDecision.WARN,
        )
        self.assertEqual(
            evaluate([
                Signal("ast", Severity.HIGH, 90, -31),
            ]).decision,
            DeploymentDecision.BLOCK,
        )

    def test_confidence_averaging(self):
        decision = evaluate([
            Signal("ast", "HIGH", 95, -20),
            Signal("metadata", "LOW", 85, 0),
        ])

        self.assertEqual(decision.confidence, 90)

    def test_highest_severity_selection(self):
        decision = evaluate([
            Signal("metadata", "LOW", 80, 0),
            Signal("ast", "CRITICAL", 70, -50),
            Signal("metadata_drift", "HIGH", 90, -20),
        ])

        self.assertEqual(decision.severity, Severity.CRITICAL)

    def test_reason_aggregation(self):
        decision = evaluate([
            Signal("ast", "HIGH", 95, -20, reasons=["left join issue"]),
            Signal("metadata", "MEDIUM", 80, -10, reasons=["row count changed"]),
        ])

        self.assertEqual(decision.reasons, ["left join issue", "row count changed"])
        self.assertEqual(len(decision.signals), 2)


if __name__ == "__main__":
    unittest.main()
