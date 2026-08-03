import json
import tempfile
import unittest
from pathlib import Path


class LifecycleMigrationTests(unittest.TestCase):
    def test_pilot_export_preserves_hashes_and_schema_boundary(self):
        from agent.migrate_pilot_store import export_pilot_store, reconcile_export

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"job_id": "j1"}), encoding="utf-8")
            exported = export_pilot_store(tmp)
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["file_count"], 1)
        self.assertTrue(exported["files"][0]["sha256"])
        self.assertTrue(reconcile_export(exported, 1)["matched"])

    def test_postgres_store_requires_explicit_credentials(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with self.assertRaisesRegex(RuntimeError, "BLOCKED BY CREDENTIALS"):
            PostgresLifecycleStore(None)


if __name__ == "__main__":
    unittest.main()
