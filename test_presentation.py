import copy
import json
import unittest

from agent.decision_engine import DeploymentDecision
from agent.incident import Incident
from agent.presentation import render_cli, render_json, render_markdown
from agent.signals import Severity, Signal


def make_incident():
    return Incident(
        incident_id="INC-0042",
        health=62,
        decision=DeploymentDecision.BLOCK,
        severity=Severity.HIGH,
        confidence=91,
        root_cause="LEFT JOIN nullification detected",
        recommendation="Move right-side filters into JOIN clauses.",
        affected_models=["fct_customer_lifetime_value"],
        signals=[
            Signal(
                component="ast",
                severity=Severity.HIGH,
                confidence=95,
                score=-40,
                reasons=["LEFT JOIN nullification detected"],
                metadata={"rule": "LEFT_JOIN_WHERE"},
            ),
            Signal(
                component="metadata_checks",
                severity=Severity.MEDIUM,
                confidence=85,
                score=-15,
                reasons=["Duplicate count increased"],
                metadata={"duplicate_count": 7},
            ),
        ],
        metadata={"scenario": "duplicate-spike"},
    )


class PresentationTests(unittest.TestCase):
    def test_cli_contains_every_major_section(self):
        rendered = render_cli(make_incident())

        self.assertIn("Relium Deployment Decision", rendered)
        self.assertIn("Pipeline Health: 62 / 100", rendered)
        self.assertIn("Deployment Decision: BLOCK DEPLOYMENT", rendered)
        self.assertIn("Severity: HIGH", rendered)
        self.assertIn("Confidence: 91%", rendered)
        self.assertIn("Primary Root Cause:", rendered)
        self.assertIn("Top Reasons:", rendered)
        self.assertIn("Recommendation:", rendered)
        self.assertIn("Signals Considered:", rendered)
        self.assertIn("Affected Models:", rendered)
        self.assertIn("- ast", rendered)
        self.assertIn("- metadata_checks", rendered)

    def test_cli_includes_reasoning_section(self):
        rendered = render_cli(make_incident())

        self.assertIn("Reasoning:", rendered)
        self.assertIn("Executive Summary:", rendered)
        self.assertIn("Deployment is blocked because", rendered)
        self.assertNotIn("Deployment BLOCK was blocked", rendered)
        self.assertIn("Conclusion:", rendered)
        self.assertIn("multiple reliability signals", rendered)
        self.assertIn("Recommendation:", rendered)

    def test_cli_includes_evidence_section(self):
        rendered = render_cli(make_incident())

        self.assertIn("Evidence:", rendered)
        self.assertIn("- AST: LEFT JOIN nullification detected", rendered)
        self.assertIn("- Metadata Checks: Duplicate count increased", rendered)

    def test_cli_reasoning_spacing_separates_joined_words(self):
        incident = Incident(
            incident_id="INC-SPACING",
            health=25,
            decision=DeploymentDecision.BLOCK,
            severity=Severity.HIGH,
            confidence=88,
            root_cause="Rowswith unexpected duplication.",
            recommendation="Review rows silently.Rows should not be joined.",
            signals=[
                Signal(
                    component="metadata_checks",
                    severity=Severity.HIGH,
                    confidence=88,
                    score=-30,
                    reasons=["Rowswith unexpected duplication silently.Rows changed"],
                )
            ],
        )

        rendered = render_cli(incident)

        self.assertNotIn("Rowswith", rendered)
        self.assertNotIn("silently.Rows", rendered)
        self.assertIn("Rows with unexpected duplication", rendered)
        self.assertIn("silently. Rows changed", rendered)
        self.assertEqual(
            incident.signals[0].reasons,
            ["Rowswith unexpected duplication silently.Rows changed"],
        )

    def test_markdown_contains_every_section(self):
        rendered = render_markdown(make_incident())

        self.assertIn("# Relium Deployment Decision", rendered)
        self.assertIn("## Pipeline Health", rendered)
        self.assertIn("62 / 100", rendered)
        self.assertIn("## Deployment Decision", rendered)
        self.assertIn("BLOCK DEPLOYMENT", rendered)
        self.assertIn("## Severity", rendered)
        self.assertIn("HIGH", rendered)
        self.assertIn("## Confidence", rendered)
        self.assertIn("91%", rendered)
        self.assertIn("## Primary Root Cause", rendered)
        self.assertIn("## Top Reasons", rendered)
        self.assertIn("## Recommendation", rendered)
        self.assertIn("## Signals Considered", rendered)
        self.assertIn("## Affected Models", rendered)

    def test_markdown_includes_reasoning_section(self):
        rendered = render_markdown(make_incident())

        self.assertIn("## Reasoning", rendered)
        self.assertIn("### Executive Summary", rendered)
        self.assertIn("Deployment is blocked because", rendered)
        self.assertNotIn("Deployment BLOCK was blocked", rendered)
        self.assertIn("### Evidence", rendered)
        self.assertIn("- **AST: LEFT JOIN nullification detected**", rendered)
        self.assertIn(
            "- **Metadata Checks: Duplicate count increased**",
            rendered,
        )
        self.assertIn("### Conclusion", rendered)
        self.assertIn("multiple reliability signals", rendered)
        self.assertIn("### Recommendation", rendered)

    def test_decision_labels_render_for_warn_and_allow(self):
        warn_incident = Incident(
            incident_id="INC-WARN",
            health=88,
            decision=DeploymentDecision.WARN,
            severity=Severity.MEDIUM,
            confidence=88,
            root_cause="Moderate drift detected",
            recommendation="Review before deployment.",
        )
        allow_incident = Incident(
            incident_id="INC-ALLOW",
            health=98,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=88,
            root_cause="",
            recommendation="",
        )

        self.assertIn("Deployment Decision: WARN", render_cli(warn_incident))
        self.assertIn("Deployment Decision: ALLOW", render_cli(allow_incident))
        self.assertIn("Confidence: 88%", render_cli(allow_incident))
        self.assertIn("98 / 100", render_markdown(allow_incident))

    def test_json_is_fully_serializable(self):
        payload = render_json(make_incident())

        serialized = json.dumps(payload)

        self.assertIsInstance(serialized, str)
        self.assertEqual(payload["incident_id"], "INC-0042")
        self.assertEqual(payload["signal_count"], 2)
        self.assertEqual(
            payload["signal_components"],
            ["ast", "metadata_checks"],
        )
        self.assertEqual(
            payload["top_reasons"],
            [
                "LEFT JOIN nullification detected",
                "Duplicate count increased",
            ],
        )
        self.assertEqual(payload["metadata"], {"scenario": "duplicate-spike"})

    def test_empty_lists_are_handled_gracefully(self):
        incident = Incident(
            incident_id="INC-EMPTY",
            health=100,
            decision=DeploymentDecision.ALLOW,
            severity=Severity.LOW,
            confidence=0,
            root_cause="",
            recommendation="",
        )

        cli = render_cli(incident)
        markdown = render_markdown(incident)
        payload = render_json(incident)

        self.assertIn("Top Reasons:", cli)
        self.assertIn("- None", cli)
        self.assertIn("Signals Considered:", cli)
        self.assertNotIn("Affected Models:", cli)
        self.assertIn("## Top Reasons", markdown)
        self.assertIn("- None", markdown)
        self.assertIn("## Signals Considered", markdown)
        self.assertNotIn("## Affected Models", markdown)
        self.assertEqual(payload["signal_count"], 0)
        self.assertEqual(payload["signal_components"], [])
        self.assertEqual(payload["top_reasons"], [])
        self.assertEqual(payload["affected_models"], [])

    def test_enum_serialization_uses_values(self):
        payload = render_json(make_incident())

        self.assertEqual(payload["decision"], "BLOCK")
        self.assertEqual(payload["severity"], "HIGH")

    def test_rendering_never_mutates_incident(self):
        incident = make_incident()
        before = copy.deepcopy(incident)

        render_cli(incident)
        render_markdown(incident)
        render_json(incident)

        self.assertEqual(incident, before)


if __name__ == "__main__":
    unittest.main()
