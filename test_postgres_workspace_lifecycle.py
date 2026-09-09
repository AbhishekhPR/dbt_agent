from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone


DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _reset_schema():
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")


@unittest.skipUnless(DSN, "requires real PostgreSQL")
class PostgresWorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        _reset_schema()
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        self.store = PostgresLifecycleStore(DSN)
        self._workspace("a", 9, 101)
        self._workspace("b", 10, 202)

    def tearDown(self):
        self.store.close()

    def _workspace(self, suffix, installation_id, repository_id):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            f"org_clerk_{suffix}", organization_name=f"Workspace {suffix}")
        tenant_id = tenant["tenant_id"]
        self.store.ensure_tenant(f"legacy-{suffix}", f"repo-{suffix}", "prod")
        self.store.record_github_installation(
            installation_id, github_account_id=installation_id,
            github_account_login=f"account-{suffix}",
            github_account_type="Organization", github_app_id=1,
            repository_selection="selected")
        self.store.bind_github_installation_to_tenant(
            installation_id, tenant_id=tenant_id,
            bound_by_clerk_user_id=f"user_{suffix}",
            verified_github_user_id=installation_id)
        self.store.select_tenant_repository(
            repository_id, tenant_id=tenant_id,
            github_installation_id=installation_id,
            owner_login=f"account-{suffix}", name=f"repo-{suffix}")
        token_id = f"token-{suffix}"
        self.store.create_service_token(
            token_id, f"digest-{suffix}", f"legacy-{suffix}", f"repo-{suffix}",
            scope="ci")
        self.store.record_tenant_repository_ci_token_and_bind_root(
            repository_id, tenant_id=tenant_id, ci_token_id=token_id,
            delivery="display_once", issued_at=NOW, verified_at=NOW)
        return tenant_id

    def _tenant(self, suffix):
        return self.store.tenant_by_clerk_organization(f"org_clerk_{suffix}")[
            "tenant_id"]

    def test_begin_is_idempotent_and_atomically_freezes_all_admission(self):
        tenant_id = self._tenant("a")
        first = self.store.begin_workspace_deletion(
            tenant_id=tenant_id, initiated_by_clerk_user_id="user_a",
            confirmation_verified_at=NOW)
        second = self.store.begin_workspace_deletion(
            tenant_id=tenant_id, initiated_by_clerk_user_id="user_a",
            confirmation_verified_at=NOW)

        self.assertEqual(first["operation_id"], second["operation_id"])
        control = self.store.connection.execute(
            "SELECT * FROM tenant_lifecycle_controls WHERE tenant_id=%s",
            (tenant_id,)).fetchone()
        self.assertEqual(control["workspace_state"], "deleting")
        self.assertEqual(control["work_admission_state"], "blocked")
        self.assertEqual(control["billing_checkout_state"], "blocked")
        with self.assertRaises(ValueError):
            self.store.create_service_token(
                "late", "digest", "legacy-a", "repo-a", scope="ci")
        self.assertIsNone(self.store.workspace_lifecycle_operation_for_tenant(
            self._tenant("b"), first["operation_id"]))

    def test_begin_refuses_incomplete_operational_ownership_without_freezing(self):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_unmapped", organization_name="Unmapped")
        tenant_id = tenant["tenant_id"]
        self.store.ensure_tenant("legacy-unmapped", "repo-unmapped", "prod")
        self.store.record_github_installation(
            99, github_account_id=99, github_account_login="unmapped",
            github_account_type="Organization", github_app_id=1,
            repository_selection="selected")
        self.store.bind_github_installation_to_tenant(
            99, tenant_id=tenant_id, bound_by_clerk_user_id="user_unmapped",
            verified_github_user_id=99)
        self.store.select_tenant_repository(
            999, tenant_id=tenant_id, github_installation_id=99,
            owner_login="unmapped", name="repo-unmapped")
        self.store.create_service_token(
            "token-unmapped", "digest", "legacy-unmapped", "repo-unmapped",
            scope="ci")
        self.store.record_tenant_repository_ci_token(
            999, tenant_id=tenant_id, ci_token_id="token-unmapped",
            delivery="display_once", issued_at=NOW)
        with self.assertRaisesRegex(ValueError, "operational_ownership_incomplete"):
            self.store.begin_workspace_deletion(
                tenant_id=tenant_id,
                initiated_by_clerk_user_id="user_unmapped",
                confirmation_verified_at=NOW)
        row = self.store.connection.execute(
            "SELECT workspace_state FROM tenant_lifecycle_controls WHERE tenant_id=%s",
            (tenant_id,)).fetchone()
        self.assertTrue(row is None or row["workspace_state"] == "active")

    def test_guarded_purge_deletes_all_a_evidence_and_preserves_b(self):
        tenant_id = self._tenant("a")
        operation = self.store.begin_workspace_deletion(
            tenant_id=tenant_id, initiated_by_clerk_user_id="user_a",
            confirmation_verified_at=NOW)
        for suffix in ("a", "b"):
            self.store.connection.execute(
                "INSERT INTO manifest_evidence "
                "(organization_id,repository_id,evidence_id,commit_sha,manifest_hash,"
                "manifest,idempotency_key,payload_hash) VALUES "
                "(%s,%s,%s,%s,%s,'{}',%s,%s)",
                (f"legacy-{suffix}", f"repo-{suffix}", f"manifest-{suffix}",
                 ("a" if suffix == "a" else "b") * 40, "c" * 64,
                 f"idem-{suffix}", "d" * 64))
            self.store.connection.execute(
                "INSERT INTO metadata_snapshots "
                "(organization_id,repository_id,snapshot_id,environment,"
                "evidence_hash,idempotency_key,payload_hash,observed_at,collected_at) "
                "VALUES (%s,%s,%s,'prod',%s,%s,%s,%s,%s)",
                (f"legacy-{suffix}", f"repo-{suffix}", f"snapshot-{suffix}",
                 "e" * 64, f"snap-idem-{suffix}", "f" * 64, NOW, NOW))
        self.store.connection.execute(
            "UPDATE tenant_lifecycle_controls SET credential_state='revoked' "
            "WHERE tenant_id=%s", (tenant_id,))
        self.store.connection.execute(
            "UPDATE workspace_lifecycle_operations SET phase='artifact_purge', "
            "billing_terminal_verified_at=%s, github_terminal_verified_at=%s, "
            "credentials_revoked_at=%s WHERE operation_id=%s",
            (NOW, NOW, NOW, operation["operation_id"]))

        result = self.store.purge_workspace_operational_data(
            tenant_id=tenant_id, operation_id=operation["operation_id"])

        self.assertGreater(result["operational_records_deleted"], 0)
        for table in ("manifest_evidence", "metadata_snapshots", "organizations"):
            self.assertEqual(self.store.connection.execute(
                f"SELECT count(*) AS n FROM {table} WHERE organization_id='legacy-a'"
            ).fetchone()["n"], 0)
            self.assertGreater(self.store.connection.execute(
                f"SELECT count(*) AS n FROM {table} WHERE organization_id='legacy-b'"
            ).fetchone()["n"], 0)
        self.assertIsNotNone(self.store.tenant_by_id(tenant_id))

    def test_finalize_leaves_only_dissociated_receipt_for_workspace(self):
        tenant_id = self._tenant("a")
        operation = self.store.begin_workspace_deletion(
            tenant_id=tenant_id, initiated_by_clerk_user_id="user_a",
            confirmation_verified_at=NOW)
        self.store.connection.execute(
            "UPDATE tenant_lifecycle_controls SET credential_state='revoked' "
            "WHERE tenant_id=%s", (tenant_id,))
        self.store.connection.execute(
            "UPDATE workspace_lifecycle_operations SET phase='artifact_purge', "
            "billing_terminal_verified_at=%s, github_terminal_verified_at=%s, "
            "credentials_revoked_at=%s WHERE operation_id=%s",
            (NOW, NOW, NOW, operation["operation_id"]))
        self.store.purge_workspace_operational_data(
            tenant_id=tenant_id, operation_id=operation["operation_id"])
        self.store.set_workspace_lifecycle_phase(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            expected_phases={"artifact_purge"}, phase="database_purge",
            updated_at=NOW, operational_records_deleted=1)
        self.store.set_workspace_lifecycle_phase(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            expected_phases={"database_purge"},
            phase="clerk_organization_deletion", updated_at=NOW)

        receipt = self.store.finalize_workspace_deletion(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            completed_at=NOW)

        self.assertEqual(receipt["receipt_id"], operation["operation_id"])
        self.assertIsNone(self.store.tenant_by_id(tenant_id))
        self.assertIsNotNone(self.store.tenant_by_id(self._tenant("b")))
        columns = {row["column_name"] for row in self.store.connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='deletion_receipts'").fetchall()}
        self.assertTrue(columns.isdisjoint({
            "tenant_id", "clerk_user_id", "organization_id",
            "polar_customer_id", "github_installation_id"}))


if __name__ == "__main__":
    unittest.main()
