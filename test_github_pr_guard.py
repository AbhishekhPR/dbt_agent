import copy
import json
import unittest

from agent.decision_engine import DeploymentDecision
from agent.github_pr_guard import build_pr_review
from agent.incident import Incident
from agent.signals import Severity, Signal


def make_incident(
    *,
    decision=DeploymentDecision.BLOCK,
    health=42,
    severity=Severity.CRITICAL,
    confidence=91,
    root_cause="LEFT JOIN nullification detected",
    recommendation="Move right-side filters into JOIN clauses.",
    affected_models=None,
    signals=None,
):
    return Incident(
        incident_id="INC-PR-1",
        health=health,
        decision=decision,
        severity=severity,
        confidence=confidence,
        root_cause=root_cause,
        recommendation=recommendation,
        affected_models=(
            ["fct_orders", "dashboard_revenue"]
            if affected_models is None
            else affected_models
        ),
        signals=signals
        if signals is not None
        else [
            Signal(
                "ast",
                Severity.CRITICAL,
                95,
                -70,
                reasons=["Cross join detected"],
                metadata={"model_name": "fct_orders", "rule": "CROSS_JOIN"},
            ),
            Signal(
                "metadata_checks",
                Severity.HIGH,
                85,
                -30,
                reasons=["Duplicate count increased"],
                metadata={"model_name": "dashboard_revenue", "duplicate_count": 7},
            ),
        ],
        metadata={"source": "unit-test"},
    )


class GithubPrGuardTests(unittest.TestCase):
    def test_block_reviews_render_correctly(self):
        review = build_pr_review(make_incident())

        self.assertEqual(review["title"], "Relium AI Deployment Review")
        self.assertEqual(review["deployment_decision"], "BLOCK")
        self.assertEqual(review["pipeline_health"], "42 / 100")
        self.assertEqual(review["health"], 42)
        self.assertEqual(review["confidence"], "91%")
        self.assertEqual(review["confidence_percent"], 91)
        self.assertEqual(review["highest_severity"], "CRITICAL")
        self.assertEqual(
            review["primary_root_cause"],
            "LEFT JOIN nullification detected",
        )
        self.assertIn("Deployment is blocked because", review["executive_summary"])
        self.assertEqual(
            review["recommendation"],
            "Move right-side filters into JOIN clauses.",
        )
        self.assertEqual(review["models_reviewed"], 2)
        self.assertEqual(
            [signal["component"] for signal in review["signals_considered"]],
            ["ast", "metadata_checks"],
        )

    def test_warn_reviews_render_correctly(self):
        review = build_pr_review(
            make_incident(
                decision=DeploymentDecision.WARN,
                health=78,
                severity=Severity.MEDIUM,
                confidence=84,
                root_cause="Moderate drift detected",
            )
        )

        self.assertEqual(review["deployment_decision"], "WARN")
        self.assertEqual(review["pipeline_health"], "78 / 100")
        self.assertEqual(review["confidence"], "84%")
        self.assertEqual(review["highest_severity"], "MEDIUM")
        self.assertIn(
            "Deployment should proceed with caution",
            review["executive_summary"],
        )

    def test_allow_reviews_render_correctly(self):
        review = build_pr_review(
            make_incident(
                decision=DeploymentDecision.ALLOW,
                health=99,
                severity=Severity.LOW,
                confidence=76,
                root_cause="",
                recommendation="Deploy normally.",
                affected_models=["fct_orders"],
                signals=[],
            )
        )

        self.assertEqual(review["deployment_decision"], "ALLOW")
        self.assertEqual(review["pipeline_health"], "99 / 100")
        self.assertEqual(review["confidence"], "76%")
        self.assertEqual(review["highest_severity"], "LOW")
        self.assertEqual(review["models_reviewed"], 1)
        self.assertIn("Deployment is allowed", review["executive_summary"])
        self.assertEqual(review["evidence"], [])

    def test_multiple_models_are_counted_correctly(self):
        incident = make_incident(
            affected_models=["fct_orders", "fct_orders", "dashboard_revenue"],
            signals=[
                Signal(
                    "ast",
                    Severity.HIGH,
                    95,
                    -40,
                    metadata={"model_name": "fct_orders"},
                ),
                Signal(
                    "blast_radius",
                    Severity.MEDIUM,
                    85,
                    -15,
                    metadata={"model_name": "stg_customers"},
                ),
            ],
        )

        review = build_pr_review(incident)

        self.assertEqual(review["models_reviewed"], 3)
        self.assertEqual(
            review["model_names"],
            ["fct_orders", "dashboard_revenue", "stg_customers"],
        )

    def test_evidence_ordering_is_deterministic(self):
        incident = make_incident(
            signals=[
                Signal("metadata_checks", Severity.HIGH, 95, -30, reasons=["A", "B"]),
                Signal("blast_radius", Severity.MEDIUM, 85, -15, reasons=["C"]),
            ]
        )

        first = build_pr_review(incident)
        second = build_pr_review(incident)

        self.assertEqual(first["evidence"], second["evidence"])
        self.assertEqual(
            [item["title"] for item in first["evidence"]],
            [
                "Metadata Checks: A",
                "Metadata Checks: B",
                "Blast Radius: C",
            ],
        )

    def test_recommendation_is_preserved(self):
        review = build_pr_review(
            make_incident(recommendation="Escalate to analytics owner.")
        )

        self.assertEqual(review["recommendation"], "Escalate to analytics owner.")

    def test_serialization_succeeds(self):
        review = build_pr_review(make_incident())

        serialized = json.dumps(review)

        self.assertIsInstance(serialized, str)

    def test_incident_is_never_mutated(self):
        incident = make_incident()
        before = copy.deepcopy(incident)

        review = build_pr_review(incident)
        review["signals_considered"][0]["metadata"]["model_name"] = "mutated"
        review["evidence"][0]["supporting_metadata"]["model_name"] = "mutated"

        self.assertEqual(incident, before)


if __name__ == "__main__":
    unittest.main()
