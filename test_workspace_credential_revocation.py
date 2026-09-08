from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace


MIGRATION = Path(
    "agent/migrations/postgres/0024_workspace_credential_revocation.sql")
DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


class CredentialRevocationMigrationContractTests(unittest.TestCase):
    def test_schema_supports_digest_destruction_cancellation_and_provenance(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS workspace_credential_revocations", sql)
        self.assertIn("ALTER COLUMN secret_hash DROP NOT NULL", sql)
        self.assertIn("'CANCELED'", sql)
        self.assertIn("source_clerk_user_id TEXT", sql)
        self.assertIn("tenant_repositories_ci_token_fk", sql)
        self.assertIn("collector_identities_token_fk", sql)
        self.assertGreaterEqual(sql.count("NOT VALID"), 2)
        self.assertNotIn("DELETE FROM reviews", sql)
        self.assertNotIn("DELETE FROM evidence", sql)


class _Authorizer:
    def __init__(self, tenant_id="ten_" + "a" * 32, error=None):
        self.tenant_id = tenant_id
        self.error = error

    def require_owner(self, principal):
        if self.error:
            raise self.error
        return SimpleNamespace(tenant_id=self.tenant_id,
                               clerk_user_id="user_owner")


class _Store:
    def __init__(self, inventory=None):
        self.inventory = inventory or {
            "ownership_status": "complete", "operational_roots": ["root-a"]}
        self.calls = []

    def tenant_operational_inventory(self, tenant_id):
        self.calls.append(("inventory", tenant_id))
        return self.inventory

    def revoke_workspace_credentials_for_tenant(self, **values):
        self.calls.append(("revoke", values))
        return {"state": "revoked", "service_tokens_revoked": 2,
                "claimed_work_remaining": 0}


class CredentialRevocationAuthorizationTests(unittest.TestCase):
    def test_owner_context_is_the_only_tenant_scope(self):
        from agent.workspace_credential_revocation import revoke_workspace_credentials

        store = _Store()
        result = revoke_workspace_credentials(
            principal=SimpleNamespace(tenant_id="ten_" + "b" * 32),
            authorizer=_Authorizer(), store=store)

        self.assertEqual(result["state"], "revoked")
        self.assertEqual(store.calls[0], ("inventory", "ten_" + "a" * 32))
        self.assertEqual(store.calls[1][1]["tenant_id"], "ten_" + "a" * 32)

    def test_incomplete_ownership_fails_before_mutation(self):
        from agent.workspace_credential_revocation import (
            CredentialRevocationError, revoke_workspace_credentials,
        )
        store = _Store({"ownership_status": "incomplete",
                        "operational_roots": ["root-a"]})

        with self.assertRaises(CredentialRevocationError):
            revoke_workspace_credentials(
                principal=object(), authorizer=_Authorizer(), store=store)

        self.assertEqual(store.calls, [("inventory", "ten_" + "a" * 32)])

    def test_authorization_failure_precedes_inventory(self):
        from agent.workspace_credential_revocation import revoke_workspace_credentials
        store = _Store()

        with self.assertRaisesRegex(RuntimeError, "not owner"):
            revoke_workspace_credentials(
                principal=object(), authorizer=_Authorizer(error=RuntimeError(
                    "not owner")), store=store)

        self.assertEqual(store.calls, [])


@unittest.skipUnless(DSN, "PostgreSQL credential revocation tests require a server")
class CredentialRevocationPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.store = PostgresLifecycleStore(DSN)
        self.first = self._fixture("a", 1001)
        self.second = self._fixture("b", 1002)

    def tearDown(self):
        self.store.close()

    def _fixture(self, suffix, github_repository_id):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            f"clerk_org_{suffix}", organization_name=f"Tenant {suffix}")
        tenant_id = tenant["tenant_id"]
        root = f"root-{suffix}"
        repository = f"repo-{suffix}"
        token = f"token-{suffix}"
        connection = self.store.connection
        connection.execute("INSERT INTO organizations (organization_id) VALUES (%s)",
                           (root,))
        connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) VALUES (%s, %s)",
            (root, repository))
        connection.execute(
            "INSERT INTO environments (organization_id, repository_id, environment) "
            "VALUES (%s, %s, 'production')", (root, repository))
        installation_id = github_repository_id + 10
        connection.execute(
            "INSERT INTO github_installations "
            "(github_installation_id, github_account_id, github_account_login, "
            " github_account_type) VALUES (%s, %s, %s, 'Organization')",
            (installation_id, installation_id + 10000, root))
        connection.execute(
            "INSERT INTO tenant_github_installations "
            "(github_installation_id, tenant_id, bound_by_clerk_user_id, "
            " verified_github_user_id) VALUES (%s, %s, %s, %s)",
            (installation_id, tenant_id, f"clerk-user-{suffix}",
             installation_id + 20000))
        connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_github_installation_id, verified_at) "
            "VALUES (%s, %s, 'verified_github_repository', %s, %s, now())",
            (root, tenant_id, github_repository_id, installation_id))
        connection.execute(
            "INSERT INTO api_service_tokens "
            "(token_id, secret_hash, organization_id, repository_id, scope) "
            "VALUES (%s, %s, %s, %s, 'ci')",
            (token, suffix * 64, root, repository))
        connection.execute(
            "INSERT INTO collector_identities "
            "(organization_id, repository_id, collector_id, environment, token_id) "
            "VALUES (%s, %s, %s, 'production', %s)",
            (root, repository, f"collector-{suffix}", token))
        connection.execute(
            "INSERT INTO tenant_repositories "
            "(github_repository_id, tenant_id, github_installation_id, owner_login, "
            " name, ci_token_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (github_repository_id, tenant_id, installation_id,
             root, repository, token))
        connection.execute(
            "INSERT INTO collection_requests "
            "(organization_id, repository_id, request_id, environment, reason, "
            " expires_at, state) VALUES (%s, %s, %s, 'production', 'test', "
            " now() + interval '1 hour', 'PENDING')",
            (root, repository, f"request-{suffix}"))
        for state in ("PENDING", "CLAIMED"):
            connection.execute(
                "INSERT INTO outbox_events "
                "(event_id, organization_id, repository_id, environment, "
                " event_type, payload, state, subject_type, subject_id, dedup_key) "
                "VALUES (%s, %s, %s, 'production', 'review.recompute', '{}'::jsonb, "
                " %s, 'review', %s, %s)",
                (f"outbox-{suffix}-{state.lower()}", root, repository, state,
                 f"review-{suffix}", state.lower()))
        connection.execute(
            "INSERT INTO dashboard_sessions "
            "(session_id_hash, organization_id, repository_id, github_login, "
            " github_permission, may_govern, permission_checked_at, "
            " github_access_token, github_refresh_token, csrf_token, expires_at, "
            " source_clerk_user_id) "
            "VALUES (%s, %s, %s, %s, 'admin', true, now(), %s, %s, %s, "
            " now() + interval '1 hour', %s)",
            (f"session-{suffix}", root, repository, f"user-{suffix}",
             b"encrypted-access", b"encrypted-refresh", f"csrf-{suffix}",
             f"clerk-user-{suffix}"))
        return {"tenant_id": tenant_id, "root": root, "repository": repository,
                "token": token, "github_repository_id": github_repository_id}

    def test_atomic_revocation_destroys_only_owned_credentials_and_cancels_work(self):
        from agent.workspace_credential_revocation import revoke_workspace_credentials

        result = revoke_workspace_credentials(
            principal=object(),
            authorizer=_Authorizer(self.first["tenant_id"]), store=self.store)

        self.assertEqual(result["state"], "revoked")
        token = self.store.connection.execute(
            "SELECT secret_hash, revoked_at FROM api_service_tokens WHERE token_id=%s",
            (self.first["token"],)).fetchone()
        self.assertIsNone(token["secret_hash"])
        self.assertIsNotNone(token["revoked_at"])
        other = self.store.connection.execute(
            "SELECT secret_hash, revoked_at FROM api_service_tokens WHERE token_id=%s",
            (self.second["token"],)).fetchone()
        self.assertEqual(other["secret_hash"], "b" * 64)
        self.assertIsNone(other["revoked_at"])
        pointer = self.store.connection.execute(
            "SELECT ci_token_id FROM tenant_repositories WHERE tenant_id=%s",
            (self.first["tenant_id"],)).fetchone()
        self.assertIsNone(pointer["ci_token_id"])
        collector = self.store.connection.execute(
            "SELECT token_id, revoked FROM collector_identities "
            "WHERE organization_id=%s", (self.first["root"],)).fetchone()
        self.assertIsNone(collector["token_id"])
        self.assertTrue(collector["revoked"])
        request = self.store.connection.execute(
            "SELECT state FROM collection_requests WHERE organization_id=%s",
            (self.first["root"],)).fetchone()
        self.assertEqual(request["state"], "CANCELED")
        outbox = {row["event_id"]: row["state"] for row in
                  self.store.connection.execute(
                      "SELECT event_id, state FROM outbox_events "
                      "WHERE organization_id=%s", (self.first["root"],)).fetchall()}
        self.assertEqual(outbox["outbox-a-pending"], "CANCELED")
        self.assertEqual(outbox["outbox-a-claimed"], "CLAIMED")
        session = self.store.connection.execute(
            "SELECT revoked_at, github_access_token, github_refresh_token "
            "FROM dashboard_sessions WHERE session_id_hash='session-a'").fetchone()
        self.assertIsNotNone(session["revoked_at"])
        self.assertIsNone(session["github_access_token"])
        self.assertIsNone(session["github_refresh_token"])
        self.assertEqual(result["claimed_work_remaining"], 1)

    def test_retry_is_idempotent(self):
        from agent.workspace_credential_revocation import revoke_workspace_credentials

        first = revoke_workspace_credentials(
            principal=object(), authorizer=_Authorizer(self.first["tenant_id"]),
            store=self.store)
        second = revoke_workspace_credentials(
            principal=object(), authorizer=_Authorizer(self.first["tenant_id"]),
            store=self.store)

        self.assertEqual(first["state"], "revoked")
        self.assertEqual(second["state"], "revoked")
        self.assertEqual(second["service_tokens_revoked"], 0)

    def test_inventory_change_between_authorization_and_mutation_fails_closed(self):
        with self.assertRaises(Exception):
            self.store.revoke_workspace_credentials_for_tenant(
                tenant_id=self.first["tenant_id"],
                expected_operational_roots=("wrong-root",),
                clerk_user_id="clerk-user-a")
        token = self.store.get_service_token(self.first["token"])
        self.assertIsNotNone(token["secret_hash"])


if __name__ == "__main__":
    unittest.main()
