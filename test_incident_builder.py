import unittest

from agent.decision_engine import DeploymentDecision, evaluate
from agent.incident import Incident
from agent.incident_builder import build_incident, summarize_incident
from agent.signals import Severity, Signal


class IncidentBuilderTests(unittest.TestCase):
    def test_builds_incident_from_decision(self):
        signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["LEFT JOIN nullification detected"],
        )
        decision = evaluate([signal])

        incident = build_incident(
            decision,
            incident_id="INC-0042",
            root_cause="LEFT JOIN issue",
            recommendation="Move filter into JOIN.",
        )

        self.assertIsInstance(incident, Incident)
        self.assertEqual(incident.incident_id, "INC-0042")
        self.assertEqual(incident.health, decision.health)
        self.assertEqual(incident.decision, decision.decision)
        self.assertEqual(incident.severity, decision.severity)
        self.assertEqual(incident.confidence, decision.confidence)
        self.assertEqual(incident.root_cause, "LEFT JOIN issue")
        self.assertEqual(incident.recommendation, "Move filter into JOIN.")
        self.assertIs(incident.signals[0], signal)

    def test_derives_root_cause_from_highest_severity_signal(self):
        low_signal = Signal(
            component="metadata",
            severity=Severity.LOW,
            confidence=80,
            score=-5,
            reasons=["Low-risk metadata warning"],
        )
        high_signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["High-risk AST finding", "Second AST reason"],
        )
        decision = evaluate([low_signal, high_signal])

        incident = build_incident(decision)

        self.assertEqual(incident.root_cause, "High-risk AST finding")

    def test_uses_fallback_recommendation(self):
        decision = evaluate([
            Signal("ast", Severity.MEDIUM, 80, -20, reasons=["Medium issue"])
        ])

        incident = build_incident(decision)

        self.assertEqual(
            incident.recommendation,
            "Review the flagged pipeline signals before deployment.",
        )

    def test_preserves_affected_models(self):
        decision = evaluate([])

        incident = build_incident(
            decision,
            affected_models=["orders", "customers"],
        )

        self.assertEqual(incident.affected_models, ["orders", "customers"])

    def test_copies_metadata_safely(self):
        decision = evaluate([])
        metadata = {"source": "unit-test"}

        incident = build_incident(decision, metadata=metadata)
        metadata["source"] = "mutated"

        self.assertEqual(incident.metadata, {"source": "unit-test"})

    def test_summarize_incident_returns_serializable_dict(self):
        signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["First reason", "Second reason"],
        )
        incident = build_incident(
            evaluate([signal]),
            incident_id="INC-0099",
            affected_models=["fct_revenue"],
            metadata={"source": "unit-test"},
        )

        summary = summarize_incident(incident)

        self.assertEqual(
            summary,
            {
                "incident_id": "INC-0099",
                "decision": DeploymentDecision.BLOCK.value,
                "health": 60,
                "severity": Severity.HIGH.value,
                "confidence": 95,
                "root_cause": "First reason",
                "recommendation": (
                    "Review the flagged pipeline signals before deployment."
                ),
                "affected_models": ["fct_revenue"],
                "signal_count": 1,
                "top_reasons": ["First reason", "Second reason"],
            },
        )

    def test_does_not_mutate_input_objects(self):
        signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["Original reason"],
            metadata={"rule": "LEFT_JOIN_NULLIFIED"},
        )
        decision = evaluate([signal])
        original_decision_signals = list(decision.signals)
        original_signal_reasons = list(signal.reasons)
        affected_models = ["orders"]
        metadata = {"source": "unit-test"}

        incident = build_incident(
            decision,
            affected_models=affected_models,
            metadata=metadata,
        )
        incident.affected_models.append("customers")
        incident.metadata["source"] = "mutated"

        self.assertEqual(decision.signals, original_decision_signals)
        self.assertEqual(signal.reasons, original_signal_reasons)
        self.assertEqual(affected_models, ["orders"])
        self.assertEqual(metadata, {"source": "unit-test"})


if __name__ == "__main__":
    unittest.main()
