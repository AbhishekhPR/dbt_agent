import json
import unittest

from agent.decision_engine import DeploymentDecision
from agent.decision_assembly import (
    assemble_decision_incident,
    assemble_pipeline_incident,
    summarize_decision_incident,
    summarize_pipeline_incident,
)
from agent.incident import Incident
from agent.signals import Severity, Signal


class DecisionAssemblyTests(unittest.TestCase):
    def test_assembles_incident_from_multiple_signals(self):
        signals = [
            Signal(
                component="metadata_checks",
                severity=Severity.HIGH,
                confidence=95,
                score=-30,
                reasons=["Duplicate count increased"],
            ),
            Signal(
                component="blast_radius",
                severity=Severity.MEDIUM,
                confidence=85,
                score=-15,
                reasons=["Downstream models affected"],
            ),
        ]

        incident = assemble_decision_incident(signals, incident_id="INC-0042")

        self.assertIsInstance(incident, Incident)
        self.assertEqual(incident.incident_id, "INC-0042")
        self.assertEqual(incident.signals, signals)

    def test_health_is_calculated_from_combined_scores(self):
        incident = assemble_decision_incident([
            Signal("metadata_checks", Severity.HIGH, 95, -30),
            Signal("blast_radius", Severity.MEDIUM, 85, -15),
        ])

        self.assertEqual(incident.health, 55)

    def test_decision_becomes_block_below_threshold(self):
        incident = assemble_decision_incident([
            Signal("metadata_checks", Severity.HIGH, 95, -30),
            Signal("metadata_drift", Severity.HIGH, 95, -35),
        ])

        self.assertEqual(incident.decision, DeploymentDecision.BLOCK)

    def test_confidence_averages_across_signals(self):
        incident = assemble_decision_incident([
            Signal("metadata_checks", Severity.HIGH, 95, -30),
            Signal("blast_radius", Severity.LOW, 75, -5),
        ])

        self.assertEqual(incident.confidence, 85)

    def test_affected_models_and_metadata_are_preserved(self):
        incident = assemble_decision_incident(
            [Signal("blast_radius", Severity.HIGH, 95, -25)],
            affected_models=["orders", "customers"],
            metadata={"source": "unit-test"},
        )

        self.assertEqual(incident.affected_models, ["orders", "customers"])
        self.assertEqual(incident.metadata, {"source": "unit-test"})

    def test_summary_dict_is_serializable(self):
        incident = assemble_decision_incident(
            [
                Signal(
                    "metadata_checks",
                    Severity.HIGH,
                    95,
                    -30,
                    reasons=["Duplicate count increased"],
                )
            ],
            incident_id="INC-0100",
            affected_models=["orders"],
        )

        summary = summarize_decision_incident(incident)

        json.dumps(summary)
        self.assertEqual(
            summary,
            {
                "incident_id": "INC-0100",
                "decision": DeploymentDecision.WARN.value,
                "health": 70,
                "severity": Severity.HIGH.value,
                "confidence": 95,
                "root_cause": "Duplicate count increased",
                "recommendation": (
                    "Review the flagged pipeline signals before deployment."
                ),
                "affected_models": ["orders"],
                "signal_count": 1,
                "top_reasons": ["Duplicate count increased"],
            },
        )

    def test_input_signals_are_not_mutated(self):
        signal = Signal(
            "metadata_checks",
            Severity.HIGH,
            95,
            -30,
            reasons=["Original reason"],
            metadata={"row_count": 4},
        )
        signals = [signal]
        original_reasons = list(signal.reasons)
        original_metadata = dict(signal.metadata)
        affected_models = ["orders"]
        metadata = {"source": "unit-test"}

        incident = assemble_decision_incident(
            signals,
            affected_models=affected_models,
            metadata=metadata,
        )
        incident.affected_models.append("customers")
        incident.metadata["source"] = "mutated"

        self.assertEqual(signals, [signal])
        self.assertEqual(signal.reasons, original_reasons)
        self.assertEqual(signal.metadata, original_metadata)
        self.assertEqual(affected_models, ["orders"])
        self.assertEqual(metadata, {"source": "unit-test"})

    def test_pipeline_assembly_combines_detector_signals(self):
        incident = assemble_pipeline_incident(
            metadata_signal=Signal(
                "metadata_checks",
                Severity.HIGH,
                95,
                -30,
                reasons=["Duplicate count increased"],
            ),
            drift_signal=Signal(
                "metadata_drift",
                Severity.HIGH,
                95,
                -35,
                reasons=["Duplicate customer_id increased 400%"],
            ),
            blast_radius_signal=Signal(
                "blast_radius",
                Severity.MEDIUM,
                85,
                -15,
                reasons=["Downstream models affected"],
            ),
            historical_reliability_signal=Signal(
                "historical_reliability",
                Severity.LOW,
                75,
                -5,
                reasons=["Historical reliability is strong"],
            ),
        )

        self.assertEqual(len(incident.signals), 4)
        self.assertEqual(incident.decision, DeploymentDecision.BLOCK)

    def test_pipeline_assembly_ignores_none_signals(self):
        metadata_signal = Signal("metadata_checks", Severity.HIGH, 95, -30)

        incident = assemble_pipeline_incident(
            metadata_signal=metadata_signal,
            drift_signal=None,
            blast_radius_signal=None,
        )

        self.assertEqual(incident.signals, [metadata_signal])

    def test_pipeline_assembly_preserves_signal_order(self):
        ast_signal = Signal("ast", Severity.HIGH, 95, -40)
        metadata_signal = Signal("metadata_checks", Severity.HIGH, 95, -30)
        drift_signal = Signal("metadata_drift", Severity.HIGH, 95, -35)
        blast_signal = Signal("blast_radius", Severity.MEDIUM, 85, -15)
        reliability_signal = Signal("historical_reliability", Severity.LOW, 75, -5)

        incident = assemble_pipeline_incident(
            historical_reliability_signal=reliability_signal,
            blast_radius_signal=blast_signal,
            drift_signal=drift_signal,
            metadata_signal=metadata_signal,
            ast_signal=ast_signal,
        )

        self.assertEqual(
            [signal.component for signal in incident.signals],
            [
                "ast",
                "metadata_checks",
                "metadata_drift",
                "blast_radius",
                "historical_reliability",
            ],
        )

    def test_pipeline_health_reflects_all_signal_scores(self):
        incident = assemble_pipeline_incident(
            ast_signal=Signal("ast", Severity.HIGH, 95, -40),
            metadata_signal=Signal("metadata_checks", Severity.HIGH, 95, -30),
            blast_radius_signal=Signal("blast_radius", Severity.MEDIUM, 85, -15),
        )

        self.assertEqual(incident.health, 15)

    def test_pipeline_decision_blocks_when_combined_health_is_low(self):
        incident = assemble_pipeline_incident(
            metadata_signal=Signal("metadata_checks", Severity.HIGH, 95, -30),
            drift_signal=Signal("metadata_drift", Severity.HIGH, 95, -35),
        )

        self.assertEqual(incident.decision, DeploymentDecision.BLOCK)

    def test_pipeline_summary_includes_signal_components(self):
        incident = assemble_pipeline_incident(
            metadata_signal=Signal(
                "metadata_checks",
                Severity.HIGH,
                95,
                -30,
                reasons=["Duplicate count increased"],
            ),
            blast_radius_signal=Signal(
                "blast_radius",
                Severity.MEDIUM,
                85,
                -15,
                reasons=["Downstream models affected"],
            ),
            incident_id="INC-0200",
        )

        summary = summarize_pipeline_incident(incident)

        json.dumps(summary)
        self.assertEqual(
            summary["signal_components"],
            ["metadata_checks", "blast_radius"],
        )
        self.assertEqual(summary["signal_count"], 2)
        self.assertEqual(summary["decision"], DeploymentDecision.BLOCK.value)

    def test_pipeline_affected_models_and_metadata_are_preserved(self):
        incident = assemble_pipeline_incident(
            metadata_signal=Signal("metadata_checks", Severity.HIGH, 95, -30),
            affected_models=["orders", "customers"],
            metadata={"source": "unit-test"},
        )

        self.assertEqual(incident.affected_models, ["orders", "customers"])
        self.assertEqual(incident.metadata, {"source": "unit-test"})

    def test_pipeline_inputs_are_not_mutated(self):
        metadata_signal = Signal(
            "metadata_checks",
            Severity.HIGH,
            95,
            -30,
            reasons=["Original reason"],
            metadata={"row_count": 4},
        )
        original_reasons = list(metadata_signal.reasons)
        original_metadata = dict(metadata_signal.metadata)
        affected_models = ["orders"]
        metadata = {"source": "unit-test"}

        incident = assemble_pipeline_incident(
            metadata_signal=metadata_signal,
            affected_models=affected_models,
            metadata=metadata,
        )
        incident.affected_models.append("customers")
        incident.metadata["source"] = "mutated"

        self.assertEqual(metadata_signal.reasons, original_reasons)
        self.assertEqual(metadata_signal.metadata, original_metadata)
        self.assertEqual(affected_models, ["orders"])
        self.assertEqual(metadata, {"source": "unit-test"})


if __name__ == "__main__":
    unittest.main()
