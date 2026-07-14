import copy
import json
import unittest
from unittest.mock import patch

from agent.ast_analyzer import run_ast_analysis
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
    def test_compiled_code_is_preferred_and_reaches_ast_unchanged(self):
        manifest = _manifest(
            compiled_code=RISKY_SQL,
            raw_code="select 1 as raw_value",
            sql="select 2 as legacy_value",
        )

        with patch("agent.pr_analysis.run_ast_analysis", wraps=run_ast_analysis) as analyzer:
            result = _review(manifest)

        analyzer.assert_called_once_with(RISKY_SQL, "fct_revenue")
        self.assertEqual(result["sql_sources"][0]["sql_source"], "compiled_code")
        self.assertEqual(result["sql_sources"][0]["ast_status"], "evaluated")
        self.assertIn("ast", result["incident"]["signal_components"])
        self.assertIn("LEFT JOIN", " ".join(result["incident"]["top_reasons"]))

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


def _review(manifest):
    return review_manifest_change(
        manifest=manifest,
        changed_files=["models/marts/fct_revenue.sql"],
        deployment_id="deploy-1",
    )


def _manifest(*, compiled_code=None, raw_code=None, sql=None):
    model = {
        "resource_type": "model",
        "name": "fct_revenue",
        "unique_id": "model.analytics.fct_revenue",
        "original_file_path": "models/marts/fct_revenue.sql",
        "columns": {
            "customer_id": {"name": "customer_id"},
            "revenue": {"name": "revenue"},
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
        "nodes": {"model.analytics.fct_revenue": model},
    }


if __name__ == "__main__":
    unittest.main()
