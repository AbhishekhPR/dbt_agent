import sqlite3
import unittest


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        from agent.sqlite_lifecycle_store import SQLiteLifecycleStore
        self.store = SQLiteLifecycleStore(sqlite3.connect(":memory:"))
        self.store.ensure_schema()
        self.store.ensure_tenant("org-1", "repo-1", "prod")
        self.store.ensure_tenant("org-2", "repo-2", "prod")

    def test_evidence_is_immutable_and_tenant_scoped(self):
        evidence = self.store.append_evidence("org-1", "repo-1", "prod", {"kind": "manifest", "hash": "abc"})
        self.assertEqual(evidence["payload"]["hash"], "abc")
        with self.assertRaises(ValueError):
            self.store.append_evidence("org-1", "repo-1", "prod", {"kind": "manifest", "hash": "abc"}, evidence_id=evidence["evidence_id"])
        self.assertEqual(self.store.list_evidence("org-2", "repo-2", "prod"), [])

    def test_deployment_transitions_are_append_only_and_idempotent(self):
        deployment = self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-1", "merge_sha": "sha"})
        self.assertEqual(deployment["deployment_id"], "dep-1")
        self.assertEqual(self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-1", "merge_sha": "sha"}), deployment)
        self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "reviewed")
        self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "approved")
        with self.assertRaises(ValueError):
            self.store.append_transition("org-1", "repo-1", "prod", "dep-1", "reviewed")
        self.assertEqual(len(self.store.transitions("org-1", "repo-1", "prod", "dep-1")), 1)

    def test_outbox_claim_is_idempotent_and_tenant_scoped(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-2"})
        event = self.store.claim_outbox("org-1", "repo-1", "prod", "worker-1")
        self.assertEqual(event["deployment_id"], "dep-2")
        self.assertIsNone(self.store.claim_outbox("org-1", "repo-1", "prod", "worker-2"))
        self.assertIsNone(self.store.claim_outbox("org-2", "repo-2", "prod", "worker-1"))

    def test_policy_detector_and_threshold_versions_are_persisted(self):
        versions = self.store.record_versions("org-1", "repo-1", "prod", policy="policy-v1", detector="detector-v1", threshold="threshold-v1")
        self.assertEqual(versions["policy_version"], "policy-v1")
        self.assertEqual(self.store.latest_versions("org-1", "repo-1", "prod")["detector_version"], "detector-v1")

    def test_disconnect_and_delete_tombstone(self):
        self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-3"})
        self.store.disconnect_repository("org-1", "repo-1")
        with self.assertRaises(ValueError):
            self.store.create_deployment("org-1", "repo-1", "prod", {"deployment_id": "dep-4"})
        tombstone = self.store.delete_tenant("org-1")
        self.assertEqual(tombstone["organization_id"], "org-1")
        self.assertEqual(self.store.list_evidence("org-1", "repo-1", "prod"), [])


if __name__ == "__main__":
    unittest.main()
