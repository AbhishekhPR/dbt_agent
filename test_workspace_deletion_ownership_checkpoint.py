"""A deletion must not be blocked by the consequences of its own deletion.

Foundation 2 derives `ci_token_binding` ownership from a live chain:

    repositories -> api_service_tokens (scope 'ci')
                 -> tenant_repositories.ci_token_id
                 -> tenant_github_installations

The deletion sequence then deliberately destroys exactly that chain --
`revoke_workspace_credentials_for_tenant` runs
`UPDATE api_service_tokens SET secret_hash = NULL, revoked_at = ...` and
`UPDATE tenant_repositories SET ci_token_id = NULL`. The operational purge, two
phases later, re-derived ownership from the chain that is now gone and refused:

    ownership proven             -> operation created
    github / credential revocation -> CI token revoked, projection severed
    operational purge            -> re-derives ownership -> INCOMPLETE

In production that left one workspace at phase `artifacts_purged` with
`credentials_revoked_at` set, `operational_records_deleted = 0`, and its single
root reporting `repository_count 1 / matched_count 0` -- permanently undeletable.

`operational_ownership_verified_at` records the proof once, before anything is
revoked. A contradiction found later still fails closed.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class OwnershipCheckpointTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)

    def tearDown(self):
        self.store.close()

    # -- fixtures ---------------------------------------------------------

    def _proven_root(self, suffix, installation_id, github_repository_id,
                     organization_id, repository_id, token_id, tenant_id=None):
        if tenant_id is None:
            tenant_id = self.store.upsert_tenant_for_clerk_organization(
                f"clerk-{suffix}", organization_name=f"Workspace {suffix}"
            )["tenant_id"]
        self.store.record_github_installation(
            installation_id, github_app_id=1,
            github_account_id=installation_id + 9000,
            github_account_login=f"account-{suffix}",
            github_account_type="Organization",
            repository_selection="selected", status="active")
        self.store.bind_github_installation_to_tenant(
            installation_id, tenant_id=tenant_id,
            bound_by_clerk_user_id=f"user-{suffix}",
            verified_github_user_id=installation_id + 8000,
            bound_via_state_id=None)
        self.store.select_tenant_repository(
            github_repository_id, tenant_id=tenant_id,
            github_installation_id=installation_id,
            owner_login=f"owner-{suffix}", name=f"repository-{suffix}")
        self.store.ensure_repository(organization_id, repository_id)
        self.store.create_service_token(
            token_id, "secret-hash", organization_id, repository_id,
            environment=None, description="ci", scope="ci")
        self.store.record_tenant_repository_ci_token(
            github_repository_id, tenant_id=tenant_id, ci_token_id=token_id,
            delivery="display_once", issued_at=datetime.now(timezone.utc))
        # Bridge the root exactly as production did: through the derived proof,
        # never by hand.
        self.store.reconcile_tenant_operational_roots(apply=True)
        return tenant_id

    def _claim_by_another_tenant(self, suffix, installation_id,
                                 github_repository_id, ci_token_id):
        """Point a second workspace's repository projection at the same token."""
        other = self.store.upsert_tenant_for_clerk_organization(
            f"clerk-{suffix}", organization_name=f"Workspace {suffix}"
        )["tenant_id"]
        self.store.record_github_installation(
            installation_id, github_app_id=1,
            github_account_id=installation_id + 9000,
            github_account_login=f"account-{suffix}",
            github_account_type="Organization",
            repository_selection="selected", status="active")
        self.store.bind_github_installation_to_tenant(
            installation_id, tenant_id=other,
            bound_by_clerk_user_id=f"user-{suffix}",
            verified_github_user_id=installation_id + 8000,
            bound_via_state_id=None)
        self.store.select_tenant_repository(
            github_repository_id, tenant_id=other,
            github_installation_id=installation_id,
            owner_login=f"owner-{suffix}", name=f"repository-{suffix}")
        self.store.connection.execute(
            "UPDATE tenant_repositories SET ci_token_id = %s "
            "WHERE github_repository_id = %s",
            (ci_token_id, github_repository_id))
        self.store.connection.commit()
        return other

    def _engine(self, tenant_id):
        from agent.api.clerk_management import ClerkResourceAbsent
        from agent.api.workspace_membership import WorkspaceAuthorizationContext
        from agent.github_app.client import GitHubNotFoundError
        from agent.workspace_credential_revocation import (
            revoke_workspace_credentials,
        )
        from agent.workspace_deletion_lifecycle import WorkspaceDeletionEngine

        class Authorizer:
            def require_owner(self, principal):
                return WorkspaceAuthorizationContext(
                    tenant_id=tenant_id, clerk_user_id="user_owner",
                    role="owner", ownership_status="authoritative",
                    active_owner_count=1, sync_generation="sync_1")

        class GitHub:
            def delete_installation(self, installation_id, app_jwt):
                return {}

            def get_installation(self, installation_id, app_jwt):
                raise GitHubNotFoundError("absent", status_code=404)

        class Clerk:
            def __init__(self):
                self.deleted = []

            def delete_organization(self, organization_id):
                self.deleted.append(organization_id)
                return {}

            def get_organization(self, organization_id):
                if organization_id in self.deleted:
                    raise ClerkResourceAbsent("absent")
                return {"id": organization_id}

        return WorkspaceDeletionEngine(
            authorizer=Authorizer(), store=self.store, polar_client=object(),
            github_client=GitHub(), github_app_jwt=lambda: "jwt",
            clerk_client=Clerk(),
            billing_revoker=lambda **kwargs: {"state": "verified_safe"},
            # The REAL revoker, so the proof material is genuinely destroyed.
            credential_revoker=revoke_workspace_credentials,
            repository_storage=None)

    def _principal(self):
        from agent.api.clerk_identity import ClerkPrincipal

        return ClerkPrincipal(
            clerk_user_id="user_owner", clerk_organization_id="clerk-org",
            tenant_id=None, factor_verification_age=(0, -1),
            clerk_token_issued_at=datetime.now(timezone.utc),
            is_impersonated=False)

    def _run(self, engine, operation_id, limit=10):
        from agent.workspace_deletion_lifecycle import LifecycleBlocked

        for _ in range(limit):
            try:
                operation = engine.advance(self._principal(), operation_id)
            except LifecycleBlocked as blocked:
                return blocked.category, None
            if operation.get("state") == "completed":
                return None, operation
        return None, None

    def _ownership_status(self, tenant_id):
        return self.store.tenant_operational_inventory(
            tenant_id)["ownership_status"]

    # -- the production sequence -------------------------------------------

    def test_the_deletion_survives_revoking_its_own_ownership_proof(self):
        tenant_id = self._proven_root(
            "prod", 701, 7001, "Abhishekh-col", "relium-saas-demo", "token-ci")
        tenant = self.store.tenant_by_id(tenant_id)
        engine = self._engine(tenant_id)

        # 1 + 2. One CI-proven legacy root; the request validates ownership.
        self.assertEqual(self._ownership_status(tenant_id), "complete")
        operation = engine.request(
            self._principal(), confirmation=tenant["organization_name"])
        self.assertIsNotNone(
            self.store.workspace_lifecycle_operation(
                operation["operation_id"])["operational_ownership_verified_at"])

        # 3 -> 6. Revocation destroys the proof material, and the purge still
        #         runs to completion on the checkpoint recorded above.
        category, completed = self._run(engine, operation["operation_id"])

        self.assertIsNone(category, f"deletion blocked by {category}")
        self.assertEqual(completed["state"], "completed")
        self.assertIsNotNone(completed.get("receipt_id"))
        self.assertGreater(int(completed["operational_records_deleted"]), 0)

    def test_the_proof_material_really_is_destroyed_on_the_way(self):
        """Guards the test above from silently ceasing to reproduce the bug."""
        tenant_id = self._proven_root(
            "destroy", 702, 7002, "DestroyRoot", "only", "token-destroy")
        engine = self._engine(tenant_id)
        tenant = self.store.tenant_by_id(tenant_id)
        operation = engine.request(
            self._principal(), confirmation=tenant["organization_name"])

        # Advance only as far as credential revocation.
        for _ in range(2):
            engine.advance(self._principal(), operation["operation_id"])

        severed = self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_repositories "
            "WHERE tenant_id = %s AND ci_token_id IS NOT NULL",
            (tenant_id,)).fetchone()["n"]
        self.assertEqual(int(severed), 0, "the CI projection must be severed")
        self.assertEqual(self._ownership_status(tenant_id), "incomplete")

    def test_a_retry_after_credentials_are_revoked_still_completes(self):
        tenant_id = self._proven_root(
            "retry", 703, 7003, "RetryRoot", "only", "token-retry")
        tenant = self.store.tenant_by_id(tenant_id)
        engine = self._engine(tenant_id)
        operation = engine.request(
            self._principal(), confirmation=tenant["organization_name"])
        operation_id = operation["operation_id"]

        for _ in range(2):
            engine.advance(self._principal(), operation_id)
        self.assertEqual(self._ownership_status(tenant_id), "incomplete")

        # Requesting again returns the SAME operation, and advancing it from
        # here -- with the credentials already gone -- must still finish.
        again = engine.request(
            self._principal(), confirmation=tenant["organization_name"])
        self.assertEqual(again["operation_id"], operation_id)

        category, completed = self._run(engine, operation_id)

        self.assertIsNone(category, f"retry blocked by {category}")
        self.assertEqual(completed["state"], "completed")

    # -- still fails closed -------------------------------------------------

    def test_ownership_incomplete_before_revocation_still_blocks(self):
        from agent.workspace_deletion_lifecycle import LifecycleBlocked

        tenant_id = self._proven_root(
            "blocked", 704, 7004, "BlockedRoot", "proven", "token-blocked")
        # A second repository under the same root with no proof at all.
        self.store.ensure_repository("BlockedRoot", "unproven")
        self.store.reconcile_tenant_operational_roots(apply=True)
        tenant = self.store.tenant_by_id(tenant_id)

        self.assertNotEqual(self._ownership_status(tenant_id), "complete")
        with self.assertRaises(LifecycleBlocked) as raised:
            self._engine(tenant_id).request(
                self._principal(), confirmation=tenant["organization_name"])

        self.assertEqual(raised.exception.category,
                         "operational_ownership_incomplete")
        self.assertIsNone(self.store.connection.execute(
            "SELECT operation_id FROM workspace_lifecycle_operations "
            "WHERE tenant_id = %s", (tenant_id,)).fetchone())

    def test_contradictory_ownership_still_blocks_the_request(self):
        from agent.workspace_deletion_lifecycle import LifecycleBlocked

        first = self._proven_root(
            "contra-a", 705, 7005, "ContraRoot", "first", "token-contra-a")
        self._proven_root(
            "contra-b", 706, 7006, "ContraRoot", "second", "token-contra-b")
        tenant = self.store.tenant_by_id(first)

        self.assertEqual(self._ownership_status(first), "inconsistent")
        with self.assertRaises(LifecycleBlocked) as raised:
            self._engine(first).request(
                self._principal(), confirmation=tenant["organization_name"])

        self.assertEqual(raised.exception.category,
                         "operational_ownership_inconsistent")

    def test_a_checkpoint_never_excuses_a_contradiction_at_purge(self):
        """The checkpoint answers for absence of proof, never for a conflict."""
        tenant_id = self._proven_root(
            "purge-contra", 707, 7007, "PurgeContraRoot", "first",
            "token-purge-a")
        engine = self._engine(tenant_id)
        tenant = self.store.tenant_by_id(tenant_id)
        operation = engine.request(
            self._principal(), confirmation=tenant["organization_name"])
        operation_id = operation["operation_id"]
        for _ in range(3):
            engine.advance(self._principal(), operation_id)

        # Another workspace now proves a claim on the same root. It cannot be
        # built through `_proven_root` here -- minting a CI token under a
        # workspace that is already freezing is correctly refused -- so the
        # projection is written directly, which is what a genuine cross-tenant
        # claim would look like in the data.
        self._claim_by_another_tenant(
            "purge-contra-b", 708, 7008, "token-purge-a")
        self.assertEqual(self._ownership_status(tenant_id), "inconsistent")
        self.assertIsNotNone(
            self.store.workspace_lifecycle_operation(
                operation_id)["operational_ownership_verified_at"])

        with self.assertRaises(ValueError) as raised:
            self.store.purge_workspace_operational_data(
                tenant_id=tenant_id, operation_id=operation_id)

        self.assertEqual(str(raised.exception),
                         "operational_ownership_inconsistent")

    def test_an_operation_without_a_checkpoint_must_still_prove_ownership(self):
        """The column grants nothing on its own."""
        tenant_id = self._proven_root(
            "nockpt", 709, 7009, "NoCheckpointRoot", "only", "token-nockpt")
        engine = self._engine(tenant_id)
        tenant = self.store.tenant_by_id(tenant_id)
        operation = engine.request(
            self._principal(), confirmation=tenant["organization_name"])
        operation_id = operation["operation_id"]
        for _ in range(3):
            engine.advance(self._principal(), operation_id)

        # Erase the checkpoint: the proof material is gone, so with no record
        # that ownership was ever verified the purge must refuse.
        self.store.connection.execute(
            "UPDATE workspace_lifecycle_operations "
            "SET operational_ownership_verified_at = NULL "
            "WHERE operation_id = %s", (operation_id,))
        self.store.connection.commit()

        with self.assertRaises(ValueError) as raised:
            self.store.purge_workspace_operational_data(
                tenant_id=tenant_id, operation_id=operation_id)

        self.assertEqual(str(raised.exception),
                         "operational_ownership_incomplete")


if __name__ == "__main__":
    unittest.main()
