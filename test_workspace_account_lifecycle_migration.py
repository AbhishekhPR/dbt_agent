from pathlib import Path
import unittest


MIGRATION = Path("agent/migrations/postgres/0025_workspace_account_lifecycle.sql")


class WorkspaceAccountLifecycleMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_adds_durable_operations_and_dissociated_receipts(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS workspace_lifecycle_operations", self.sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_lifecycle_operations", self.sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS deletion_receipts", self.sql)
        receipt = self.sql.split("CREATE TABLE IF NOT EXISTS deletion_receipts", 1)[1]
        receipt = receipt.split(");", 1)[0]
        for forbidden in ("tenant_id", "clerk_user_id", "organization_id",
                          "github_installation_id", "polar_customer_id",
                          "polar_subscription_id"):
            self.assertNotIn(forbidden, receipt)

    def test_only_one_unfinished_operation_is_allowed_per_scope(self):
        self.assertIn("uq_workspace_lifecycle_active_tenant", self.sql)
        self.assertIn("uq_account_lifecycle_active_user", self.sql)
        self.assertGreaterEqual(self.sql.count("WHERE completed_at IS NULL"), 2)

    def test_authoritative_bridge_remains_restrictive(self):
        self.assertNotIn("DROP CONSTRAINT tenant_operational_roots", self.sql)
        self.assertNotIn("ALTER TABLE tenant_operational_roots", self.sql)

    def test_manifest_deletion_requires_transaction_local_guard_and_deleting_state(self):
        self.assertIn("relium.lifecycle_purge_tenant", self.sql)
        self.assertIn("workspace_state = 'deleting'", self.sql)
        self.assertIn("TG_OP = 'DELETE'", self.sql)
        self.assertIn("CREATE OR REPLACE FUNCTION relium_reject_snapshot_mutation", self.sql)

    def test_freeze_states_are_database_constrained(self):
        self.assertIn("workspace_state IN ('active', 'frozen', 'deleting')", self.sql)
        self.assertIn("work_admission_state IN ('active', 'blocked')", self.sql)


if __name__ == "__main__":
    unittest.main()
