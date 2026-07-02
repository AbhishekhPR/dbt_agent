import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent.decision_engine import DeploymentDecision
from agent.github_pr_guard import build_pr_review, render_pr_review_markdown
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


def make_semantic_diff_signal():
    return Signal(
        "semantic_diff",
        Severity.HIGH,
        92,
        -35,
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

    def test_markdown_contains_all_major_sections(self):
        markdown = render_pr_review_markdown(build_pr_review(make_incident()))

        self.assertIn("## Relium AI Deployment Review", markdown)
        self.assertIn("**Deployment Decision:** BLOCK", markdown)
        self.assertIn("**Pipeline Health:** 42 / 100", markdown)
        self.assertIn("**Confidence:** 91%", markdown)
        self.assertIn("**Models Reviewed:** 2", markdown)
        self.assertIn("**Highest Severity:** CRITICAL", markdown)
        self.assertIn("### Primary Root Cause", markdown)
        self.assertIn("### Executive Summary", markdown)
        self.assertIn("### Evidence", markdown)
        self.assertIn("### Recommendation", markdown)
        self.assertIn("### Signals Considered", markdown)

    def test_block_review_markdown_renders_clearly(self):
        markdown = render_pr_review_markdown(build_pr_review(make_incident()))

        self.assertIn("**Deployment Decision:** BLOCK", markdown)
        self.assertIn("Deployment is blocked because", markdown)
        self.assertIn("LEFT JOIN nullification detected", markdown)
        self.assertIn("Move right-side filters into JOIN clauses.", markdown)

    def test_warn_and_allow_markdown_render_correctly(self):
        warn = render_pr_review_markdown(
            build_pr_review(
                make_incident(
                    decision=DeploymentDecision.WARN,
                    health=78,
                    severity=Severity.MEDIUM,
                    confidence=84,
                )
            )
        )
        allow = render_pr_review_markdown(
            build_pr_review(
                make_incident(
                    decision=DeploymentDecision.ALLOW,
                    health=99,
                    severity=Severity.LOW,
                    confidence=76,
                    recommendation="Deploy normally.",
                    affected_models=["fct_orders"],
                    signals=[],
                )
            )
        )

        self.assertIn("**Deployment Decision:** WARN", warn)
        self.assertIn("**Pipeline Health:** 78 / 100", warn)
        self.assertIn("Deployment should proceed with caution", warn)
        self.assertIn("**Deployment Decision:** ALLOW", allow)
        self.assertIn("**Pipeline Health:** 99 / 100", allow)
        self.assertIn("Deployment is allowed", allow)

    def test_markdown_preserves_evidence_order(self):
        review = build_pr_review(
            make_incident(
                signals=[
                    Signal("metadata_checks", Severity.HIGH, 95, -30, reasons=["A", "B"]),
                    Signal("blast_radius", Severity.MEDIUM, 85, -15, reasons=["C"]),
                ]
            )
        )

        markdown = render_pr_review_markdown(review)

        first = markdown.index("Metadata Checks: A")
        second = markdown.index("Metadata Checks: B")
        third = markdown.index("Blast Radius: C")
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_markdown_evidence_is_curated(self):
        incident = make_incident(
            signals=[
                Signal(
                    "kpi_impact",
                    Severity.HIGH,
                    94,
                    -30,
                    reasons=[
                        "dbt_metrics value Revenue matched KPI concept Revenue/ GMV",
                        "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
                    ],
                    metadata={"raw_reason_count": 2},
                )
            ]
        )
        review = build_pr_review(incident)

        markdown = render_pr_review_markdown(review)

        self.assertIn(
            "KPI Impact: Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
            markdown,
        )
        self.assertNotIn("matched KPI concept", markdown)
        self.assertNotIn("dbt_metrics value", markdown)
        self.assertIn(
            "dbt_metrics value Revenue matched KPI concept Revenue/ GMV",
            review["signals_considered"][0]["reasons"],
        )

    def test_empty_evidence_and_signals_are_handled_gracefully(self):
        markdown = render_pr_review_markdown(
            build_pr_review(
                make_incident(
                    decision=DeploymentDecision.ALLOW,
                    health=100,
                    severity=Severity.LOW,
                    confidence=75,
                    affected_models=[],
                    signals=[],
                )
            )
        )

        self.assertIn("### Evidence\n- None", markdown)
        self.assertIn("### Signals Considered\n- None", markdown)

    def test_markdown_does_not_mutate_review_object(self):
        review = build_pr_review(make_incident())
        before = copy.deepcopy(review)

        render_pr_review_markdown(review)

        self.assertEqual(review, before)

    def test_pr_review_demo_command_prints_markdown_and_exits_zero(self):
        from click.testing import CliRunner

        from agent.cli import cli

        escaped_stdout = io.StringIO()
        escaped_stderr = io.StringIO()
        with redirect_stdout(escaped_stdout), redirect_stderr(escaped_stderr):
            result = CliRunner().invoke(cli, ["pr-review-demo"])
        output = result.output
        escaped_output = escaped_stdout.getvalue() + escaped_stderr.getvalue()
        if escaped_output:
            output += escaped_output

        self.assertEqual(result.exit_code, 0, output)
        self.assertIn("Relium AI Deployment Review", output)
        self.assertIn("Deployment Decision", output)
        self.assertIn("Pipeline Health", output)
        self.assertIn("Evidence", output)
        self.assertIn("Business Metrics", output)
        self.assertIn("failed_pickups", output)

    def test_pr_review_demo_output_writes_markdown_file_and_exits_zero(self):
        from click.testing import CliRunner

        from agent.cli import cli

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "relium_pr_review.md"
            escaped_stdout = io.StringIO()
            escaped_stderr = io.StringIO()
            with redirect_stdout(escaped_stdout), redirect_stderr(escaped_stderr):
                result = CliRunner().invoke(
                    cli,
                    ["pr-review-demo", "--output", str(output_path)],
                )
            output = result.output
            escaped_output = escaped_stdout.getvalue() + escaped_stderr.getvalue()
            if escaped_output:
                output += escaped_output

            self.assertEqual(result.exit_code, 0, output)
            self.assertTrue(output_path.exists())
            written = output_path.read_text(encoding="utf-8")

        self.assertIn("PR review written to", output)
        self.assertIn("relium_pr_review.md", output)
        self.assertIn("Relium AI Deployment Review", written)
        self.assertIn("Deployment Decision", written)
        self.assertIn("Pipeline Health", written)
        self.assertIn("Evidence", written)
        self.assertIn("Business Metrics", written)
        self.assertIn("failed_pickups", written)

    def test_github_pr_review_renders_business_metrics(self):
        incident = make_incident(
            signals=[
                Signal(
                    "business_metrics",
                    Severity.HIGH,
                    95,
                    -35,
                    reasons=["High severity metric spike detected"],
                    metadata={
                        "metrics": {
                            "failed_pickups": 17,
                            "mis_sorts": 14,
                            "overflow_avalanches": 7,
                        },
                        "baseline": {
                            "failed_pickups": 5,
                            "mis_sorts": 5,
                            "overflow_avalanches": 4,
                        },
                        "spike_percentages": {
                            "failed_pickups": 240.0,
                            "mis_sorts": 180.0,
                            "overflow_avalanches": 75.0,
                        },
                    },
                )
            ],
        )

        review = build_pr_review(incident)
        markdown = render_pr_review_markdown(review)

        self.assertEqual(
            review["business_metrics"],
            [
                "Failed Pickups +240%",
                "Mis-sorts +180%",
                "Overflow Avalanches +75%",
            ],
        )
        self.assertIn("### Business Metrics", markdown)
        self.assertIn("- Failed Pickups +240%", markdown)
        self.assertIn("- Mis-sorts +180%", markdown)
        self.assertIn("- Overflow Avalanches +75%", markdown)

    def test_github_pr_review_renders_healthy_business_metrics(self):
        incident = make_incident(
            decision=DeploymentDecision.ALLOW,
            health=100,
            severity=Severity.LOW,
            confidence=90,
            signals=[
                Signal(
                    "business_metrics",
                    Severity.LOW,
                    90,
                    0,
                    reasons=["Business metrics within expected range"],
                    metadata={
                        "metrics": {"failed_pickups": 0},
                        "baseline": {"failed_pickups": 1},
                        "spike_percentages": {},
                    },
                )
            ],
        )

        markdown = render_pr_review_markdown(build_pr_review(incident))

        self.assertIn("### Business Metrics", markdown)
        self.assertIn("Healthy", markdown)

    def test_build_pr_review_includes_historical_semantic_change(self):
        review = build_pr_review(make_incident(signals=[make_semantic_diff_signal()]))

        self.assertEqual(
            review["historical_semantic_change"],
            {
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
                "previous_snapshot_id": "abc123",
                "current_snapshot_id": "def456",
                "reasons": [
                    "Revenue gained upstream dependency refunds",
                    "Revenue lost invariant never negative",
                ],
            },
        )

    def test_pr_review_markdown_renders_historical_semantic_change_section(self):
        review = build_pr_review(make_incident(signals=[make_semantic_diff_signal()]))

        markdown = render_pr_review_markdown(review)

        self.assertIn("### Historical Semantic Change", markdown)
        self.assertIn("- Revenue gained upstream dependency refunds", markdown)
        self.assertIn("- Revenue lost invariant never negative", markdown)
        self.assertIn("- Changed KPIs: Revenue", markdown)
        self.assertIn("**Previous Snapshot:** abc123", markdown)
        self.assertIn("**Current Snapshot:** def456", markdown)

    def test_pr_review_unchanged_when_semantic_diff_is_absent(self):
        review = build_pr_review(make_incident())
        markdown = render_pr_review_markdown(review)

        self.assertNotIn("historical_semantic_change", review)
        self.assertNotIn("Historical Semantic Change", markdown)

    def test_pr_review_markdown_does_not_mutate_review_with_semantic_diff(self):
        review = build_pr_review(make_incident(signals=[make_semantic_diff_signal()]))
        before = copy.deepcopy(review)

        render_pr_review_markdown(review)

        self.assertEqual(review, before)

    def test_pr_review_preserves_semantic_diff_dependency_and_contract_changes(self):
        review = build_pr_review(make_incident(signals=[make_semantic_diff_signal()]))
        semantic_change = review["historical_semantic_change"]

        self.assertEqual(
            semantic_change["dependency_changes"],
            {
                "Revenue": {
                    "upstream_sources": {
                        "added": ["refunds"],
                        "removed": [],
                    },
                },
            },
        )
        self.assertEqual(
            semantic_change["contract_changes"],
            {
                "Revenue": {
                    "invariants": {
                        "added": [],
                        "removed": ["never negative"],
                    },
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
