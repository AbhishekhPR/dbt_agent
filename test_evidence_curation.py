import unittest
import copy

from agent.evidence_curation import curate_evidence, curate_reasons
from agent.signals import Severity, Signal


class EvidenceCurationTests(unittest.TestCase):
    def test_deduplicates_repeated_reasons(self):
        signals = [
            Signal("metadata_checks", Severity.HIGH, 90, -30, reasons=["Duplicate count increased"]),
            Signal("blast_radius", Severity.MEDIUM, 80, -15, reasons=["Duplicate count increased"]),
        ]

        self.assertEqual(curate_reasons(signals), ["Duplicate count increased"])

    def test_filters_low_level_kpi_discovery_matches(self):
        signals = [
            Signal(
                "kpi_impact",
                Severity.HIGH,
                95,
                -30,
                reasons=[
                    "dbt_metrics value Revenue matched KPI concept Revenue / GMV",
                    "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
                ],
            )
        ]

        reasons = curate_reasons(signals)
        evidence = curate_evidence(signals)

        self.assertEqual(
            reasons,
            ["Revenue is impacted through stg_orders -> fct_revenue -> Revenue"],
        )
        self.assertNotIn("matched KPI concept", " ".join(item["reason"] for item in evidence))

    def test_keeps_semantic_diff_reasons(self):
        signals = [
            Signal(
                "semantic_diff",
                Severity.HIGH,
                92,
                -35,
                reasons=["Revenue gained upstream dependency refunds"],
            )
        ]

        self.assertEqual(
            curate_reasons(signals),
            ["Revenue gained upstream dependency refunds"],
        )

    def test_keeps_semantic_contract_high_level_reasons(self):
        signals = [
            Signal(
                "semantic_contract",
                Severity.HIGH,
                91,
                -30,
                reasons=["Revenue violated invariant never negative"],
            )
        ]

        evidence = curate_evidence(signals)

        self.assertEqual(evidence[0]["label"], "Semantic Contract")
        self.assertEqual(evidence[0]["reason"], "Revenue violated invariant never negative")

    def test_keeps_kpi_impact_high_level_reasons(self):
        signals = [
            Signal(
                "kpi_impact",
                Severity.MEDIUM,
                85,
                -15,
                reasons=["Revenue is impacted through stg_orders -> fct_revenue -> Revenue"],
            )
        ]

        evidence = curate_evidence(signals)

        self.assertEqual(evidence[0]["label"], "KPI Impact")
        self.assertEqual(
            evidence[0]["reason"],
            "Revenue is impacted through stg_orders -> fct_revenue -> Revenue",
        )

    def test_fixes_joined_spacing_issues(self):
        signals = [
            Signal(
                "kpi_impact",
                Severity.MEDIUM,
                85,
                -15,
                reasons=[
                    "Revenue/ GMV matchedKPI through viapayments",
                ],
            )
        ]

        self.assertEqual(
            curate_reasons(signals),
            ["Revenue / GMV matched KPI through via payments"],
        )

    def test_respects_max_reasons(self):
        signals = [
            Signal(
                "metadata_checks",
                Severity.MEDIUM,
                80,
                -15,
                reasons=["A", "B", "C"],
            )
        ]

        self.assertEqual(curate_reasons(signals, max_reasons=2), ["A", "B"])

    def test_deterministic_ordering_prioritizes_business_level_reasons(self):
        signals = [
            Signal("metadata_checks", Severity.MEDIUM, 80, -15, reasons=["Metadata changed"]),
            Signal("semantic_contract", Severity.HIGH, 90, -30, reasons=["Revenue invariant changed"]),
            Signal("kpi_impact", Severity.HIGH, 92, -30, reasons=["Revenue is impacted"]),
            Signal("semantic_diff", Severity.HIGH, 95, -35, reasons=["Revenue gained refunds"]),
        ]

        first = curate_evidence(signals)
        second = curate_evidence(signals)

        self.assertEqual(first, second)
        self.assertEqual(
            [item["reason"] for item in first],
            [
                "Revenue gained refunds",
                "Revenue is impacted",
                "Revenue invariant changed",
                "Metadata changed",
            ],
        )

    def test_curated_reasons_order_semantic_diff_reasons_by_business_priority(self):
        signals = [
            Signal(
                "semantic_diff",
                Severity.HIGH,
                95,
                -35,
                reasons=[
                    "Revenue gained related model stg_refunds",
                    "Revenue gained upstream dependency refunds",
                    "Revenue gained downstream consumer revenue_dashboard",
                ],
            )
        ]

        self.assertEqual(
            curate_reasons(signals),
            [
                "Revenue gained upstream dependency refunds",
                "Revenue gained related model stg_refunds",
                "Revenue gained downstream consumer revenue_dashboard",
            ],
        )

    def test_invariant_removal_orders_before_upstream_dependency_reason(self):
        signals = [
            Signal(
                "semantic_diff",
                Severity.HIGH,
                95,
                -35,
                reasons=[
                    "Revenue gained upstream dependency refunds",
                    "Revenue lost invariant never negative",
                ],
            )
        ]

        self.assertEqual(
            curate_reasons(signals),
            [
                "Revenue lost invariant never negative",
                "Revenue gained upstream dependency refunds",
            ],
        )

    def test_curating_semantic_diff_reasons_does_not_mutate_inputs(self):
        signals = [
            Signal(
                "semantic_diff",
                Severity.HIGH,
                95,
                -35,
                reasons=[
                    "Revenue gained related model stg_refunds",
                    "Revenue gained upstream dependency refunds",
                ],
                metadata={"changed_kpis": ["Revenue"]},
            )
        ]
        before = copy.deepcopy(signals)

        curate_reasons(signals)
        curate_evidence(signals)

        self.assertEqual(signals, before)

    def test_business_semantic_diff_reasons_rank_above_column_level_reasons(self):
        reasons = curate_reasons(_noisy_column_lineage_signals())

        self.assertEqual(
            reasons[:2],
            [
                "Revenue gained upstream dependency refunds",
                "Revenue gained related model stg_refunds",
            ],
        )
        self.assertLess(
            reasons.index("Revenue gained upstream dependency refunds"),
            reasons.index("fct_revenue.refund_amount output column was added"),
        )

    def test_staging_output_column_additions_are_not_promoted_into_top_reasons(self):
        reasons = curate_reasons(_noisy_column_lineage_signals(), max_reasons=5)

        self.assertEqual(
            reasons,
            [
                "Revenue gained upstream dependency refunds",
                "Revenue gained related model stg_refunds",
                "fct_revenue.refund_amount output column was added",
                "Revenue may be impacted by changed model fct_revenue",
                "Revenue is semantically impacted by changed models",
            ],
        )
        self.assertNotIn("stg_refunds.order_id output column was added", reasons)
        self.assertNotIn("stg_refunds.refund_amount output column was added", reasons)
        self.assertNotIn("stg_refunds.refund_id output column was added", reasons)

    def test_evidence_does_not_start_with_staging_column_additions(self):
        evidence = curate_evidence(_noisy_column_lineage_signals(), max_items=5)

        self.assertEqual(
            [item["reason"] for item in evidence],
            [
                "Revenue gained upstream dependency refunds",
                "Revenue gained related model stg_refunds",
                "fct_revenue.refund_amount output column was added",
                "Revenue may be impacted by changed model fct_revenue",
                "Revenue is semantically impacted by changed models",
            ],
        )


def _noisy_column_lineage_signals():
    return [
        Signal(
            "semantic_diff",
            Severity.HIGH,
            95,
            -35,
            reasons=[
                "stg_refunds.order_id output column was added",
                "stg_refunds.refund_amount output column was added",
                "Revenue gained upstream dependency refunds",
                "fct_revenue.refund_amount output column was added",
                "Revenue gained related model stg_refunds",
                "stg_refunds.refund_id output column was added",
            ],
            metadata={
                "column_dependency_changes": [
                    "stg_refunds.order_id output column was added",
                    "stg_refunds.refund_amount output column was added",
                    "fct_revenue.refund_amount output column was added",
                    "stg_refunds.refund_id output column was added",
                ],
            },
        ),
        Signal(
            "kpi_impact",
            Severity.HIGH,
            92,
            -30,
            reasons=["Revenue may be impacted by changed model fct_revenue"],
        ),
        Signal(
            "semantic_contract",
            Severity.MEDIUM,
            85,
            -15,
            reasons=["Revenue is semantically impacted by changed models"],
        ),
    ]


if __name__ == "__main__":
    unittest.main()
