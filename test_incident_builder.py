import copy
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

    def test_semantic_diff_reason_becomes_primary_root_cause_when_present(self):
        semantic_diff = Signal(
            component="semantic_diff",
            severity=Severity.MEDIUM,
            confidence=92,
            score=-35,
            reasons=["Revenue gained upstream dependency refunds"],
        )
        generic_kpi = Signal(
            component="kpi_impact",
            severity=Severity.CRITICAL,
            confidence=95,
            score=-30,
            reasons=["Revenue is impacted by fct_revenue"],
        )
        decision = evaluate([generic_kpi, semantic_diff])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue gained upstream dependency refunds",
        )

    def test_upstream_dependency_semantic_diff_reason_beats_related_model_reason(self):
        semantic_diff = Signal(
            component="semantic_diff",
            severity=Severity.HIGH,
            confidence=92,
            score=-35,
            reasons=[
                "Revenue gained related model stg_refunds",
                "Revenue gained upstream dependency refunds",
            ],
        )
        decision = evaluate([semantic_diff])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue gained upstream dependency refunds",
        )

    def test_upstream_dependency_semantic_diff_reason_beats_downstream_consumer_reason(self):
        semantic_diff = Signal(
            component="semantic_diff",
            severity=Severity.HIGH,
            confidence=92,
            score=-35,
            reasons=[
                "Revenue gained downstream consumer revenue_dashboard",
                "Revenue gained upstream dependency refunds",
            ],
        )
        decision = evaluate([semantic_diff])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue gained upstream dependency refunds",
        )

    def test_invariant_removal_semantic_diff_reason_beats_upstream_dependency_reason(self):
        semantic_diff = Signal(
            component="semantic_diff",
            severity=Severity.HIGH,
            confidence=92,
            score=-35,
            reasons=[
                "Revenue gained upstream dependency refunds",
                "Revenue lost invariant never negative",
            ],
        )
        decision = evaluate([semantic_diff])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue lost invariant never negative",
        )

    def test_semantic_contract_reason_is_chosen_when_semantic_diff_absent(self):
        semantic_contract = Signal(
            component="semantic_contract",
            severity=Severity.MEDIUM,
            confidence=88,
            score=-20,
            reasons=["Revenue violated invariant never negative"],
        )
        generic_kpi = Signal(
            component="kpi_impact",
            severity=Severity.HIGH,
            confidence=90,
            score=-30,
            reasons=["Revenue is impacted by fct_revenue"],
        )
        decision = evaluate([generic_kpi, semantic_contract])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue violated invariant never negative",
        )

    def test_kpi_impact_reason_is_chosen_when_higher_semantic_signals_absent(self):
        generic_kpi = Signal(
            component="kpi_impact",
            severity=Severity.MEDIUM,
            confidence=88,
            score=-15,
            reasons=["Revenue is impacted through stg_orders -> fct_revenue -> Revenue"],
        )
        ast_signal = Signal(
            component="ast",
            severity=Severity.CRITICAL,
            confidence=95,
            score=-40,
            reasons=["Cross join detected"],
        )
        decision = evaluate([ast_signal, generic_kpi])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
        )

    def test_low_level_kpi_discovery_matches_are_never_root_cause(self):
        generic_kpi = Signal(
            component="kpi_impact",
            severity=Severity.HIGH,
            confidence=88,
            score=-15,
            reasons=[
                "dbt_metrics value Revenue matched KPI concept Revenue/ GMV",
                "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
            ],
        )
        decision = evaluate([generic_kpi])

        incident = build_incident(decision)

        self.assertEqual(
            incident.root_cause,
            "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
        )
        self.assertNotIn("matched KPI concept", incident.root_cause)

    def test_existing_non_semantic_root_cause_behavior_remains_unchanged(self):
        metadata_signal = Signal(
            component="metadata_checks",
            severity=Severity.MEDIUM,
            confidence=85,
            score=-15,
            reasons=["Duplicate count increased"],
        )
        ast_signal = Signal(
            component="ast",
            severity=Severity.HIGH,
            confidence=95,
            score=-40,
            reasons=["LEFT JOIN nullification detected"],
        )
        decision = evaluate([metadata_signal, ast_signal])

        incident = build_incident(decision)

        self.assertEqual(incident.root_cause, "LEFT JOIN nullification detected")

    def test_root_cause_prioritization_does_not_mutate_input_signals(self):
        signals = [
            Signal(
                component="kpi_impact",
                severity=Severity.HIGH,
                confidence=88,
                score=-15,
                reasons=[
                    "dbt_metrics value Revenue matched KPI concept Revenue/ GMV",
                    "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
                ],
                metadata={"kpi": "Revenue"},
            )
        ]
        before = copy.deepcopy(signals)

        build_incident(evaluate(signals))

        self.assertEqual(signals, before)

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
