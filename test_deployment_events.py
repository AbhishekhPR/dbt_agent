import sqlite3
import unittest


class DeploymentEventTests(unittest.TestCase):
    def setUp(self):
        from agent.deployment_events import DeploymentEventProcessor
        from agent.sqlite_lifecycle_store import SQLiteLifecycleStore
        self.store = SQLiteLifecycleStore(sqlite3.connect(":memory:"))
        self.store.ensure_schema()
        self.store.ensure_tenant("org", "repo", "prod")
        self.processor = DeploymentEventProcessor(self.store)

    def event(self, event_id, event_type, deployment_id="dep-1"):
        return {"event_id": event_id, "event_type": event_type, "organization_id": "org", "repository_id": "repo", "environment": "prod", "deployment_id": deployment_id, "payload": {"merge_sha": "abc"}}

    def test_public_events_follow_exact_lifecycle_and_are_idempotent(self):
        self.processor.process(self.event("e1", "reviewed"))
        self.processor.process(self.event("e2", "approved"))
        self.processor.process(self.event("e3", "deployment_started"))
        self.processor.process(self.event("e4", "deployment_succeeded"))
        self.processor.process(self.event("e5", "post_deployment_monitoring"))
        result = self.processor.process(self.event("e6", "healthy"))
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(self.processor.process(self.event("e6", "healthy"))["duplicate"], True)

    def test_out_of_order_event_is_retained_and_rejected(self):
        result = self.processor.process(self.event("e1", "deployment_succeeded"))
        self.assertEqual(result["status"], "REJECTED_OUT_OF_ORDER")
        self.assertEqual(result["event_id"], "e1")

    def test_missing_tenant_and_bad_payload_are_rejected(self):
        missing = self.event("e1", "reviewed")
        missing["organization_id"] = "unknown"
        result = self.processor.process(missing)
        self.assertEqual(result["status"], "REJECTED_TENANT")
        bad = self.event("e2", "reviewed")
        bad.pop("deployment_id")
        self.assertEqual(self.processor.process(bad)["status"], "REJECTED_PAYLOAD")

    def test_public_api_delegates_without_internal_method_access(self):
        from agent.deployment_api import DeploymentEventAPI
        api = DeploymentEventAPI(self.processor)
        response = api.post(self.event("api-1", "reviewed"))
        self.assertEqual(response["status"], "reviewed")


if __name__ == "__main__":
    unittest.main()
