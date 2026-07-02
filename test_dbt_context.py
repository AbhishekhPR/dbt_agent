import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.dbt_context import (
    extract_project_context_from_manifest,
    load_project_context_from_manifest_path,
)
from agent.semantic_context import build_semantic_context


DEMO_DIR = Path(__file__).parent / "demo" / "history_aware"


class DbtContextTests(unittest.TestCase):
    def test_extracts_model_names(self):
        context = extract_project_context_from_manifest(_manifest())

        self.assertEqual(context["model_names"], ["fct_orders", "stg_orders"])
        self.assertEqual(
            [model["name"] for model in context["models"]],
            ["fct_orders", "stg_orders"],
        )

    def test_extracts_model_columns(self):
        context = extract_project_context_from_manifest(_manifest())

        fct_orders = _named(context["models"], "fct_orders")

        self.assertEqual(fct_orders["columns"], ["gross_revenue", "order_id"])
        self.assertIn("gross_revenue", context["column_names"])

    def test_extracts_refs(self):
        context = extract_project_context_from_manifest(_manifest())

        fct_orders = _named(context["models"], "fct_orders")

        self.assertEqual(fct_orders["refs"], ["stg_orders"])
        self.assertIn(
            {"parent": "stg_orders", "child": "fct_orders", "relationship": "ref"},
            context["refs"],
        )

    def test_extracts_sources(self):
        context = extract_project_context_from_manifest(_manifest())

        source = context["sources"][0]
        stg_orders = _named(context["models"], "stg_orders")

        self.assertEqual(source["name"], "raw_shop.orders")
        self.assertEqual(source["source_name"], "raw_shop")
        self.assertEqual(source["table_name"], "orders")
        self.assertEqual(source["columns"], ["order_id", "payment_amount"])
        self.assertEqual(stg_orders["sources"], ["raw_shop.orders"])

    def test_extracts_metrics(self):
        context = extract_project_context_from_manifest(_manifest())

        self.assertEqual(
            context["metrics"],
            [
                {
                    "name": "Revenue / GMV",
                    "label": "Revenue",
                    "type": "simple",
                    "description": "Completed customer payment volume.",
                    "model": "fct_orders",
                }
            ],
        )
        self.assertEqual(context["dbt_metrics"], context["metrics"])

    def test_extracts_exposures(self):
        context = extract_project_context_from_manifest(_manifest())

        self.assertEqual(
            context["exposures"],
            [
                {
                    "name": "revenue_dashboard",
                    "type": "dashboard",
                    "depends_on": ["fct_orders", "Revenue / GMV"],
                    "owner": {"name": "Analytics", "email": "analytics@example.com"},
                    "description": "Executive revenue dashboard.",
                }
            ],
        )
        self.assertEqual(context["dashboard_names"], ["revenue_dashboard"])

    def test_handles_missing_optional_fields(self):
        context = extract_project_context_from_manifest(
            {
                "nodes": {
                    "model.pkg.empty_model": {
                        "resource_type": "model",
                        "name": "empty_model",
                    }
                }
            }
        )

        self.assertEqual(
            context["models"],
            [
                {
                    "name": "empty_model",
                    "unique_id": "model.pkg.empty_model",
                    "path": None,
                    "columns": [],
                    "refs": [],
                    "sources": [],
                    "description": "",
                    "tags": [],
                    "materialized": None,
                }
            ],
        )
        self.assertEqual(context["sources"], [])
        self.assertEqual(context["metrics"], [])

    def test_output_works_with_build_semantic_context(self):
        context = extract_project_context_from_manifest(_manifest())

        semantic_context = build_semantic_context(
            project_context=context,
            changed_models=["stg_orders"],
        )

        self.assertTrue(semantic_context.discovered_kpis)
        self.assertIn("fct_orders", semantic_context.semantic_graph.nodes)
        self.assertTrue(semantic_context.kpi_impact_report.impacted_kpis)

    def test_invalid_path_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Manifest file not found"):
            load_project_context_from_manifest_path("missing-manifest.json")

    def test_invalid_json_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid manifest JSON"):
                load_project_context_from_manifest_path(str(path))

    def test_manifest_input_is_not_mutated(self):
        manifest = _manifest()
        original = copy.deepcopy(manifest)

        context = extract_project_context_from_manifest(manifest)
        context["models"][0]["columns"].append("mutated")

        self.assertEqual(manifest, original)

    def test_output_is_json_serializable(self):
        context = extract_project_context_from_manifest(_manifest())

        serialized = json.dumps(context)

        self.assertIsInstance(serialized, str)

    def test_history_aware_demo_manifests_load_through_dbt_context(self):
        previous = load_project_context_from_manifest_path(
            str(DEMO_DIR / "manifest_previous.json")
        )
        current = load_project_context_from_manifest_path(
            str(DEMO_DIR / "manifest_current.json")
        )

        self.assertIn("fct_revenue", previous["model_names"])
        self.assertIn("fct_revenue", current["model_names"])
        self.assertIn("stg_refunds", current["model_names"])
        self.assertEqual(previous["metrics"][0]["name"], "Revenue")
        self.assertEqual(current["metrics"][0]["name"], "Revenue")

    def test_history_aware_demo_current_context_preserves_revenue_kpi_name(self):
        context = load_project_context_from_manifest_path(
            str(DEMO_DIR / "manifest_current.json")
        )

        semantic_context = build_semantic_context(
            project_context=context,
            changed_models=["fct_revenue"],
        )

        self.assertIn(
            "Revenue",
            [contract.kpi_name for contract in semantic_context.knowledge_report.contracts],
        )


def _named(items, name):
    for item in items:
        if item["name"] == name:
            return item
    raise AssertionError(f"{name!r} not found in {items!r}")


def _manifest():
    return {
        "metadata": {
            "project_name": "jaffle_shop",
            "dbt_version": "1.8.0",
        },
        "nodes": {
            "model.jaffle_shop.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "unique_id": "model.jaffle_shop.stg_orders",
                "original_file_path": "models/staging/stg_orders.sql",
                "columns": {
                    "order_id": {"name": "order_id", "description": "Order id"},
                    "payment_amount": {"name": "payment_amount"},
                },
                "depends_on": {
                    "nodes": ["source.jaffle_shop.raw_shop.orders"],
                },
                "description": "Staged orders.",
                "tags": ["staging"],
                "config": {"materialized": "view"},
            },
            "model.jaffle_shop.fct_orders": {
                "resource_type": "model",
                "name": "fct_orders",
                "unique_id": "model.jaffle_shop.fct_orders",
                "path": "marts/fct_orders.sql",
                "columns": {
                    "order_id": {"name": "order_id"},
                    "gross_revenue": {"name": "gross_revenue"},
                },
                "depends_on": {
                    "nodes": ["model.jaffle_shop.stg_orders"],
                },
                "refs": [["stg_orders"]],
                "description": "Fact orders.",
                "tags": ["mart", "revenue"],
                "config": {"materialized": "table"},
            },
        },
        "sources": {
            "source.jaffle_shop.raw_shop.orders": {
                "resource_type": "source",
                "name": "orders",
                "source_name": "raw_shop",
                "table_name": "orders",
                "unique_id": "source.jaffle_shop.raw_shop.orders",
                "columns": {
                    "order_id": {"name": "order_id"},
                    "payment_amount": {"name": "payment_amount"},
                },
                "description": "Raw orders.",
            }
        },
        "metrics": {
            "metric.jaffle_shop.revenue": {
                "name": "Revenue / GMV",
                "label": "Revenue",
                "type": "simple",
                "description": "Completed customer payment volume.",
                "model": "ref('fct_orders')",
            }
        },
        "exposures": {
            "exposure.jaffle_shop.revenue_dashboard": {
                "name": "revenue_dashboard",
                "type": "dashboard",
                "depends_on": {
                    "nodes": [
                        "model.jaffle_shop.fct_orders",
                        "metric.jaffle_shop.revenue",
                    ],
                },
                "owner": {
                    "name": "Analytics",
                    "email": "analytics@example.com",
                },
                "description": "Executive revenue dashboard.",
            }
        },
        "semantic_models": {
            "semantic_model.jaffle_shop.orders": {
                "name": "orders_semantic_model",
                "model": "ref('fct_orders')",
                "description": "Semantic model for orders.",
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
