import copy
import json
import unittest
from unittest.mock import patch

from agent.ast_analyzer import run_ast_analysis
from agent.decision_assembly import assemble_decision_incident
from agent.deployment_review_service import review_manifest_change


RISKY_SQL = """SELECT
    o.customer_id,
    SUM(o.order_total) AS revenue
FROM raw_orders o
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
WHERE c.is_deleted = 0
GROUP BY o.customer_id"""


class DeploymentReviewServiceTests(unittest.TestCase):
    def test_raw_code_is_preferred_for_customer_authored_ast_analysis(self):
        manifest = _manifest(
            compiled_code=RISKY_SQL,
            raw_code="select 1 as raw_value",
            sql="select 2 as legacy_value",
        )

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_called_once_with("select 1 as raw_value", "fct_revenue")
        self.assertEqual(result["sql_sources"][0]["sql_source"], "raw_code")
        self.assertEqual(result["sql_sources"][0]["ast_status"], "evaluated")
        self.assertIn("ast", result["incident"]["signal_components"])
        self.assertNotIn("LEFT JOIN", " ".join(result["incident"]["top_reasons"]))

    def test_raw_code_is_used_when_compiled_code_is_blank(self):
        raw_sql = "select customer_id from raw_customers"
        manifest = _manifest(compiled_code="   ", raw_code=raw_sql, sql="select 2")

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_called_once_with(raw_sql, "fct_revenue")
        self.assertEqual(result["sql_sources"][0]["sql_source"], "raw_code")

    def test_sql_is_final_real_sql_fallback(self):
        legacy_sql = "select customer_id from legacy_orders"
        manifest = _manifest(raw_code=None, sql=legacy_sql)

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_called_once_with(legacy_sql, "fct_revenue")
        self.assertEqual(result["sql_sources"][0]["sql_source"], "sql")

    def test_missing_sql_is_skipped_without_synthetic_ast_result(self):
        manifest = _manifest()

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_not_called()
        self.assertNotIn("ast", result["incident"]["signal_components"])
        self.assertEqual(
            result["sql_sources"],
            [
                {
                    "unique_id": "model.analytics.fct_revenue",
                    "name": "fct_revenue",
                    "original_file_path": "models/marts/fct_revenue.sql",
                    "path": "models/marts/fct_revenue.sql",
                    "sql_available": False,
                    "sql_source": "unavailable",
                    "ast_status": "skipped",
                }
            ],
        )
        self.assertNotIn("select * from fct_revenue", json.dumps(result).lower())

    def test_manifest_is_not_mutated(self):
        manifest = _manifest(compiled_code=RISKY_SQL)
        original = copy.deepcopy(manifest)

        _review(manifest)

        self.assertEqual(manifest, original)

    def test_unknown_explicit_model_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "Changed model not found in manifest: missing_model"):
            review_manifest_change(
                manifest=_manifest(compiled_code=RISKY_SQL),
                changed_files=[],
                changed_models=["missing_model"],
                deployment_id="deploy-1",
            )

    def test_result_is_deterministic_and_json_serializable(self):
        manifest = _manifest(compiled_code="select customer_id from raw_orders")

        first = _review(manifest)
        second = _review(manifest)

        self.assertEqual(first, second)
        self.assertEqual(first["version"], "1")
        self.assertEqual(first["decision"], first["incident"]["decision"])
        self.assertIn("cli", first["rendered"])
        self.assertIn("markdown", first["rendered"])
        json.dumps(first, sort_keys=True)

    def test_comment_only_model_change_is_allow(self):
        result = _review(
            _manifest(
                model_name="dim_calendar",
                columns=["date_day"],
                raw_code=(
                    "-- Explain why this projection is intentionally narrow.\n"
                    "select date_day from raw_dates"
                ),
                compiled_code="select date_day from raw_dates",
            )
        )

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["incident"]["health"], 100)
        self.assertEqual(result["incident"]["top_reasons"], [])
        self.assertIn(
            "No material deployment risks detected.",
            result["rendered"]["markdown"],
        )

    def test_macro_expansion_findings_are_not_attributed_to_customer_sql(self):
        raw_sql = "{{ dbt_date.get_base_dates(n_dateparts=30, datepart='day') }}"
        generated_sql = (
            "select *, amount / denominator as ratio "
            "from generated_date_spine where status != 'cancelled'"
        )
        manifest = _manifest(
            model_name="dim_calendar",
            columns=["date_day"],
            raw_code=raw_sql,
            compiled_code=generated_sql,
        )

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_called_once_with(raw_sql, "dim_calendar")
        self.assertEqual(result["decision"], "ALLOW")
        rendered = json.dumps(result)
        self.assertNotIn("Division without a zero-safe guard", rendered)
        self.assertNotIn("Not-equal filter may silently exclude NULL rows", rendered)

    def test_customer_authored_division_and_null_filter_block(self):
        risky_raw_sql = (
            "select amount / denominator as ratio "
            "from raw_orders where status != 'cancelled'"
        )

        result = _review(
            _manifest(
                raw_code=risky_raw_sql,
                compiled_code=risky_raw_sql,
            )
        )

        self.assertEqual(result["decision"], "BLOCK")
        self.assertLess(result["incident"]["health"], 70)
        reasons = " ".join(result["incident"]["top_reasons"])
        self.assertIn("zero", reasons.lower())
        self.assertIn("NULL", reasons)

    def test_revenue_linked_risky_sql_is_penalized_only_by_ast(self):
        risky_sql = (
            "select customer_id, sum(order_total) / count(*) as average_order_value "
            "from raw_orders where order_status != 'cancelled' group by customer_id"
        )
        captured = {}

        def capture(signals, **kwargs):
            captured["signals"] = list(signals)
            return assemble_decision_incident(signals, **kwargs)

        with patch(
            "agent.pr_analysis.assemble_decision_incident",
            side_effect=capture,
        ):
            result = _review(
                _manifest(
                    raw_code=risky_sql,
                    compiled_code=risky_sql,
                )
            )

        signals = {
            signal.component: signal
            for signal in captured["signals"]
        }
        self.assertEqual(signals["ast"].score, -35)
        self.assertEqual(signals["kpi_impact"].score, 0)
        self.assertEqual(signals["semantic_contract"].score, 0)
        self.assertEqual(signals["kpi_impact"].reasons, [])
        self.assertEqual(signals["semantic_contract"].reasons, [])
        self.assertEqual(result["incident"]["health"], 65)
        self.assertFalse(
            any(
                "Revenue" in reason
                for reason in result["incident"]["top_reasons"]
            )
        )
        findings = result["material_findings"]
        self.assertEqual(
            [finding["title"] for finding in findings],
            [
                "Division without a zero-safe guard",
                "Integer division may truncate decimal values",
                "Not-equal filter may silently exclude NULL rows",
            ],
        )
        self.assertEqual(
            {finding["affected_model"] for finding in findings},
            {"fct_revenue"},
        )
        self.assertTrue(
            all(finding["recommended_fix"] for finding in findings)
        )
        serialized_findings = json.dumps(findings)
        self.assertNotIn(risky_sql, serialized_findings)
        self.assertNotIn("line_reference", serialized_findings)

    def test_harmless_revenue_linked_sql_is_allow(self):
        safe_sql = (
            "select customer_id, order_total "
            "from raw_orders where order_status = 'completed'"
        )

        result = _review(
            _manifest(
                raw_code=safe_sql,
                compiled_code=safe_sql,
            )
        )

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["incident"]["health"], 100)
        self.assertEqual(result["incident"]["top_reasons"], [])


def _review(manifest):
    model = next(iter(manifest["nodes"].values()))
    return review_manifest_change(
        manifest=manifest,
        changed_files=[model["original_file_path"]],
        deployment_id="deploy-1",
    )


def _manifest(
    *,
    compiled_code=None,
    raw_code=None,
    sql=None,
    model_name="fct_revenue",
    columns=None,
):
    columns = list(columns or ["customer_id", "revenue"])
    unique_id = f"model.analytics.{model_name}"
    original_file_path = f"models/marts/{model_name}.sql"
    model = {
        "resource_type": "model",
        "name": model_name,
        "unique_id": unique_id,
        "original_file_path": original_file_path,
        "columns": {
            column: {"name": column}
            for column in columns
        },
    }
    if compiled_code is not None:
        model["compiled_code"] = compiled_code
    if raw_code is not None:
        model["raw_code"] = raw_code
    if sql is not None:
        model["sql"] = sql
    return {
        "metadata": {"project_name": "analytics", "dbt_version": "1.8.0"},
        "nodes": {unique_id: model},
    }


if __name__ == "__main__":
    unittest.main()
