import sqlite3
import unittest


class DeliveryJournalTests(unittest.TestCase):
    def setUp(self):
        from agent.delivery_journal import DeliveryJournal
        self.journal = DeliveryJournal(sqlite3.connect(":memory:"))
        self.journal.ensure_schema()

    def test_channels_are_independent_and_deduplicated(self):
        first = self.journal.record("org", "repo", "prod", channel="github", event_key="inc-1", payload={"message": "BLOCK"})
        duplicate = self.journal.record("org", "repo", "prod", channel="github", event_key="inc-1", payload={"message": "BLOCK"})
        slack = self.journal.record("org", "repo", "prod", channel="slack", event_key="inc-1", payload={"message": "BLOCK"})
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertNotEqual(first["journal_id"], slack["journal_id"])

    def test_slack_disabled_is_optional_and_redacted(self):
        result = self.journal.record("org", "repo", "prod", channel="slack", event_key="inc-2", payload={"token": "secret", "sql": "select *"}, enabled=False)
        self.assertEqual(result["status"], "DISABLED")
        self.assertNotIn("secret", str(self.journal.list("org", "repo", "prod")))

    def test_retry_is_bounded(self):
        result = self.journal.record("org", "repo", "prod", channel="dashboard", event_key="inc-3", payload={}, max_attempts=2)
        self.journal.fail(result["journal_id"])
        self.journal.fail(result["journal_id"])
        self.assertEqual(self.journal.get(result["journal_id"])["status"], "DEAD_LETTER")


class DashboardContractTests(unittest.TestCase):
    def test_dashboard_resources_have_stable_contract_names(self):
        from agent.dashboard_contracts import DASHBOARD_RESOURCES
        self.assertIn("review_detail", DASHBOARD_RESOURCES)
        self.assertIn("incident_detail", DASHBOARD_RESOURCES)


if __name__ == "__main__":
    unittest.main()
