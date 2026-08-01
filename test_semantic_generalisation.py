import json
import unittest
from pathlib import Path


class SemanticGeneralisationTests(unittest.TestCase):
    def _compare(self, before, after, model="model.sales"):
        from agent.pr_analysis import compare_manifest_sql

        def manifest(sql):
            return {"nodes": {model: {"resource_type": "model", "name": model.rsplit(".", 1)[-1], "raw_code": sql}}}

        return compare_manifest_sql(manifest(before), manifest(after), [model])

    def test_material_refund_definition_variants(self):
        variants = {
            "direct subtraction removal": (
                "gross_revenue - refunds as net_revenue",
                "gross_revenue as net_revenue",
            ),
            "renamed aliases": (
                "gross_amount - refund_amount as net_amount",
                "gross_amount as net_amount",
            ),
            "coalesce removal": (
                "gross_revenue - coalesce(refunds, 0) as net_revenue",
                "gross_revenue as net_revenue",
            ),
            "upstream cte removal": (
                "with adjusted as (select total_sales - refund_total as realized_sales from source_data) select realized_sales from adjusted",
                "with adjusted as (select total_sales as realized_sales from source_data) select realized_sales from adjusted",
            ),
        }
        for case, (before, after) in variants.items():
            with self.subTest(case=case):
                comparison = self._compare(before, after)
                self.assertTrue(comparison["material_sql_changes"])
                finding = comparison["material_sql_changes"][0]
                self.assertEqual(finding["finding_owner"], "semantic_refund_fallback")
                self.assertEqual(finding["finding_type"], "refund_adjustment_subtraction_removed")
                self.assertTrue(finding["fallback_used"])

    def test_equivalent_refactors_and_unrelated_refunds_do_not_flag(self):
        controls = {
            "renamed refund cte with subtraction preserved": (
                "with refund_rows as (select refund_amount from source_data) select gross_amount - refund_amount as net_amount from refund_rows",
                "with adjustment_rows as (select refund_amount from source_data) select gross_amount - refund_amount as net_amount from adjustment_rows",
            ),
            "equivalent subtraction rewrite": (
                "gross_revenue - refunds as net_revenue",
                "gross_revenue - (refunds) as net_revenue",
            ),
            "comments and formatting only": (
                "select gross_revenue - refunds as net_revenue from source_data",
                "-- equivalent business definition\nSELECT gross_revenue - refunds AS net_revenue FROM source_data",
            ),
            "unused refund field removed": (
                "with source_rows as (select order_id, refund_note from source_data) select order_id from source_rows",
                "with source_rows as (select order_id from source_data) select order_id from source_rows",
            ),
            "unrelated refund field removed": (
                "select ticket_id, refund_policy_label from support_ticket_metadata",
                "select ticket_id from support_ticket_metadata",
            ),
        }
        for case, (before, after) in controls.items():
            with self.subTest(case=case):
                self.assertFalse(self._compare(before, after)["material_sql_changes"])

    def test_declared_differently_named_margin_definition_uses_semantic_diff_owner(self):
        from agent.deployment_review_service import review_manifest_change

        base = _declared_margin_manifest(
            metric_name="Contribution Margin Contract",
            sql="select billed_amount - dispute_amount as realized_margin from billing_source",
            invariants=["chargebacks must be subtracted"],
        )
        head = _declared_margin_manifest(
            metric_name="Realized Margin Definition",
            sql="select billed_amount as realized_margin from billing_source",
            invariants=[],
        )

        result = review_manifest_change(
            manifest=head,
            previous_manifest=base,
            changed_files=["models/margin_statement.sql"],
            deployment_id="declared-margin-change",
            manifest_source={"base": "github", "head": "github"},
            base_sha="base-margin-sha",
            head_sha="head-margin-sha",
        )

        self.assertNotEqual(result["decision"], "ALLOW")
        findings = result["incident"]["metadata"]["semantic_findings"]
        material = [finding for finding in findings if finding["finding_owner"] == "semantic_diff"]
        self.assertTrue(material)
        self.assertTrue(all(not finding["fallback_used"] for finding in material))
        self.assertFalse(any(finding["finding_owner"] == "semantic_refund_fallback" for finding in findings))
        for finding in material:
            self.assertEqual(
                set(finding),
                {
                    "component",
                    "severity",
                    "finding_owner",
                    "finding_type",
                    "evidence_source",
                    "base_manifest_available",
                    "head_manifest_available",
                    "semantic_comparison_evaluated",
                    "fallback_used",
                },
            )

    def test_equivalent_refund_logic_moved_between_ctes_remains_allow(self):
        from agent.deployment_review_service import review_manifest_change

        base = _manifest_for_model(
            "adjusted_values",
            "with adjusted as (select gross_amount - coalesce(refund_amount, 0) as net_amount from source_data) select net_amount from adjusted",
            ["gross_amount", "refund_amount", "net_amount"],
        )
        head = _manifest_for_model(
            "adjusted_values",
            "with source_values as (select gross_amount, refund_amount from source_data), adjusted as (select gross_amount - coalesce(refund_amount, 0) as net_amount from source_values) select net_amount from adjusted",
            ["gross_amount", "refund_amount", "net_amount"],
        )

        result = review_manifest_change(
            manifest=head,
            previous_manifest=base,
            changed_files=["models/adjusted_values.sql"],
            deployment_id="equivalent-cte-movement",
            manifest_source={"base": "github", "head": "github"},
        )

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["incident"]["metadata"]["semantic_findings"], [])
        self.assertFalse(result["incident"]["metadata"]["manifest_comparison"]["material_sql_changes"])

    def test_unrelated_support_refund_policy_field_removal_remains_allow(self):
        from agent.deployment_review_service import review_manifest_change

        base = _manifest_for_model(
            "support_ticket_labels",
            "select ticket_id, refund_policy_label from support_ticket_metadata",
            ["ticket_id", "refund_policy_label"],
        )
        head = _manifest_for_model(
            "support_ticket_labels",
            "select ticket_id from support_ticket_metadata",
            ["ticket_id"],
        )

        result = review_manifest_change(
            manifest=head,
            previous_manifest=base,
            changed_files=["models/support_ticket_labels.sql"],
            deployment_id="support-label-removal",
            manifest_source={"base": "github", "head": "github"},
        )

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["incident"]["metadata"]["semantic_findings"], [])
        self.assertFalse(result["incident"]["metadata"]["manifest_comparison"]["material_sql_changes"])

    def test_refund_fallback_audit_metadata_is_explicit_and_contains_no_sql(self):
        from agent.deployment_review_service import review_manifest_change

        base_sql = "select total_sales - refund_total as realized_sales from settlement_source"
        head_sql = "select total_sales as realized_sales from settlement_source"
        base = _manifest_for_model(
            "settlement_summary",
            base_sql,
            ["total_sales", "refund_total", "realized_sales"],
        )
        head = _manifest_for_model(
            "settlement_summary",
            head_sql,
            ["total_sales", "realized_sales"],
        )

        result = review_manifest_change(
            manifest=head,
            previous_manifest=base,
            changed_files=["models/settlement_summary.sql"],
            deployment_id="fallback-owner",
            manifest_source={"base": "github", "head": "github"},
        )

        self.assertNotEqual(result["decision"], "ALLOW")
        findings = result["incident"]["metadata"]["semantic_findings"]
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["finding_owner"], "semantic_refund_fallback")
        self.assertEqual(finding["finding_type"], "refund_adjustment_subtraction_removed")
        self.assertEqual(finding["evidence_source"], "trusted_base_head_manifest_sql")
        self.assertTrue(finding["base_manifest_available"])
        self.assertTrue(finding["head_manifest_available"])
        self.assertTrue(finding["semantic_comparison_evaluated"])
        self.assertTrue(finding["fallback_used"])
        self.assertEqual(
            finding["fallback_scope"],
            "refund_adjustment_subtraction_from_net_or_gross_business_expression",
        )
        serialized = json.dumps(result, sort_keys=True).lower()
        self.assertNotIn(base_sql, serialized)
        self.assertNotIn(head_sql, serialized)

    def test_refund_fallback_documentation_states_scope_and_precedence(self):
        import inspect

        from agent import pr_analysis

        documentation = Path(__file__).parent / "docs" / "semantic-refund-fallback.md"
        self.assertTrue(documentation.is_file())
        text = documentation.read_text(encoding="utf-8").lower()
        docstring = inspect.getdoc(pr_analysis._material_sql_delta).lower()
        for required in (
            "declared contracts, invariants, and kpi definitions take precedence",
            "deliberately narrow",
            "not arbitrary sql semantic equivalence",
            "refund/adjustment subtraction",
            "semantic_refund_fallback",
        ):
            self.assertIn(required, text)
        self.assertIn("deliberately narrow", docstring)
        self.assertIn("not arbitrary sql semantic equivalence", docstring)


def _manifest_for_model(model_name, sql, columns):
    unique_id = f"model.analytics.{model_name}"
    return {
        "metadata": {"project_name": "semantic_audit"},
        "nodes": {
            unique_id: {
                "resource_type": "model",
                "unique_id": unique_id,
                "name": model_name,
                "original_file_path": f"models/{model_name}.sql",
                "raw_code": sql,
                "depends_on": {"nodes": []},
                "columns": {column: {"name": column} for column in columns},
            }
        },
    }


def _declared_margin_manifest(*, metric_name, sql, invariants):
    manifest = _manifest_for_model(
        "margin_statement",
        sql,
        ["billed_amount", "dispute_amount", "realized_margin"],
    )
    metric_id = "metric.analytics." + metric_name.lower().replace(" ", "_")
    manifest["metrics"] = {
        metric_id: {
            "resource_type": "metric",
            "unique_id": metric_id,
            "name": metric_name,
            "label": metric_name,
            "type": "simple",
            "description": "Contribution margin after customer chargeback adjustments.",
            "depends_on": {"nodes": ["model.analytics.margin_statement"]},
            "meta": {
                "relium": {
                    "business_meaning": "Realized billed value after disputes.",
                    "invariants": list(invariants),
                }
            },
        }
    }
    return manifest


if __name__ == "__main__":
    unittest.main()
