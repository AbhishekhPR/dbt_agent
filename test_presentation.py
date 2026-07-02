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


def make_business_metric_incident(*, healthy=False):
    signal = Signal(
        component="business_metrics",
        severity=Severity.LOW if healthy else Severity.HIGH,
        confidence=90 if healthy else 95,
        score=0 if healthy else -35,
        reasons=(
            ["Business metrics within expected range"]
            if healthy
            else ["High severity metric spike detected"]
        ),
        metadata={
            "metrics": {
                "failed_pickups": 17 if not healthy else 0,
                "mis_sorts": 14 if not healthy else 0,
                "overflow_avalanches": 7 if not healthy else 0,
            },
            "baseline": {
                "failed_pickups": 5,
                "mis_sorts": 5,
                "overflow_avalanches": 4,
            },
            "spike_percentages": (
                {}
                if healthy
                else {
                    "failed_pickups": 240.0,
                    "mis_sorts": 180.0,
                    "overflow_avalanches": 75.0,
                }
            ),
        },
    )
    return Incident(
        incident_id="INC-BIZ",
        health=65 if not healthy else 100,
        decision=DeploymentDecision.BLOCK if not healthy else DeploymentDecision.ALLOW,
        severity=Severity.HIGH if not healthy else Severity.LOW,
        confidence=95 if not healthy else 90,
        root_cause="High severity metric spike detected" if not healthy else "",
        recommendation="Review business metric regressions." if not healthy else "",
        signals=[signal],
    )


def make_semantic_diff_incident():
    return Incident(
        incident_id="INC-SEMDIFF",
        health=65,
        decision=DeploymentDecision.BLOCK,
        severity=Severity.HIGH,
        confidence=92,
        root_cause="Revenue gained upstream dependency refunds",
        recommendation="Review historical semantic changes before deployment.",
        signals=[
            Signal(
                component="semantic_diff",
                severity=Severity.HIGH,
                confidence=92,
                score=-35,
                reasons=[
                    "Revenue gained upstream dependency refunds",
                    "Revenue lost invariant never negative",
                ],
                metadata={
                    "previous_snapshot_id": "abc123",
                    "current_snapshot_id": "def456",
                    "changed_kpis": ["Revenue"],
                    "added_kpis": ["MRR"],
                    "removed_kpis": ["Churn"],
                    "dependency_changes": {
                        "Revenue": {
                            "upstream_sources": {
                                "added": ["refunds"],
                                "removed": [],
                            },
                        },
                    },
                    "contract_changes": {
                        "Revenue": {
                            "invariants": {
                                "added": [],
                                "removed": ["never negative"],
                            },
                        },
                    },
                },
            )
        ],
    )


def make_noisy_kpi_incident():
    return Incident(
        incident_id="INC-NOISY-KPI",
        health=70,
        decision=DeploymentDecision.WARN,
        severity=Severity.MEDIUM,
        confidence=88,
        root_cause="Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
        recommendation="Review impacted KPI owners.",
        signals=[
            Signal(
                component="kpi_impact",
                severity=Severity.MEDIUM,
                confidence=88,
                score=-15,
                reasons=[
                    "dbt_metrics value Revenue matched KPI concept Revenue/ GMV",
                    "business_terms value orders matched KPI concept Revenue/ GMV",
                    "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
                ],
            )
        ],
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
        self.assertIn("- SQL Logic: LEFT JOIN nullification detected", rendered)
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
        self.assertIn("- **SQL Logic: LEFT JOIN nullification detected**", rendered)
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

    def test_cli_renders_business_metrics(self):
        rendered = render_cli(make_business_metric_incident())

        self.assertIn("Business Metrics", rendered)
        self.assertIn("- Failed Pickups +240%", rendered)
        self.assertIn("- Mis-sorts +180%", rendered)
        self.assertIn("- Overflow Avalanches +75%", rendered)

    def test_markdown_renders_business_metrics(self):
        rendered = render_markdown(make_business_metric_incident())

        self.assertIn("## Business Metrics", rendered)
        self.assertIn("- Failed Pickups +240%", rendered)
        self.assertIn("- Mis-sorts +180%", rendered)
        self.assertIn("- Overflow Avalanches +75%", rendered)

    def test_healthy_business_metrics_render_healthy(self):
        cli = render_cli(make_business_metric_incident(healthy=True))
        markdown = render_markdown(make_business_metric_incident(healthy=True))

        self.assertIn("Business Metrics", cli)
        self.assertIn("Healthy", cli)
        self.assertIn("## Business Metrics", markdown)
        self.assertIn("Healthy", markdown)

    def test_existing_rendering_has_no_business_metrics_without_signal(self):
        rendered = render_cli(make_incident())

        self.assertNotIn("Business Metrics", rendered)

    def test_cli_renders_historical_semantic_change_when_semantic_diff_exists(self):
        rendered = render_cli(make_semantic_diff_incident())

        self.assertIn("Historical Semantic Change", rendered)
        self.assertIn("- Revenue gained upstream dependency refunds", rendered)
        self.assertIn("- Revenue lost invariant never negative", rendered)
        self.assertIn("- Changed KPIs: Revenue", rendered)
        self.assertIn("- Added KPIs: MRR", rendered)
        self.assertIn("- Removed KPIs: Churn", rendered)
        self.assertIn("- Dependency Changes: Revenue upstream_sources added refunds", rendered)
        self.assertIn("- Contract Changes: Revenue invariants removed never negative", rendered)

    def test_markdown_renders_historical_semantic_change_when_semantic_diff_exists(self):
        rendered = render_markdown(make_semantic_diff_incident())

        self.assertIn("### Historical Semantic Change", rendered)
        self.assertIn("- Revenue gained upstream dependency refunds", rendered)
        self.assertIn("- Revenue lost invariant never negative", rendered)
        self.assertIn("- Changed KPIs: Revenue", rendered)
        self.assertIn("- Added KPIs: MRR", rendered)
        self.assertIn("- Removed KPIs: Churn", rendered)

    def test_existing_rendering_has_no_historical_semantic_change_without_signal(self):
        cli = render_cli(make_incident())
        markdown = render_markdown(make_incident())

        self.assertNotIn("Historical Semantic Change", cli)
        self.assertNotIn("Historical Semantic Change", markdown)

    def test_historical_semantic_change_snapshot_ids_are_rendered(self):
        cli = render_cli(make_semantic_diff_incident())
        markdown = render_markdown(make_semantic_diff_incident())

        self.assertIn("- Previous Snapshot: abc123", cli)
        self.assertIn("- Current Snapshot: def456", cli)
        self.assertIn("**Previous Snapshot:** abc123", markdown)
        self.assertIn("**Current Snapshot:** def456", markdown)

    def test_historical_semantic_change_reasons_are_rendered(self):
        rendered = render_markdown(make_semantic_diff_incident())

        self.assertIn("Revenue gained upstream dependency refunds", rendered)
        self.assertIn("Revenue lost invariant never negative", rendered)

    def test_top_reasons_filter_low_level_kpi_discovery_matches(self):
        cli = render_cli(make_noisy_kpi_incident())
        markdown = render_markdown(make_noisy_kpi_incident())

        self.assertIn(
            "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
            cli,
        )
        self.assertIn(
            "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
            markdown,
        )
        self.assertNotIn("matched KPI concept", cli)
        self.assertNotIn("dbt_metrics value", cli)
        self.assertNotIn("business_terms value", markdown)


if __name__ == "__main__":
    unittest.main()
