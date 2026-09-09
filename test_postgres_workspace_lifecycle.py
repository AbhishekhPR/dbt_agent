from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone


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

        claimed = self.store.claim_workspace_lifecycle_operation(
            tenant_id=tenant_id, operation_id=first["operation_id"],
            lease_id="lease_first", now=NOW,
            lease_expires_at=NOW.replace(minute=2))
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.store.claim_workspace_lifecycle_operation(
            tenant_id=tenant_id, operation_id=first["operation_id"],
            lease_id="lease_second", now=NOW,
            lease_expires_at=NOW.replace(minute=3)))
        self.store.release_workspace_lifecycle_operation(
            tenant_id=tenant_id, operation_id=first["operation_id"],
            lease_id="lease_first")

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

    def test_account_deletion_preserves_every_shared_workspace(self):
        tenant_a, tenant_b = self._tenant("a"), self._tenant("b")
        self.store.upsert_clerk_github_identity(
            "shared_user", github_user_id=44, github_login="shared",
            access_token="encrypted-before-delete")
        self.store.create_github_installation_state(
            state_hash="b" * 64, tenant_id=tenant_a,
            clerk_user_id="shared_user", expires_at=NOW + timedelta(minutes=5),
            created_at=NOW)
        operation = self.store.begin_account_deletion(
            clerk_user_id="shared_user",
            memberships=({"clerk_organization_id": "org_clerk_a",
                          "clerk_membership_id": "mem_a",
                          "authoritative_role": "member", "active_owner_count": 1},
                         {"clerk_organization_id": "org_clerk_b",
                          "clerk_membership_id": "mem_b",
                          "authoritative_role": "owner", "active_owner_count": 2}),
            dissociated_actor_ref="deleted_actor_0123456789abcdef0123456789abcdef",
            confirmation_verified_at=NOW)
        with self.assertRaisesRegex(ValueError, "account lifecycle"):
            self.store.consume_github_installation_state(
                "b" * 64, now=NOW + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "account lifecycle"):
            self.store.upsert_clerk_github_identity(
                "shared_user", github_user_id=44, github_login="shared",
                access_token="encrypted-test-value")
        with self.assertRaisesRegex(ValueError, "account lifecycle"):
            self.store.create_github_installation_state(
                state_hash="a" * 64, tenant_id=tenant_a,
                clerk_user_id="shared_user", expires_at=NOW.replace(minute=5),
                created_at=NOW)
        with self.assertRaisesRegex(ValueError, "account lifecycle"):
            self.store.create_dashboard_session(
                "session-hash", organization_id="legacy-a",
                repository_id="repo-a", environment="prod",
                github_login="shared", github_user_id=44,
                github_permission="push", may_govern=True,
                permission_checked_at=NOW, csrf_token="csrf", expires_at=NOW,
                source_clerk_user_id="shared_user")
        for organization_id in ("org_clerk_a", "org_clerk_b"):
            self.store.mark_account_membership_absent(
                operation_id=operation["operation_id"],
                organization_id=organization_id, updated_at=NOW)
        self.assertEqual(self.store.revoke_account_local_access(
            "shared_user", operation["dissociated_actor_ref"])["state"], "revoked")
        self.store.set_account_lifecycle_phase(
            operation_id=operation["operation_id"],
            expected_phases={"leaving_workspaces"}, phase="credentials_revoked",
            updated_at=NOW)
        self.store.set_account_lifecycle_phase(
            operation_id=operation["operation_id"],
            expected_phases={"credentials_revoked"}, phase="clerk_user_deletion",
            updated_at=NOW)
        receipt = self.store.finalize_account_deletion(
            operation_id=operation["operation_id"], completed_at=NOW)
        self.assertEqual(receipt["state"], "completed")
        self.assertIsNotNone(self.store.tenant_by_id(tenant_a))
        self.assertIsNotNone(self.store.tenant_by_id(tenant_b))

    def test_expired_workspace_lease_cannot_write_after_takeover(self):
        tenant_id = self._tenant("a")
        operation = self.store.begin_workspace_deletion(
            tenant_id=tenant_id, initiated_by_clerk_user_id="user_a",
            confirmation_verified_at=NOW)
        first = self.store.claim_workspace_lifecycle_operation(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            lease_id="lease_first", now=NOW,
            lease_expires_at=NOW + timedelta(seconds=1))
        second = self.store.claim_workspace_lifecycle_operation(
            tenant_id=tenant_id, operation_id=operation["operation_id"],
            lease_id="lease_second", now=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(minutes=2))
        self.assertIsNotNone(second)
        with self.assertRaisesRegex(ValueError, "not scoped"):
            self.store.record_workspace_lifecycle_provider_result(
                operation_id=operation["operation_id"], tenant_id=tenant_id,
                provider="github", target_kind="installation",
                target_reference="9", outcome="verified_absent",
                failure_category=None, recorded_at=NOW,
                expected_generation=first["generation"], lease_id="lease_first")
        current = self.store.workspace_lifecycle_operation_for_tenant(
            tenant_id, operation["operation_id"])
        self.assertEqual(current["lease_id"], "lease_second")

    def test_collector_and_repository_revocation_are_tenant_isolated(self):
        tenant_a = self._tenant("a")
        self.store.create_service_token(
            "collector-a", "collector-digest-a", "legacy-a", "repo-a",
            environment="prod", scope="collector")
        self.store.create_service_token(
            "collector-b", "collector-digest-b", "legacy-b", "repo-b",
            environment="prod", scope="collector")
        self.store.register_collector(
            "legacy-a", "repo-a", "prod", collector_id="collector_a",
            token_id="collector-a")
        self.store.register_collector(
            "legacy-b", "repo-b", "prod", collector_id="collector_b",
            token_id="collector-b")
        result = self.store.revoke_tenant_collector_access(
            tenant_id=tenant_a, token_id="collector-a",
            initiated_by_clerk_user_id="user_a")
        self.assertEqual(result["tokens_revoked"], 1)
        token_b = self.store.connection.execute(
            "SELECT secret_hash,revoked_at FROM api_service_tokens "
            "WHERE token_id='collector-b'").fetchone()
        self.assertIsNotNone(token_b["secret_hash"])
        self.assertIsNone(token_b["revoked_at"])

        self.store.create_service_token(
            "collector-a-2", "collector-digest-a-2", "legacy-a", "repo-a",
            environment="prod", scope="collector")
        self.store.register_collector(
            "legacy-a", "repo-a", "prod", collector_id="collector_a_2",
            token_id="collector-a-2")
        self.store.create_collection_request(
            "legacy-a", "repo-a", "prod", request_id="request-a",
            reason="test", expires_at=NOW + timedelta(hours=1),
            targets=({"relation_name": "orders"},))

        operation = self.store.begin_github_access_operation(
            tenant_id=tenant_a, initiated_by_clerk_user_id="user_a",
            operation_kind="repository_disconnect", github_repository_id=101)
        self.assertEqual(operation["state"], "completed")
        repository_a = self.store.connection.execute(
            "SELECT disconnected_at,ci_token_id FROM tenant_repositories "
            "WHERE github_repository_id=101").fetchone()
        repository_b = self.store.connection.execute(
            "SELECT disconnected_at,ci_token_id FROM tenant_repositories "
            "WHERE github_repository_id=202").fetchone()
        self.assertIsNotNone(repository_a["disconnected_at"])
        self.assertIsNone(repository_a["ci_token_id"])
        self.assertIsNone(repository_b["disconnected_at"])
        self.assertEqual(repository_b["ci_token_id"], "token-b")
        collector_a_2 = self.store.connection.execute(
            "SELECT secret_hash,revoked_at FROM api_service_tokens "
            "WHERE token_id='collector-a-2'").fetchone()
        self.assertIsNone(collector_a_2["secret_hash"])
        self.assertIsNotNone(collector_a_2["revoked_at"])
        self.assertFalse(self.store.connection.execute(
            "SELECT connected FROM environments WHERE organization_id='legacy-a' "
            "AND repository_id='repo-a' AND environment='prod'").fetchone()["connected"])
        request = self.store.get_collection_request(
            "legacy-a", "repo-a", "request-a")
        self.assertEqual(request["state"], "CANCELED")
        identity = self.store.get_collector(
            "legacy-a", "repo-a", "collector_a_2")
        self.assertTrue(identity["revoked"])
        self.assertIsNone(identity["token_id"])


if __name__ == "__main__":
    unittest.main()
