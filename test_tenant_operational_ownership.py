from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


class TenantInventoryAuthorizationTests(unittest.TestCase):
    def test_inventory_uses_authorizer_context_not_principal_tenant_claim(self):
        from agent.api.workspace_membership import WorkspaceAuthorizationContext
        from agent.tenant_operational_ownership import inventory_for_current_workspace

        authoritative = WorkspaceAuthorizationContext(
            tenant_id="ten_" + "a" * 32,
            clerk_user_id="user_real",
            role="owner",
            ownership_status="authoritative",
            active_owner_count=1,
            sync_generation="sync_current",
        )

        class Authorizer:
            def require_admin_or_owner(self, principal):
                self.principal = principal
                return authoritative

        class Store:
            def tenant_operational_inventory(self, tenant_id):
                self.tenant_id = tenant_id
                return {"tenant_id": tenant_id, "counts": {}}

        principal = SimpleNamespace(tenant_id="ten_" + "b" * 32)
        authorizer = Authorizer()
        store = Store()

        result = inventory_for_current_workspace(
            store, principal=principal, authorizer=authorizer)

        self.assertIs(authorizer.principal, principal)
        self.assertEqual(store.tenant_id, authoritative.tenant_id)
        self.assertEqual(result["tenant_id"], authoritative.tenant_id)

    def test_inventory_rejects_a_non_authorization_context(self):
        from agent.tenant_operational_ownership import (
            TenantOperationalOwnershipUnavailable,
            inventory_for_current_workspace,
        )

        class Authorizer:
            def require_admin_or_owner(self, principal):
                return {"tenant_id": "browser-controlled"}

        with self.assertRaises(TenantOperationalOwnershipUnavailable):
            inventory_for_current_workspace(
                object(), principal=object(), authorizer=Authorizer())


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class TenantOperationalOwnershipStoreTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.store.close()

    def _tenant(self, suffix, installation_id, repository_id,
                owner="legacy-owner", name="warehouse"):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            f"clerk-{suffix}", organization_name=f"Tenant {suffix}")
        tenant_id = tenant["tenant_id"]
        self.store.record_github_installation(
            installation_id,
            github_app_id=1,
            github_account_id=installation_id + 1000,
            github_account_login=owner,
            github_account_type="Organization",
            repository_selection="selected",
            status="active",
        )
        self.store.bind_github_installation_to_tenant(
            installation_id,
            tenant_id=tenant_id,
            bound_by_clerk_user_id="user-owner",
            verified_github_user_id=88,
            bound_via_state_id=None,
        )
        self.store.select_tenant_repository(
            repository_id,
            tenant_id=tenant_id,
            github_installation_id=installation_id,
            owner_login=owner,
            name=name,
        )
        return tenant_id

    def _ci_token(self, tenant_id, repository_id, token_id,
                  owner="legacy-owner", name="warehouse", scope="ci"):
        self.store.ensure_repository(owner, name)
        self.store.create_service_token(
            token_id, "e" * 64, owner, name,
            environment=None, description="test", scope=scope)
        return self.store.record_tenant_repository_ci_token_and_bind_root(
            repository_id,
            tenant_id=tenant_id,
            ci_token_id=token_id,
            delivery="display_once",
            issued_at=self.now,
            verified_at=self.now,
        )

    def test_exact_ci_binding_is_transactional_and_idempotent(self):
        tenant_id = self._tenant("one", 201, 2001)

        first = self._ci_token(tenant_id, 2001, "ci-one")
        second = self.store.record_tenant_repository_ci_token_and_bind_root(
            2001,
            tenant_id=tenant_id,
            ci_token_id="ci-one",
            delivery="display_once",
            issued_at=self.now,
            verified_at=self.now,
        )

        self.assertEqual(first["organization_id"], "legacy-owner")
        self.assertEqual(first["tenant_id"], tenant_id)
        self.assertEqual(first["mapping_basis"], "ci_token_binding")
        self.assertEqual(second["tenant_id"], tenant_id)
        count = self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots"
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_verified_numeric_repository_can_establish_only_a_new_root(self):
        tenant_id = self._tenant(
            "verified", 209, 2009, owner="new-owner", name="warehouse")

        mapping = self.store.establish_tenant_operational_root_from_github(
            tenant_id=tenant_id,
            github_repository_id=2009,
            github_installation_id=209,
            organization_id="new-owner",
            verified_at=self.now,
        )

        self.assertEqual(mapping["tenant_id"], tenant_id)
        self.assertEqual(mapping["mapping_basis"], "verified_github_repository")
        self.assertEqual(mapping["source_github_repository_id"], 2009)
        self.assertEqual(mapping["source_github_installation_id"], 209)

    def test_verified_repository_does_not_claim_an_existing_unmapped_root(self):
        tenant_id = self._tenant(
            "legacy", 210, 2010, owner="legacy-existing", name="warehouse")
        self.store.ensure_repository("legacy-existing", "historical")

        mapping = self.store.establish_tenant_operational_root_from_github(
            tenant_id=tenant_id,
            github_repository_id=2010,
            github_installation_id=210,
            organization_id="legacy-existing",
            verified_at=self.now,
        )

        self.assertIsNone(mapping)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots "
            "WHERE organization_id='legacy-existing'"
        ).fetchone()["n"], 0)

    def test_ci_rotation_accepts_a_previously_verified_same_tenant_root(self):
        tenant_id = self._tenant(
            "preowned", 211, 2011, owner="preowned-root", name="first")
        self.store.establish_tenant_operational_root_from_github(
            tenant_id=tenant_id,
            github_repository_id=2011,
            github_installation_id=211,
            organization_id="preowned-root",
            verified_at=self.now,
        )
        self.store.ensure_repository("preowned-root", "first")
        self.store.ensure_repository("preowned-root", "other-without-ci")
        self.store.create_service_token(
            "ci-preowned", "a" * 64, "preowned-root", "first",
            environment=None, description="test", scope="ci")

        mapping = self.store.record_tenant_repository_ci_token_and_bind_root(
            2011,
            tenant_id=tenant_id,
            ci_token_id="ci-preowned",
            delivery="display_once",
            issued_at=self.now,
            verified_at=self.now,
        )

        self.assertEqual(mapping["tenant_id"], tenant_id)
        self.assertEqual(mapping["mapping_basis"], "verified_github_repository")

    def test_billing_scope_resolves_only_through_authoritative_root(self):
        installation_id = 218
        repository_id = 2018
        tenant_id = self._tenant(
            "billing", installation_id, repository_id,
            owner="acme", name="warehouse-dbt")
        self.store.connection.execute(
            "INSERT INTO organizations (organization_id) VALUES ('acme')")
        self.store.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "VALUES ('acme', 'warehouse-dbt')")

        self.assertIsNone(self.store.tenant_for_operational_repository(
            "acme", "warehouse-dbt"))

        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_github_installation_id, "
            " verified_at) "
            "VALUES ('acme', %s, 'verified_github_repository', %s, %s, %s)",
            (tenant_id, repository_id, installation_id, self.now),
        )
        self.assertEqual(
            self.store.tenant_for_operational_repository(
                "acme", "warehouse-dbt"),
            tenant_id,
        )

    def test_wrong_scope_rolls_back_repository_token_update(self):
        tenant_id = self._tenant("two", 202, 2002)
        self.store.ensure_repository("legacy-owner", "warehouse")
        self.store.create_service_token(
            "collector-token", "d" * 64, "legacy-owner", "warehouse",
            environment="production", description="test", scope="collector")

        from agent.postgres_lifecycle_store import OperationalRootOwnershipConflict
        with self.assertRaises(OperationalRootOwnershipConflict):
            self.store.record_tenant_repository_ci_token_and_bind_root(
                2002,
                tenant_id=tenant_id,
                ci_token_id="collector-token",
                delivery="display_once",
                issued_at=self.now,
                verified_at=self.now,
            )

        repository = self.store.tenant_repository(tenant_id, 2002)
        self.assertIsNone(repository["ci_token_id"])
        count = self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots"
        ).fetchone()["n"]
        self.assertEqual(count, 0)

    def test_existing_root_cannot_be_claimed_from_only_one_of_its_repositories(self):
        tenant_id = self._tenant("three", 203, 2003)
        self.store.ensure_repository("legacy-owner", "other-repository")
        self.store.ensure_repository("legacy-owner", "warehouse")
        self.store.create_service_token(
            "ci-partial", "c" * 64, "legacy-owner", "warehouse",
            environment=None, description="test", scope="ci")

        from agent.postgres_lifecycle_store import OperationalRootOwnershipConflict
        with self.assertRaises(OperationalRootOwnershipConflict):
            self.store.record_tenant_repository_ci_token_and_bind_root(
                2003,
                tenant_id=tenant_id,
                ci_token_id="ci-partial",
                delivery="display_once",
                issued_at=self.now,
                verified_at=self.now,
            )

        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots"
        ).fetchone()["n"], 0)

    def test_second_tenant_cannot_claim_an_owned_root(self):
        first = self._tenant("four", 204, 2004)
        self._ci_token(first, 2004, "ci-first")
        second = self._tenant(
            "five", 205, 2005, owner="legacy-owner", name="second")
        self.store.ensure_repository("legacy-owner", "second")
        self.store.create_service_token(
            "ci-second", "b" * 64, "legacy-owner", "second",
            environment=None, description="test", scope="ci")

        from agent.postgres_lifecycle_store import OperationalRootOwnershipConflict
        with self.assertRaises(OperationalRootOwnershipConflict):
            self.store.record_tenant_repository_ci_token_and_bind_root(
                2005,
                tenant_id=second,
                ci_token_id="ci-second",
                delivery="display_once",
                issued_at=self.now,
                verified_at=self.now,
            )

        owner = self.store.connection.execute(
            "SELECT tenant_id FROM tenant_operational_roots "
            "WHERE organization_id='legacy-owner'").fetchone()["tenant_id"]
        self.assertEqual(owner, first)

    def test_inventory_counts_owned_rows_and_excludes_other_tenant(self):
        first = self._tenant("six", 206, 2006)
        self._ci_token(first, 2006, "ci-six")
        self.store.ensure_tenant("legacy-owner", "warehouse", "production")
        self.store.create_review(
            "legacy-owner", "warehouse", "production",
            review_id="review-owned", pull_number=1, commit_sha="a" * 40,
            decision="ALLOW", enforcement_mode="shadow", risk_score=0,
            evidence_coverage="COMPLETE", payload={})

        second = self._tenant(
            "seven", 207, 2007, owner="other-owner", name="other")
        self._ci_token(
            second, 2007, "ci-seven", owner="other-owner", name="other")
        self.store.ensure_tenant("other-owner", "other", "production")
        self.store.create_review(
            "other-owner", "other", "production",
            review_id="review-other", pull_number=2, commit_sha="b" * 40,
            decision="ALLOW", enforcement_mode="shadow", risk_score=0,
            evidence_coverage="COMPLETE", payload={})

        inventory = self.store.tenant_operational_inventory(first)

        self.assertEqual(inventory["operational_roots"], ["legacy-owner"])
        self.assertEqual(inventory["counts"]["reviews"], 1)
        self.assertEqual(inventory["counts"]["repositories"], 1)
        self.assertEqual(inventory["counts"]["tenant_repositories"], 1)
        self.assertEqual(
            inventory["counts"]["tenant_operational_roots"], 1)
        self.assertIn(
            "clerk_github_identities", inventory["excluded_shared_tables"])
        self.assertEqual(inventory["ownership_status"], "complete")
        self.assertEqual(inventory["unresolved_operational_roots"], [])
        self.assertNotIn("review-owned", str(inventory))
        self.assertNotIn("review-other", str(inventory))

        self.store.ensure_repository("legacy-owner", "unmatched-descendant")
        degraded = self.store.tenant_operational_inventory(first)
        self.assertEqual(degraded["ownership_status"], "incomplete")

    def test_inventory_distinguishes_empty_from_unresolved_ownership(self):
        empty = self._tenant("empty", 219, 2019)
        empty_inventory = self.store.tenant_operational_inventory(empty)
        self.assertEqual(empty_inventory["ownership_status"], "complete")
        self.assertEqual(empty_inventory["operational_roots"], [])

        tenant_id = self._tenant("partial-inventory", 220, 2020)
        self.store.ensure_repository("partial-inventory-root", "matched")
        self.store.ensure_repository("partial-inventory-root", "unmatched")
        self.store.create_service_token(
            "partial-inventory-token", "c" * 64,
            "partial-inventory-root", "matched", environment=None,
            description="test", scope="ci")
        self.store.record_tenant_repository_ci_token(
            2020, tenant_id=tenant_id,
            ci_token_id="partial-inventory-token", delivery="display_once",
            issued_at=self.now)

        inventory = self.store.tenant_operational_inventory(tenant_id)
        self.assertEqual(inventory["ownership_status"], "incomplete")
        self.assertEqual(
            inventory["unresolved_operational_roots"],
            ["partial-inventory-root"],
        )

    def test_filesystem_inventory_returns_counts_not_webhook_contents(self):
        from agent.tenant_operational_ownership import filesystem_inventory

        tenant_id = self._tenant("eight", 208, 2008)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "2008" / "jobs"
            jobs.mkdir(parents=True)
            (jobs / "delivery.json").write_text(
                '{"raw_body":"TOP-SECRET-WEBHOOK"}', encoding="utf-8")
            unowned = root / "9999" / "deliveries"
            unowned.mkdir(parents=True)
            (unowned / "unknown").write_text("complete", encoding="utf-8")

            result = filesystem_inventory(
                self.store, tenant_id=tenant_id, storage_root=root)

        self.assertEqual(result["owned_repository_directories"], 1)
        self.assertEqual(result["owned_files"], 1)
        self.assertEqual(result["unmapped_repository_directories"], 1)
        self.assertNotIn("TOP-SECRET-WEBHOOK", str(result))


if __name__ == "__main__":
    unittest.main()
