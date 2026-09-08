from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.billing.client import PolarAPIError, PolarClient


MIGRATION = Path("agent/migrations/postgres/0023_billing_lifecycle_safety.sql")


class BillingLifecycleMigrationContractTests(unittest.TestCase):
    def test_schema_persists_lifecycle_operations_observations_and_intents(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_lifecycle_controls", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS billing_lifecycle_operations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_polar_subscriptions", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_polar_checkouts", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS billing_checkout_intents", sql)
        self.assertGreaterEqual(sql.count("ON DELETE RESTRICT"), 5)
        self.assertIn("WHERE state IN ('claimed', 'provider_created')", sql)

    def test_existing_tenants_are_backfilled_active_without_billing_inference(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("INSERT INTO tenant_lifecycle_controls", sql)
        self.assertIn("SELECT tenant_id, 'active', 'active' FROM tenants", sql)
        self.assertNotIn("FROM tenant_billing", sql)


class PolarSubscriptionClassificationTests(unittest.TestCase):
    def test_provider_states_are_fail_closed(self):
        from agent.billing.lifecycle import classify_subscription

        self.assertEqual(classify_subscription({"status": "active"}),
                         "potentially_billable")
        self.assertEqual(classify_subscription(
            {"status": "active", "cancel_at_period_end": True}),
            "scheduled_cancel")
        self.assertEqual(classify_subscription({"status": "past_due"}),
                         "potentially_billable")
        self.assertEqual(classify_subscription({"status": "canceled"}), "terminal")
        self.assertEqual(classify_subscription({"status": "unpaid"}), "terminal")
        self.assertEqual(classify_subscription({"status": "future_state"}), "unknown")
        self.assertEqual(classify_subscription({}), "unknown")


class PolarLifecycleClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.settings = SimpleNamespace(
            api_base_url="https://api.polar.sh", access_token="polar_secret")

    def _client(self, responses):
        queue = list(responses)

        def transport(**request):
            self.calls.append(request)
            return queue.pop(0)

        return PolarClient(self.settings, transport=transport)

    def test_subscription_listing_follows_every_page_with_authoritative_filter(self):
        client = self._client([
            (200, json.dumps({"items": [{"id": "sub_one"}],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
            (200, json.dumps({"items": [{"id": "sub_two"}],
                              "pagination": {"total_count": 2,
                                             "max_page": 2}}).encode()),
        ])

        items = client.list_subscriptions(external_customer_id="ten_" + "a" * 32)

        self.assertEqual([item["id"] for item in items], ["sub_one", "sub_two"])
        self.assertIn("external_customer_id=ten_", self.calls[0]["url"])
        self.assertIn("page=2", self.calls[1]["url"])
        self.assertNotIn("polar_secret", " ".join(call["url"] for call in self.calls))

    def test_malformed_pagination_fails_closed(self):
        client = self._client([
            (200, json.dumps({"items": [], "pagination": {"max_page": 2}}).encode()),
        ])

        with self.assertRaises(PolarAPIError):
            client.list_subscriptions(external_customer_id="ten_" + "b" * 32)

    def test_immediate_revoke_uses_delete_and_returns_validated_subscription(self):
        client = self._client([
            (200, json.dumps({"id": "sub_live", "status": "canceled"}).encode()),
        ])

        result = client.revoke_subscription("sub_live")

        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.calls[0]["method"], "DELETE")
        self.assertTrue(self.calls[0]["url"].endswith("/v1/subscriptions/sub_live"))

    def test_identifiers_cannot_escape_the_provider_path(self):
        client = self._client([])

        with self.assertRaises(ValueError):
            client.revoke_subscription("../customers")


if __name__ == "__main__":
    unittest.main()
