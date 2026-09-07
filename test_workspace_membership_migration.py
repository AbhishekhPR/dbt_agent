from __future__ import annotations

import os
import unittest
from pathlib import Path


MIGRATION = Path("agent/migrations/postgres/0020_tenant_memberships.sql")
DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


class WorkspaceMembershipMigrationContractTests(unittest.TestCase):
    def test_migration_defines_projection_and_sync_state_without_backfill(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_memberships", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tenant_membership_sync_state", sql)
        self.assertIn("PRIMARY KEY (tenant_id, clerk_user_id)", sql)
        self.assertIn("REFERENCES tenants (tenant_id) ON DELETE CASCADE", sql)
        self.assertIn("CHECK (role IN ('owner', 'admin', 'member'))", sql)
        self.assertNotIn("INSERT INTO tenant_memberships", sql)
        self.assertNotIn("tenants.role", sql)


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class WorkspaceMembershipPostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")

    def test_constraints_and_tenant_cascade_are_enforced(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_membership_test", organization_name="Membership test")
            tenant_id = tenant["tenant_id"]
            store.connection.execute(
                "INSERT INTO tenant_membership_sync_state "
                "(tenant_id, sync_generation, sync_status, ownership_status, "
                " active_owner_count, source_fingerprint, last_attempted_at, "
                " last_synchronized_at) "
                "VALUES (%s, 'sync_one', 'synchronized', 'authoritative', 1, %s, now(), now())",
                (tenant_id, "a" * 64),
            )
            store.connection.execute(
                "INSERT INTO tenant_memberships "
                "(tenant_id, clerk_user_id, clerk_membership_id, role, clerk_role_key, "
                " role_basis, sync_generation, status, synchronized_at) "
                "VALUES (%s, 'user_owner', 'mem_owner', 'owner', 'org:owner', "
                " 'explicit_clerk_role', 'sync_one', 'active', now())",
                (tenant_id,),
            )
            with self.assertRaises(Exception):
                store.connection.execute(
                    "INSERT INTO tenant_memberships "
                    "(tenant_id, clerk_user_id, clerk_membership_id, role, clerk_role_key, "
                    " role_basis, sync_generation, status, synchronized_at) "
                    "VALUES (%s, 'user_bad', 'mem_bad', 'superuser', 'superuser', "
                    " 'clerk_membership', 'sync_one', 'active', now())",
                    (tenant_id,),
                )
            store.connection.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant_id,))
            remaining = store.connection.execute(
                "SELECT count(*) AS n FROM tenant_memberships WHERE tenant_id = %s",
                (tenant_id,),
            ).fetchone()["n"]
            self.assertEqual(remaining, 0)
        finally:
            store.close()

    def test_projection_replacement_is_atomic_and_marks_absent_members_removed(self):
        from datetime import datetime, timezone

        from agent.api.workspace_membership import (
            ClerkMembership, ClerkOrganizationSnapshot, project_memberships,
        )
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_projection_test", organization_name="Projection test")
            tenant_id = tenant["tenant_id"]
            first = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_projection_test",
                created_by_user_id="user_owner",
                memberships=(
                    ClerkMembership("user_owner", "mem_owner", "org:admin"),
                    ClerkMembership("user_member", "mem_member", "org:member"),
                ),
            ))
            now = datetime.now(timezone.utc)
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_projection_test",
                sync_generation="generation_one")
            store.replace_tenant_membership_projection(
                tenant_id=tenant_id, projection=first,
                sync_generation="generation_one", synchronized_at=now,
            )
            context = store.tenant_membership_authorization(
                tenant_id=tenant_id, clerk_user_id="user_owner",
                sync_generation="generation_one",
            )
            self.assertEqual(context["role"], "owner")
            self.assertEqual(context["active_owner_count"], 1)

            second = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_projection_test",
                created_by_user_id="user_owner",
                memberships=(ClerkMembership(
                    "user_owner", "mem_owner", "org:owner"),),
            ))
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_projection_test",
                sync_generation="generation_two")
            store.replace_tenant_membership_projection(
                tenant_id=tenant_id, projection=second,
                sync_generation="generation_two", synchronized_at=now,
            )
            removed = store.connection.execute(
                "SELECT status, sync_generation FROM tenant_memberships "
                "WHERE tenant_id=%s AND clerk_user_id='user_member'",
                (tenant_id,),
            ).fetchone()
            self.assertEqual(dict(removed), {
                "status": "removed", "sync_generation": "generation_two"})
            self.assertIsNone(store.tenant_membership_authorization(
                tenant_id=tenant_id, clerk_user_id="user_member",
                sync_generation="generation_two",
            ))
        finally:
            store.close()

    def test_projection_cannot_be_written_under_a_different_tenant(self):
        from datetime import datetime, timezone

        from agent.api.workspace_membership import (
            ClerkMembership, ClerkOrganizationSnapshot, project_memberships,
        )
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_real", organization_name="Real")
            projection = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_attacker",
                created_by_user_id="user_attacker",
                memberships=(ClerkMembership(
                    "user_attacker", "mem_attacker", "org:owner"),),
            ))
            store.begin_tenant_membership_sync(
                tenant_id=tenant["tenant_id"], clerk_organization_id="org_real",
                sync_generation="generation_attack")
            with self.assertRaises(ValueError):
                store.replace_tenant_membership_projection(
                    tenant_id=tenant["tenant_id"], projection=projection,
                    sync_generation="generation_attack",
                    synchronized_at=datetime.now(timezone.utc),
                )
            count = store.connection.execute(
                "SELECT count(*) AS n FROM tenant_memberships WHERE tenant_id=%s",
                (tenant["tenant_id"],),
            ).fetchone()["n"]
            self.assertEqual(count, 0)
        finally:
            store.close()

    def test_older_refresh_cannot_overwrite_a_newer_completed_projection(self):
        from datetime import datetime, timedelta, timezone

        from agent.api.workspace_membership import (
            ClerkMembership, ClerkOrganizationSnapshot, project_memberships,
        )
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_ordering", organization_name="Ordering")
            tenant_id = tenant["tenant_id"]
            base = datetime.now(timezone.utc)
            newer = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_ordering", created_by_user_id="user_owner",
                memberships=(ClerkMembership(
                    "user_owner", "mem_owner", "org:owner"),),
            ))
            older = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_ordering", created_by_user_id=None,
                memberships=(ClerkMembership(
                    "user_owner", "mem_owner", "org:member"),),
            ))
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_ordering",
                sync_generation="generation_older")
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_ordering",
                sync_generation="generation_newer")
            store.replace_tenant_membership_projection(
                tenant_id=tenant_id, projection=newer,
                sync_generation="generation_newer",
                synchronized_at=base + timedelta(seconds=1),
            )
            with self.assertRaises(ValueError):
                store.replace_tenant_membership_projection(
                    tenant_id=tenant_id, projection=older,
                    sync_generation="generation_older", synchronized_at=base,
                )
            row = store.connection.execute(
                "SELECT role, sync_generation FROM tenant_memberships "
                "WHERE tenant_id=%s AND clerk_user_id='user_owner'",
                (tenant_id,),
            ).fetchone()
            self.assertEqual(dict(row), {
                "role": "owner", "sync_generation": "generation_newer"})
        finally:
            store.close()

    def test_older_failed_attempt_cannot_poison_a_newer_success(self):
        from datetime import datetime, timedelta, timezone

        from agent.api.workspace_membership import (
            ClerkMembership, ClerkOrganizationSnapshot, project_memberships,
        )
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore(DSN)
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_failure_order", organization_name="Failure order")
            tenant_id = tenant["tenant_id"]
            base = datetime.now(timezone.utc)
            projection = project_memberships(ClerkOrganizationSnapshot(
                organization_id="org_failure_order",
                created_by_user_id="user_owner",
                memberships=(ClerkMembership(
                    "user_owner", "mem_owner", "org:owner"),),
            ))
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_failure_order",
                sync_generation="generation_old_failure")
            store.begin_tenant_membership_sync(
                tenant_id=tenant_id, clerk_organization_id="org_failure_order",
                sync_generation="generation_success")
            store.replace_tenant_membership_projection(
                tenant_id=tenant_id, projection=projection,
                sync_generation="generation_success",
                synchronized_at=base + timedelta(seconds=1),
            )
            store.record_tenant_membership_sync_failure(
                tenant_id=tenant_id, sync_generation="generation_old_failure",
                attempted_at=base, failure_category="authority_unavailable",
            )
            state = store.connection.execute(
                "SELECT sync_status, sync_generation, failure_category "
                "FROM tenant_membership_sync_state WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()
            self.assertEqual(dict(state), {
                "sync_status": "synchronized",
                "sync_generation": "generation_success",
                "failure_category": None,
            })
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
