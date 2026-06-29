import copy
import json
import unittest
from dataclasses import asdict

from agent.decision_engine import DeploymentDecision
from agent.incident import Incident
from agent.reasoning_engine import (
    Evidence,
    ReasoningReport,
    build_reasoning_report,
)
from agent.signals import Severity, Signal


def make_incident(
    *,
    decision=DeploymentDecision.BLOCK,
    health=55,
    severity=Severity.HIGH,
    confidence=90,
    recommendation="Review the flagged pipeline signals before deployment.",
    signals=None,
):
    return Incident(
        incident_id="INC-0042",
        health=health,
        decision=decision,
        severity=severity,
        confidence=confidence,
        root_cause="Duplicate count increased",
        recommendation=recommendation,
        affected_models=["fct_orders"],
        signals=signals
        if signals is not None
        else [
            Signal(
                "metadata_checks",
                Severity.HIGH,
                95,
                -30,
                reasons=["Duplicate count increased"],
                metadata={"duplicate_count": 5},
            )
        ],
        metadata={"scenario": "duplicate-spike"},
    )


class ReasoningEngineTests(unittest.TestCase):
    def test_block_decision_executive_summary_and_conclusion(self):
        report = build_reasoning_report(make_incident())

        self.assertIsInstance(report, ReasoningReport)
        self.assertIn("Deployment is blocked because", report.executive_summary)
        self.assertIn("55 / 100", report.executive_summary)
        self.assertIn("HIGH", report.executive_summary)
        self.assertIn("90%", report.executive_summary)
        self.assertNotIn("Deployment BLOCK was blocked", report.executive_summary)
        self.assertIn("BLOCK DEPLOYMENT", report.conclusion)
        self.assertIn("multiple reliability signals", report.conclusion)

    def test_warn_decision_executive_summary(self):
        incident = make_incident(
            decision=DeploymentDecision.WARN,
            health=78,
            severity=Severity.MEDIUM,
            confidence=85,
            signals=[
                Signal(
                    "blast_radius",
                    Severity.MEDIUM,
                    85,
                    -15,
                    reasons=["Downstream models affected"],
                )
            ],
        )

        report = build_reasoning_report(incident)

        self.assertIn("Deployment should proceed with caution", report.executive_summary)
        self.assertIn("WARN", report.executive_summary)
        self.assertIn("78 / 100", report.conclusion)

    def test_allow_decision_executive_summary(self):
        incident = make_incident(
            decision=DeploymentDecision.ALLOW,
            health=96,
            severity=Severity.LOW,
            confidence=75,
            signals=[],
        )

        report = build_reasoning_report(incident)

        self.assertIn("Deployment is allowed", report.executive_summary)
        self.assertIn("ALLOW", report.executive_summary)
        self.assertEqual(report.evidence, [])

    def test_multiple_signal_reasons_become_multiple_evidence_items(self):
        incident = make_incident(
            signals=[
                Signal(
                    "ast",
                    Severity.HIGH,
                    95,
                    -40,
                    reasons=[
                        "LEFT JOIN nullification detected",
                        "Risky WHERE filter detected",
                    ],
                    metadata={"rule": "LEFT_JOIN_WHERE"},
                ),
                Signal(
                    "metadata_drift",
                    Severity.MEDIUM,
                    85,
                    -20,
                    reasons=["Row count changed unexpectedly"],
                    metadata={"row_count_change_pct": 200},
                ),
            ],
        )

        report = build_reasoning_report(incident)

        self.assertEqual(len(report.evidence), 3)
        self.assertEqual(
            [item.title for item in report.evidence],
            [
                "AST: LEFT JOIN nullification detected",
                "AST: Risky WHERE filter detected",
                "Metadata Drift: Row count changed unexpectedly",
            ],
        )
        self.assertEqual(report.evidence[0].supporting_metadata, {"rule": "LEFT_JOIN_WHERE"})

    def test_signal_without_reason_still_becomes_evidence(self):
        incident = make_incident(
            signals=[
                Signal(
                    "historical_reliability",
                    Severity.LOW,
                    75,
                    -5,
                    metadata={"deployment_count": 20},
                )
            ],
        )

        report = build_reasoning_report(incident)

        self.assertEqual(len(report.evidence), 1)
        self.assertEqual(
            report.evidence[0].title,
            "Historical Reliability: Signal detected",
        )
        self.assertEqual(
            report.evidence[0].supporting_metadata,
            {"deployment_count": 20},
        )

    def test_evidence_order_is_deterministic(self):
        signals = [
            Signal("metadata_checks", Severity.HIGH, 95, -30, reasons=["A", "B"]),
            Signal("blast_radius", Severity.MEDIUM, 85, -15, reasons=["C"]),
        ]
        incident = make_incident(signals=signals)

        first = build_reasoning_report(incident)
        second = build_reasoning_report(incident)

        self.assertEqual(first, second)
        self.assertEqual(
            [item.title for item in first.evidence],
            [
                "Metadata Checks: A",
                "Metadata Checks: B",
                "Blast Radius: C",
            ],
        )

    def test_report_is_serializable(self):
        report = build_reasoning_report(make_incident())

        payload = asdict(report)
        serialized = json.dumps(payload)

        self.assertIsInstance(serialized, str)
        self.assertIsInstance(report.evidence[0], Evidence)
        self.assertEqual(payload["evidence"][0]["severity"], "HIGH")

    def test_uses_incident_recommendation_or_fallback(self):
        with_recommendation = build_reasoning_report(
            make_incident(recommendation="Investigate duplicate keys.")
        )
        without_recommendation = build_reasoning_report(
            make_incident(recommendation="")
        )

        self.assertEqual(
            with_recommendation.recommendation,
            "Investigate duplicate keys.",
        )
        self.assertEqual(
            without_recommendation.recommendation,
            "Review the deployment evidence before proceeding.",
        )

    def test_does_not_recompute_or_change_decision_fields(self):
        incident = make_incident(
            decision=DeploymentDecision.ALLOW,
            health=10,
            severity=Severity.LOW,
            confidence=12,
        )

        report = build_reasoning_report(incident)

        self.assertIn("ALLOW", report.executive_summary)
        self.assertIn("10 / 100", report.executive_summary)
        self.assertIn("LOW", report.executive_summary)
        self.assertIn("12%", report.executive_summary)

    def test_does_not_mutate_incident(self):
        incident = make_incident()
        before = copy.deepcopy(incident)

        build_reasoning_report(incident)

        self.assertEqual(incident, before)


if __name__ == "__main__":
    unittest.main()
