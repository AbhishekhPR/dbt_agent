import unittest

from agent.decision_engine import DeploymentDecision, evaluate
from agent.incident import Incident, create_incident
from agent.signals import Severity, Signal


class IncidentTests(unittest.TestCase):
    def test_incident_stores_all_fields(self):
        signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["LEFT JOIN nullification detected"],
            metadata={"rule": "LEFT_JOIN_NULLIFIED"},
        )
        incident = Incident(
            incident_id="INC-1234",
            health=60,
            decision=DeploymentDecision.BLOCK,
            severity=Severity.HIGH,
            confidence=95,
            root_cause="LEFT JOIN filter moved into WHERE clause",
            recommendation="Move right-side filter into JOIN ON clause.",
            affected_models=["fct_revenue"],
            signals=[signal],
            metadata={"owner": "analytics"},
        )

        self.assertEqual(incident.incident_id, "INC-1234")
        self.assertEqual(incident.health, 60)
        self.assertEqual(incident.decision, DeploymentDecision.BLOCK)
        self.assertEqual(incident.severity, Severity.HIGH)
        self.assertEqual(incident.confidence, 95)
        self.assertEqual(
            incident.root_cause,
            "LEFT JOIN filter moved into WHERE clause",
        )
        self.assertEqual(
            incident.recommendation,
            "Move right-side filter into JOIN ON clause.",
        )
        self.assertEqual(incident.affected_models, ["fct_revenue"])
        self.assertEqual(incident.signals, [signal])
        self.assertEqual(incident.metadata, {"owner": "analytics"})

    def test_affected_models_defaults_to_empty_list(self):
        incident = Incident(
            incident_id="INC-0001",
            health=100,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=0,
            root_cause="",
            recommendation="",
        )

        self.assertEqual(incident.affected_models, [])

    def test_metadata_defaults_to_empty_dict(self):
        incident = Incident(
            incident_id="INC-0001",
            health=100,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=0,
            root_cause="",
            recommendation="",
        )

        self.assertEqual(incident.metadata, {})

    def test_create_incident_copies_decision_fields(self):
        signal = Signal(
            component="metadata",
            severity=Severity.HIGH,
            confidence=90,
            score=-40,
            reasons=["Row count dropped"],
        )
        decision = evaluate([signal])

        incident = create_incident(
            decision,
            incident_id="INC-0007",
            root_cause="Row count drop",
            recommendation="Review upstream load.",
            affected_models=["stg_orders"],
            metadata={"source": "unit-test"},
        )

        self.assertEqual(incident.incident_id, "INC-0007")
        self.assertEqual(incident.health, decision.health)
        self.assertEqual(incident.decision, decision.decision)
        self.assertEqual(incident.severity, decision.severity)
        self.assertEqual(incident.confidence, decision.confidence)
        self.assertEqual(incident.root_cause, "Row count drop")
        self.assertEqual(incident.recommendation, "Review upstream load.")
        self.assertEqual(incident.affected_models, ["stg_orders"])
        self.assertEqual(incident.metadata, {"source": "unit-test"})

    def test_create_incident_preserves_signal_objects(self):
        signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
        )
        decision = evaluate([signal])

        incident = create_incident(decision)

        self.assertIs(incident.signals[0], signal)

    def test_mutable_defaults_are_not_shared(self):
        first = Incident(
            incident_id="INC-0001",
            health=100,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=0,
            root_cause="",
            recommendation="",
        )
        second = Incident(
            incident_id="INC-0002",
            health=100,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=0,
            root_cause="",
            recommendation="",
        )

        first.affected_models.append("orders")
        first.signals.append(Signal("ast", Severity.LOW, 80, 0))
        first.metadata["owner"] = "analytics"

        self.assertEqual(second.affected_models, [])
        self.assertEqual(second.signals, [])
        self.assertEqual(second.metadata, {})


if __name__ == "__main__":
    unittest.main()
