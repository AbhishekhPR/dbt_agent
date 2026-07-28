import copy
import json
import unittest

from agent.deployment_snapshot import DeploymentSnapshot
from agent.semantic_diff import SemanticDiff, compare_semantic_snapshots, to_signal


class SemanticDiffTests(unittest.TestCase):
    def test_no_semantic_changes_returns_low(self):
        previous = _snapshot("previous", [_contract("Revenue")])
        current = _snapshot("current", [_contract("Revenue")])

        diff = compare_semantic_snapshots(previous, current)

        self.assertIsInstance(diff, SemanticDiff)
        self.assertEqual(diff.severity, "LOW")
        self.assertEqual(diff.changed_kpis, [])
        self.assertEqual(diff.reasons, [])

    def test_added_kpi_detected(self):
        previous = _snapshot("previous", [_contract("Revenue")])
        current = _snapshot("current", [_contract("Revenue"), _contract("MRR")])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(diff.added_kpis, ["MRR"])
        self.assertEqual(diff.severity, "MEDIUM")
        self.assertIn("MRR KPI was added", diff.reasons)

    def test_removed_kpi_detected(self):
        previous = _snapshot("previous", [_contract("Revenue"), _contract("MRR")])
        current = _snapshot("current", [_contract("Revenue")])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(diff.removed_kpis, ["MRR"])
        self.assertEqual(diff.severity, "HIGH")
        self.assertIn("MRR KPI was removed", diff.reasons)

    def test_related_model_dependency_change_detected(self):
        previous = _snapshot("previous", [_contract("Revenue", related_models=["fct_orders"])])
        current = _snapshot("current", [_contract("Revenue", related_models=["fct_orders", "fct_refunds"])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(diff.changed_kpis, ["Revenue"])
        self.assertEqual(
            diff.dependency_changes["Revenue"]["related_models"],
            {"added": ["fct_refunds"], "removed": []},
        )
        self.assertEqual(diff.severity, "MEDIUM")
        self.assertIn("Revenue gained related model fct_refunds", diff.reasons)

    def test_related_column_change_detected(self):
        previous = _snapshot("previous", [_contract("MRR", related_columns=["subscription_amount"])])
        current = _snapshot("current", [_contract("MRR", related_columns=["plan_amount"])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.dependency_changes["MRR"]["related_columns"],
            {"added": ["plan_amount"], "removed": ["subscription_amount"]},
        )
        self.assertEqual(diff.severity, "MEDIUM")
        self.assertIn("MRR related columns changed from subscription_amount to plan_amount", diff.reasons)

    def test_upstream_source_change_detected(self):
        previous = _snapshot("previous", [_contract("Revenue", upstream_sources=["raw_orders"])])
        current = _snapshot("current", [_contract("Revenue", upstream_sources=["raw_orders", "refunds"])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.dependency_changes["Revenue"]["upstream_sources"],
            {"added": ["refunds"], "removed": []},
        )
        self.assertEqual(diff.severity, "HIGH")
        self.assertIn("Revenue gained upstream dependency refunds", diff.reasons)

    def test_downstream_consumer_change_detected(self):
        previous = _snapshot("previous", [_contract("Revenue", downstream_consumers=["exec_dashboard"])])
        current = _snapshot("current", [_contract("Revenue", downstream_consumers=["exec_dashboard", "finance_dashboard"])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.dependency_changes["Revenue"]["downstream_consumers"],
            {"added": ["finance_dashboard"], "removed": []},
        )
        self.assertEqual(diff.severity, "MEDIUM")
        self.assertIn("Revenue gained downstream consumer finance_dashboard", diff.reasons)

    def test_contract_assumption_change_detected(self):
        previous = _snapshot("previous", [_contract("Churn", assumptions=["active users exist"])])
        current = _snapshot("current", [_contract("Churn", assumptions=["cohort definitions unchanged"])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.contract_changes["Churn"]["assumptions"],
            {"added": ["cohort definitions unchanged"], "removed": ["active users exist"]},
        )
        self.assertEqual(diff.severity, "MEDIUM")
        self.assertIn("Churn contract assumption changed", diff.reasons)

    def test_contract_invariant_removal_is_high(self):
        previous = _snapshot("previous", [_contract("Revenue", invariants=["never negative"])])
        current = _snapshot("current", [_contract("Revenue", invariants=[])])

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(diff.severity, "HIGH")
        self.assertEqual(
            diff.contract_changes["Revenue"]["invariants"],
            {"added": [], "removed": ["never negative"]},
        )
        self.assertIn("Revenue lost invariant never negative", diff.reasons)

    def test_confidence_increases_with_multiple_changes(self):
        single = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", related_models=["fct_orders"])]),
            _snapshot("current", [_contract("Revenue", related_models=["fct_orders", "fct_refunds"])]),
        )
        multiple = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", related_models=["fct_orders"], invariants=["never negative"])]),
            _snapshot(
                "current",
                [_contract("Revenue", related_models=["fct_refunds"], invariants=[], upstream_sources=["refunds"])],
            ),
        )

        self.assertGreater(multiple.confidence, single.confidence)
        self.assertLessEqual(multiple.confidence, 100)

    def test_reasons_are_deterministic(self):
        previous = _snapshot(
            "previous",
            [
                _contract("Revenue", related_columns=["gross_revenue"], invariants=["never negative"]),
                _contract("MRR", related_columns=["subscription_amount"]),
            ],
        )
        current = _snapshot(
            "current",
            [
                _contract("MRR", related_columns=["plan_amount"]),
                _contract("Revenue", related_columns=["net_revenue"], invariants=[]),
            ],
        )

        first = compare_semantic_snapshots(previous, current)
        second = compare_semantic_snapshots(previous, current)

        self.assertEqual(first.reasons, second.reasons)
        self.assertEqual(
            first.reasons,
            [
                "MRR related columns changed from subscription_amount to plan_amount",
                "Revenue related columns changed from gross_revenue to net_revenue",
                "Revenue lost invariant never negative",
            ],
        )

    def test_serializable(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue")]),
            _snapshot("current", [_contract("Revenue"), _contract("MRR")]),
        )

        payload = diff.to_dict()

        json.dumps(payload)
        self.assertEqual(payload["added_kpis"], ["MRR"])

    def test_inputs_are_not_mutated(self):
        previous = _snapshot("previous", [_contract("Revenue", invariants=["never negative"])])
        current = _snapshot("current", [_contract("Revenue", invariants=[])])
        original_previous = copy.deepcopy(previous)
        original_current = copy.deepcopy(current)

        compare_semantic_snapshots(previous, current)

        self.assertEqual(previous, original_previous)
        self.assertEqual(current, original_current)

    def test_low_semantic_diff_converts_to_low_signal_with_score_zero(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue")]),
            _snapshot("current", [_contract("Revenue")]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.component, "semantic_diff")
        self.assertEqual(signal.severity, "LOW")
        self.assertEqual(signal.confidence, diff.confidence)
        self.assertEqual(signal.score, 0)

    def test_medium_semantic_diff_converts_to_medium_signal(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", related_models=["fct_orders"])]),
            _snapshot("current", [_contract("Revenue", related_models=["fct_orders", "fct_refunds"])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.component, "semantic_diff")
        self.assertEqual(signal.severity, "MEDIUM")
        self.assertEqual(signal.score, -20)

    def test_high_semantic_diff_converts_to_high_signal(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", invariants=["never negative"])]),
            _snapshot("current", [_contract("Revenue", invariants=[])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.component, "semantic_diff")
        self.assertEqual(signal.severity, "HIGH")
        self.assertEqual(signal.score, -35)

    def test_signal_preserves_reasons(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", upstream_sources=["raw_orders"])]),
            _snapshot("current", [_contract("Revenue", upstream_sources=["raw_orders", "refunds"])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.reasons, diff.reasons)

    def test_signal_preserves_changed_kpi_metadata(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", related_models=["fct_orders"])]),
            _snapshot("current", [_contract("Revenue", related_models=["fct_refunds"])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.metadata["changed_kpis"], ["Revenue"])
        self.assertEqual(signal.metadata["previous_snapshot_id"], "previous")
        self.assertEqual(signal.metadata["current_snapshot_id"], "current")

    def test_signal_preserves_added_and_removed_kpi_metadata(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue"), _contract("MRR")]),
            _snapshot("current", [_contract("Revenue"), _contract("Churn")]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.metadata["added_kpis"], ["Churn"])
        self.assertEqual(signal.metadata["removed_kpis"], ["MRR"])

    def test_signal_preserves_dependency_changes(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", upstream_sources=["raw_orders"])]),
            _snapshot("current", [_contract("Revenue", upstream_sources=["raw_orders", "refunds"])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.metadata["dependency_changes"], diff.dependency_changes)

    def test_signal_preserves_contract_changes(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", invariants=["never negative"])]),
            _snapshot("current", [_contract("Revenue", invariants=[])]),
        )

        signal = to_signal(diff)

        self.assertEqual(signal.metadata["contract_changes"], diff.contract_changes)

    def test_derives_changed_columns_by_model_from_column_dependency_changes(self):
        previous = _snapshot(
            "previous",
            [_contract("Revenue", related_models=["fct_revenue"], related_columns=["net_revenue"])],
            column_lineage_graph=_column_graph(
                "fct_revenue",
                ["net_revenue"],
                {"net_revenue": ["stg_orders.order_total"]},
            ),
        )
        current = _snapshot(
            "current",
            [_contract("Revenue", related_models=["fct_revenue"], related_columns=["net_revenue"])],
            column_lineage_graph=_column_graph(
                "fct_revenue",
                ["net_revenue"],
                {
                    "net_revenue": [
                        "stg_orders.order_total",
                        "stg_refunds.refund_amount",
                    ]
                },
            ),
        )

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.metadata["changed_columns_by_model"],
            {"fct_revenue": ["net_revenue"]},
        )
        self.assertIn(
            "fct_revenue.net_revenue gained upstream column stg_refunds.refund_amount",
            diff.metadata["column_dependency_changes"],
        )

    def test_detects_added_output_column(self):
        previous = _snapshot(
            "previous",
            [_contract("Revenue")],
            column_lineage_graph=_column_graph("fct_revenue", ["net_revenue"], {}),
        )
        current = _snapshot(
            "current",
            [_contract("Revenue")],
            column_lineage_graph=_column_graph("fct_revenue", ["debug_flag", "net_revenue"], {}),
        )

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.metadata["changed_columns_by_model"],
            {"fct_revenue": ["debug_flag"]},
        )
        self.assertIn(
            "fct_revenue.debug_flag output column was added",
            diff.metadata["column_dependency_changes"],
        )

    def test_detects_removed_output_column(self):
        previous = _snapshot(
            "previous",
            [_contract("Revenue")],
            column_lineage_graph=_column_graph("fct_revenue", ["debug_flag", "net_revenue"], {}),
        )
        current = _snapshot(
            "current",
            [_contract("Revenue")],
            column_lineage_graph=_column_graph("fct_revenue", ["net_revenue"], {}),
        )

        diff = compare_semantic_snapshots(previous, current)

        self.assertEqual(
            diff.metadata["changed_columns_by_model"],
            {"fct_revenue": ["debug_flag"]},
        )
        self.assertIn(
            "fct_revenue.debug_flag output column was removed",
            diff.metadata["column_dependency_changes"],
        )

    def test_old_snapshots_without_column_lineage_still_work(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue")]),
            _snapshot(
                "current",
                [_contract("Revenue")],
                column_lineage_graph=_column_graph("fct_revenue", ["net_revenue"], {}),
            ),
        )

        self.assertEqual(diff.metadata["changed_columns_by_model"], {})
        self.assertEqual(diff.metadata["column_dependency_changes"], [])

    def test_to_signal_does_not_mutate_diff(self):
        diff = compare_semantic_snapshots(
            _snapshot("previous", [_contract("Revenue", invariants=["never negative"])]),
            _snapshot("current", [_contract("Revenue", invariants=[])]),
        )
        original = copy.deepcopy(diff)

        to_signal(diff)

        self.assertEqual(diff, original)


def _snapshot(snapshot_id, contracts, *, column_lineage_graph=None):
    return DeploymentSnapshot(
        snapshot_id=snapshot_id,
        deployment_id=f"deploy-{snapshot_id}",
        created_at="2026-07-02T00:00:00+00:00",
        changed_models=["stg_orders"],
        semantic_context={
            "discovered_kpis": [{"name": contract["kpi_name"]} for contract in contracts],
            "knowledge_report": {"contracts": contracts},
            "metadata": {"kpi_count": len(contracts)},
            **(
                {"column_lineage_graph": copy.deepcopy(column_lineage_graph)}
                if column_lineage_graph is not None
                else {}
            ),
        },
        decision=None,
        incident_summary=None,
        metadata={"source": "unit-test"},
    )


def _contract(
    kpi_name,
    *,
    related_models=None,
    related_columns=None,
    upstream_sources=None,
    downstream_consumers=None,
    assumptions=None,
    invariants=None,
    business_meaning=None,
):
    return {
        "kpi_name": kpi_name,
        "description": f"{kpi_name} contract",
        "business_meaning": business_meaning or f"{kpi_name} meaning",
        "related_models": list(related_models or []),
        "related_columns": list(related_columns or []),
        "upstream_sources": list(upstream_sources or []),
        "downstream_consumers": list(downstream_consumers or []),
        "assumptions": list(assumptions or []),
        "invariants": list(invariants or []),
        "confidence": 80,
        "metadata": {},
    }


def _column_graph(model_name, output_columns, dependencies):
    edges = []
    for to_column, upstream_columns in dependencies.items():
        for upstream in upstream_columns:
            if "." in upstream:
                from_model, from_column = upstream.rsplit(".", 1)
            else:
                from_model, from_column = None, upstream
            edges.append(
                {
                    "from_model": from_model,
                    "from_column": from_column,
                    "to_model": model_name,
                    "to_column": to_column,
                    "confidence": 0.95 if from_model else 0.7,
                    "reason": "unit-test",
                }
            )
    return {
        "models": {
            model_name: {
                "model_name": model_name,
                "output_columns": list(output_columns),
                "edges": edges,
                "unknown_columns": [],
                "metadata": {},
            }
        },
        "metadata": {"source": "unit-test"},
    }


if __name__ == "__main__":
    unittest.main()
