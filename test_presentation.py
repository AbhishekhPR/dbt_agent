import copy
import json
import unittest
from types import SimpleNamespace

from agent.decision_engine import DeploymentDecision
from agent.incident import Incident
from agent.presentation import (
    render_backtest_cli,
    render_backtest_markdown,
    render_cli,
    render_json,
    render_markdown,
)
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


def make_column_lineage_incident():
    return Incident(
        incident_id="INC-COLUMN-LINEAGE",
        health=82,
        decision=DeploymentDecision.WARN,
        severity=Severity.MEDIUM,
        confidence=88,
        root_cause="Revenue reads fct_revenue.net_revenue",
        recommendation="Review net revenue lineage.",
        signals=[
            Signal(
                component="kpi_impact",
                severity=Severity.HIGH,
                confidence=95,
                score=-30,
                reasons=["Revenue reads fct_revenue.net_revenue"],
                metadata={
                    "column_level_evidence": [
                        "Revenue reads fct_revenue.net_revenue",
                    ],
                },
            ),
            Signal(
                component="semantic_diff",
                severity=Severity.MEDIUM,
                confidence=85,
                score=-20,
                reasons=[
                    "fct_revenue.net_revenue gained upstream column stg_refunds.refund_amount",
                ],
                metadata={
                    "previous_snapshot_id": "abc123",
                    "current_snapshot_id": "def456",
                    "changed_kpis": ["Revenue"],
                    "added_kpis": [],
                    "removed_kpis": [],
                    "dependency_changes": {},
                    "contract_changes": {},
                    "column_dependency_changes": [
                        "fct_revenue.net_revenue gained upstream column stg_refunds.refund_amount",
                    ],
                },
            ),
        ],
    )


def make_generated_assumption_verification_incident():
    checks = [
        _assumption_check(
            column_name=f"revenue_check_{index}",
            sql=f"SELECT COUNT(*) FROM fct_revenue WHERE revenue_check_{index} < 0",
        )
        for index in range(1, 9)
    ]
    return Incident(
        incident_id="INC-ASSUMPTIONS",
        health=92,
        decision=DeploymentDecision.ALLOW,
        severity=Severity.LOW,
        confidence=88,
        root_cause="",
        recommendation="",
        signals=[],
        metadata={
            "assumption_verification": {
                "checks": checks,
                "metadata": {
                    "evaluated": False,
                    "check_count": 8,
                    "evaluated_count": 0,
                },
            }
        },
    )


def make_passing_assumption_verification_incident():
    checks = [
        _assumption_check(
            column_name=f"revenue_check_{index}",
            evaluated=True,
            status="passed",
            passed=True,
            violation_count=0,
        )
        for index in range(1, 9)
    ]
    return _assumption_incident(
        {
            "checks": checks,
            "metadata": {
                "evaluated": True,
                "check_count": 8,
                "evaluated_count": 8,
                "failed_count": 0,
            },
        }
    )


def make_failed_assumption_verification_incident():
    return _assumption_incident(
        {
            "checks": [
                _assumption_check(
                    model_name="fct_revenue",
                    column_name="revenue",
                    check_type="non_negative",
                    invariant="never negative",
                    evaluated=True,
                    status="failed",
                    passed=False,
                    violation_count=3,
                    sql="SELECT COUNT(*) FROM fct_revenue WHERE revenue < 0",
                ),
                _assumption_check(
                    model_name="fct_orders",
                    column_name="customer_id",
                    check_type="not_null",
                    invariant="not null",
                    evaluated=True,
                    status="failed",
                    passed=False,
                    violation_count=10,
                    sql="SELECT COUNT(*) FROM fct_orders WHERE customer_id IS NULL",
                ),
                _assumption_check(
                    model_name="fct_orders",
                    column_name="order_id",
                    check_type="not_null",
                    invariant="not null",
                    evaluated=True,
                    status="passed",
                    passed=True,
                    violation_count=0,
                ),
            ],
            "metadata": {
                "evaluated": True,
                "check_count": 3,
                "evaluated_count": 3,
                "failed_count": 2,
            },
        },
        health=63,
        decision=DeploymentDecision.BLOCK,
        severity=Severity.HIGH,
    )


def _assumption_incident(report, *, health=92, decision=DeploymentDecision.ALLOW, severity=Severity.LOW):
    return Incident(
        incident_id="INC-ASSUMPTIONS",
        health=health,
        decision=decision,
        severity=severity,
        confidence=88,
        root_cause="",
        recommendation="",
        signals=[],
        metadata={"assumption_verification": report},
    )


def _assumption_check(
    *,
    kpi_name="Revenue",
    model_name="fct_revenue",
    column_name="revenue",
    invariant="never negative",
    check_type="non_negative",
    sql="SELECT COUNT(*) AS violation_count FROM fct_revenue WHERE revenue < 0",
    evaluated=False,
    status="not_evaluated",
    passed=None,
    violation_count=None,
):
    return {
        "kpi_name": kpi_name,
        "model_name": model_name,
        "column_name": column_name,
        "invariant": invariant,
        "check_type": check_type,
        "sql": sql,
        "evaluated": evaluated,
        "status": status,
        "passed": passed,
        "violation_count": violation_count,
        "error": None,
        "metadata": {},
    }


def make_noisy_column_lineage_incident():
    return Incident(
        incident_id="INC-NOISY-LINEAGE",
        health=70,
        decision=DeploymentDecision.WARN,
        severity=Severity.HIGH,
        confidence=92,
        root_cause="Revenue gained upstream dependency refunds",
        recommendation="Review refund-related revenue lineage.",
        affected_models=["fct_revenue"],
        signals=[
            Signal(
                component="semantic_diff",
                severity=Severity.HIGH,
                confidence=95,
                score=-35,
                reasons=[
                    "stg_refunds.order_id output column was added",
                    "stg_refunds.refund_amount output column was added",
                    "Revenue gained upstream dependency refunds",
                    "fct_revenue.refund_amount output column was added",
                    "Revenue gained related model stg_refunds",
                    "stg_refunds.refund_id output column was added",
                ],
                metadata={
                    "previous_snapshot_id": "abc123",
                    "current_snapshot_id": "def456",
                    "changed_kpis": ["Revenue"],
                    "added_kpis": [],
                    "removed_kpis": [],
                    "dependency_changes": {
                        "Revenue": {
                            "upstream_sources": {
                                "added": ["refunds"],
                                "removed": [],
                            },
                            "related_models": {
                                "added": ["stg_refunds"],
                                "removed": [],
                            },
                        },
                    },
                    "contract_changes": {},
                    "changed_columns_by_model": {
                        "fct_revenue": ["refund_amount"],
                        "stg_refunds": ["order_id", "refund_amount", "refund_id"],
                    },
                    "column_dependency_changes": [
                        "stg_refunds.order_id output column was added",
                        "stg_refunds.refund_amount output column was added",
                        "fct_revenue.refund_amount output column was added",
                        "stg_refunds.refund_id output column was added",
                    ],
                },
            ),
            Signal(
                component="kpi_impact",
                severity=Severity.HIGH,
                confidence=92,
                score=-30,
                reasons=["Revenue may be impacted by changed model fct_revenue"],
            ),
            Signal(
                component="semantic_contract",
                severity=Severity.MEDIUM,
                confidence=85,
                score=-15,
                reasons=["Revenue is semantically impacted by changed models"],
            ),
        ],
    )


def make_backtest_result():
    incident = make_noisy_column_lineage_incident()
    incident.health = 0
    incident.decision = DeploymentDecision.BLOCK
    incident.confidence = 82
    incident.metadata = {
        "assumption_verification": {
            "checks": [
                _assumption_check(column_name=f"revenue_check_{index}")
                for index in range(1, 16)
            ],
            "metadata": {
                "evaluated": False,
                "check_count": 15,
                "evaluated_count": 0,
            },
        }
    }
    incident.signals.extend(
        [
            Signal("ast", Severity.LOW, 75, 0),
            Signal("metadata_checks", Severity.LOW, 75, 0),
            Signal("metadata_drift", Severity.LOW, 75, 0),
            Signal("blast_radius", Severity.LOW, 75, 0),
            Signal("historical_reliability", Severity.LOW, 75, 0),
        ]
    )
    return SimpleNamespace(
        incident=incident,
        historical_deployment_id="refunds-backtest",
        would_have_decision="BLOCK",
        would_have_health=0,
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

    def test_normal_deployment_markdown_heading_remains_unchanged(self):
        rendered = render_markdown(make_incident())

        self.assertIn("# Relium Deployment Decision", rendered)
        self.assertIn("## Deployment Decision", rendered)

    def test_backtest_markdown_is_compact_and_purpose_built(self):
        rendered = render_backtest_markdown(make_backtest_result())

        self.assertIn("# Relium Backtest Result", rendered)
        self.assertIn("## Would Have Decided\nWOULD BLOCK", rendered)
        self.assertIn("## Historical Deployment\nrefunds-backtest", rendered)
        self.assertIn("## Pipeline Health\n0 / 100", rendered)
        self.assertIn("## Severity\nHIGH", rendered)
        self.assertIn("## Confidence\n82%", rendered)
        self.assertIn("## Primary Root Cause\nRevenue gained upstream dependency refunds", rendered)
        self.assertIn("## What Relium Would Have Caught", rendered)
        self.assertIn("- Revenue gained upstream dependency refunds", rendered)
        self.assertIn("- Revenue gained related model stg_refunds", rendered)
        self.assertIn("- fct_revenue.refund_amount output column was added", rendered)
        self.assertIn("## Semantic Change", rendered)
        self.assertIn("- Dependency Changes: Revenue upstream_sources added refunds", rendered)
        self.assertIn("## Column-Level Lineage", rendered)
        self.assertIn("- Revenue lineage now includes refund-related data", rendered)
        self.assertIn("## Assumption Verification", rendered)
        self.assertIn("- 15 checks generated for Revenue", rendered)
        self.assertIn("- 0 checks evaluated", rendered)
        self.assertIn("- Not evaluated: no warehouse connection provided", rendered)
        self.assertIn("## Signals Considered", rendered)
        self.assertIn("- semantic_diff", rendered)
        self.assertIn("- kpi_impact", rendered)
        self.assertIn("- semantic_contract", rendered)
        self.assertIn("- ast", rendered)
        self.assertIn("## Summary", rendered)
        self.assertIn(
            "Relium would have blocked this deployment before production because Revenue gained upstream dependency refunds",
            rendered,
        )
        self.assertNotIn("# Relium Deployment Decision", rendered)

    def test_backtest_cli_is_compact(self):
        rendered = render_backtest_cli(make_backtest_result())

        self.assertIn("Relium Backtest Result", rendered)
        self.assertIn("Would Have Decided: WOULD BLOCK", rendered)
        self.assertIn("What Relium Would Have Caught:", rendered)
        self.assertIn("Semantic Change:", rendered)
        self.assertIn("Summary:", rendered)
        self.assertNotIn("Relium Deployment Decision", rendered)
        self.assertNotIn("Reasoning:", rendered)

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

    def test_generated_only_assumption_checks_render_compact_summary(self):
        rendered = render_cli(make_generated_assumption_verification_incident())

        self.assertIn("Assumption Verification", rendered)
        self.assertIn("- 8 checks generated for Revenue", rendered)
        self.assertIn("- 0 checks evaluated", rendered)
        self.assertIn("- Not evaluated: no warehouse connection provided", rendered)
        self.assertNotIn("revenue_check_1 never negative", rendered)
        self.assertNotIn("SELECT COUNT(*)", rendered)

    def test_evaluated_passing_assumption_checks_render_compact_success(self):
        rendered = render_cli(make_passing_assumption_verification_incident())

        self.assertIn("Assumption Verification", rendered)
        self.assertIn("- 8 checks evaluated", rendered)
        self.assertIn("- All assumption checks passed", rendered)
        self.assertNotIn("revenue_check_1 never negative", rendered)

    def test_failed_assumption_checks_render_explicit_failed_lines(self):
        rendered = render_cli(make_failed_assumption_verification_incident())

        self.assertIn("Assumption Verification", rendered)
        self.assertIn("- FAILED: fct_revenue.revenue has 3 negative values", rendered)
        self.assertIn("- FAILED: fct_orders.customer_id has 10 null values", rendered)
        self.assertNotIn("fct_orders.order_id not null (passed)", rendered)

    def test_markdown_assumption_verification_stays_compact_and_hides_sql(self):
        rendered = render_markdown(make_generated_assumption_verification_incident())

        self.assertIn("## Assumption Verification", rendered)
        self.assertIn("- 8 checks generated for Revenue", rendered)
        self.assertIn("- 0 checks evaluated", rendered)
        self.assertNotIn("SELECT COUNT(*)", rendered)
        self.assertNotIn("revenue_check_1 never negative", rendered)

    def test_existing_rendering_has_no_assumption_verification_without_report(self):
        rendered = render_cli(make_incident())

        self.assertNotIn("Assumption Verification", rendered)

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

    def test_cli_renders_compact_column_lineage_section(self):
        rendered = render_cli(make_column_lineage_incident())

        self.assertIn("Column-Level Lineage", rendered)
        self.assertIn(
            "- fct_revenue.net_revenue gained upstream column stg_refunds.refund_amount",
            rendered,
        )
        self.assertIn("- Revenue reads fct_revenue.net_revenue", rendered)
        self.assertNotIn("column_lineage_graph", rendered)

    def test_markdown_renders_compact_column_lineage_section(self):
        rendered = render_markdown(make_column_lineage_incident())

        self.assertIn("## Column-Level Lineage", rendered)
        self.assertIn(
            "- fct_revenue.net_revenue gained upstream column stg_refunds.refund_amount",
            rendered,
        )
        self.assertIn("- Revenue reads fct_revenue.net_revenue", rendered)
        self.assertNotIn("column_lineage_graph", rendered)

    def test_top_reasons_do_not_promote_staging_column_additions(self):
        rendered = render_cli(make_noisy_column_lineage_incident())
        top_reasons = _section(rendered, "Top Reasons:", "Recommendation:")

        self.assertIn("- Revenue gained upstream dependency refunds", top_reasons)
        self.assertIn("- Revenue gained related model stg_refunds", top_reasons)
        self.assertIn("- fct_revenue.refund_amount output column was added", top_reasons)
        self.assertIn("- Revenue may be impacted by changed model fct_revenue", top_reasons)
        self.assertIn("- Revenue is semantically impacted by changed models", top_reasons)
        self.assertNotIn("stg_refunds.order_id output column was added", top_reasons)
        self.assertNotIn("stg_refunds.refund_amount output column was added", top_reasons)
        self.assertNotIn("stg_refunds.refund_id output column was added", top_reasons)

    def test_historical_semantic_change_orders_business_before_column_details(self):
        rendered = render_markdown(make_noisy_column_lineage_incident())
        section = _section(rendered, "### Historical Semantic Change", "**Previous Snapshot:**")

        self.assertLess(
            section.index("Revenue gained upstream dependency refunds"),
            section.index("fct_revenue.refund_amount output column was added"),
        )
        self.assertLess(
            section.index("Revenue gained related model stg_refunds"),
            section.index("fct_revenue.refund_amount output column was added"),
        )
        self.assertLess(
            section.index("fct_revenue.refund_amount output column was added"),
            section.index("stg_refunds.order_id output column was added"),
        )

    def test_column_lineage_section_keeps_relevant_lines_concise(self):
        rendered = render_markdown(make_noisy_column_lineage_incident())
        section = _section(rendered, "## Column-Level Lineage", "## Affected Models")

        self.assertIn("- fct_revenue.refund_amount output column was added", section)
        self.assertIn("- Revenue lineage now includes refund-related data", section)
        self.assertNotIn("stg_refunds.order_id output column was added", section)
        self.assertNotIn("stg_refunds.refund_amount output column was added", section)
        self.assertNotIn("stg_refunds.refund_id output column was added", section)


def _section(rendered: str, start: str, end: str) -> str:
    start_index = rendered.index(start)
    end_index = rendered.index(end, start_index)
    return rendered[start_index:end_index]


if __name__ == "__main__":
    unittest.main()
