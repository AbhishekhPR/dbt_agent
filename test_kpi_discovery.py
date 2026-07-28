import copy
import unittest

from agent.kpi_discovery import DiscoveredKPI, KPIHint, discover_kpis


class KPIDiscoveryTests(unittest.TestCase):
    def test_retail_context_discovers_revenue_or_gmv(self):
        context = {
            "model_names": ["fct_orders", "fct_payments"],
            "column_names": ["order_id", "payment_amount", "gross_revenue", "gmv"],
            "dashboard_names": ["Executive Revenue Dashboard"],
        }

        kpis = discover_kpis(context)

        self.assertTrue(any(kpi.name == "Revenue / GMV" for kpi in kpis))
        revenue = _kpi(kpis, "Revenue / GMV")
        self.assertIn("fct_orders", revenue.related_models)
        self.assertIn("gross_revenue", revenue.related_columns)
        self.assertGreaterEqual(revenue.confidence, 70)

    def test_streaming_context_discovers_playback_reliability(self):
        context = {
            "model_names": ["fct_playback_sessions"],
            "column_names": ["stream_start_count", "buffering_seconds", "watch_time"],
            "sql_expressions": ["sum(buffering_seconds) / sum(watch_time)"],
        }

        kpis = discover_kpis(context)

        self.assertTrue(any(kpi.name == "Playback Reliability" for kpi in kpis))

    def test_logistics_context_discovers_fulfillment_kpis(self):
        context = {
            "model_names": ["fct_fulfillment_events"],
            "column_names": ["failed_pickups", "mis_sorts", "staging_area"],
            "business_terms": ["warehouse fulfillment reliability"],
        }

        kpis = discover_kpis(context)

        self.assertTrue(any(kpi.name == "Fulfillment Reliability" for kpi in kpis))

    def test_saas_context_discovers_churn_and_mrr(self):
        context = {
            "model_names": ["fct_subscriptions"],
            "column_names": ["customer_churn_rate", "mrr", "arr"],
            "business_terms": ["subscription retention"],
        }

        kpis = discover_kpis(context)

        names = [kpi.name for kpi in kpis]
        self.assertIn("Churn / Retention", names)
        self.assertIn("Recurring Revenue", names)

    def test_unrelated_project_returns_no_kpis(self):
        context = {
            "model_names": ["stg_weather"],
            "column_names": ["temperature_celsius", "humidity_pct"],
            "file_paths": ["models/staging/stg_weather.sql"],
        }

        self.assertEqual(discover_kpis(context), [])

    def test_confidence_increases_with_more_supporting_evidence(self):
        sparse = {
            "column_names": ["revenue"],
        }
        rich = {
            "model_names": ["fct_orders", "fct_payments"],
            "column_names": ["revenue", "gmv", "payment_amount"],
            "dashboard_names": ["Revenue Overview"],
            "dbt_metrics": [{"name": "gross_revenue"}],
        }

        sparse_revenue = _kpi(discover_kpis(sparse), "Revenue / GMV")
        rich_revenue = _kpi(discover_kpis(rich), "Revenue / GMV")

        self.assertGreater(rich_revenue.confidence, sparse_revenue.confidence)

    def test_discovered_kpis_preserve_related_models_and_columns(self):
        context = {
            "model_names": ["fct_failed_payments"],
            "column_names": ["failed_payment_count", "payment_status"],
            "file_paths": ["models/marts/fct_failed_payments.sql"],
        }

        kpi = _kpi(discover_kpis(context), "Failed Payments")

        self.assertIsInstance(kpi, DiscoveredKPI)
        self.assertIn("fct_failed_payments", kpi.related_models)
        self.assertIn("failed_payment_count", kpi.related_columns)
        self.assertTrue(kpi.reasons)
        self.assertIn("hints", kpi.metadata)
        self.assertIsInstance(kpi.metadata["hints"][0], KPIHint)

    def test_input_context_is_not_mutated(self):
        context = {
            "model_names": ["fct_inventory"],
            "column_names": ["inventory_accuracy", "stockout_count"],
            "semantic_models": [{"name": "inventory_semantic_model"}],
        }
        original = copy.deepcopy(context)

        first = discover_kpis(context)
        second = discover_kpis(context)

        self.assertEqual(context, original)
        self.assertEqual(first, second)


def _kpi(kpis, name):
    for kpi in kpis:
        if kpi.name == name:
            return kpi
    raise AssertionError(f"KPI {name!r} not discovered. Found: {[k.name for k in kpis]}")


if __name__ == "__main__":
    unittest.main()
