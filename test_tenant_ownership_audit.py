from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from click.testing import CliRunner


DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class TenantOwnershipAuditTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        self.store = PostgresLifecycleStore(DSN)

    def tearDown(self):
        self.store.close()

    def _tenant_repository(self, suffix, installation_id, github_repository_id,
                           organization_id, repository_id, token_id=None):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            f"clerk-{suffix}", organization_name=f"Tenant {suffix}")
        tenant_id = tenant["tenant_id"]
        self.store.record_github_installation(
            installation_id,
            github_app_id=1,
            github_account_id=installation_id + 9000,
            github_account_login=f"account-{suffix}",
            github_account_type="Organization",
            repository_selection="selected",
            status="active",
        )
        self.store.bind_github_installation_to_tenant(
            installation_id,
            tenant_id=tenant_id,
            bound_by_clerk_user_id=f"user-{suffix}",
            verified_github_user_id=installation_id + 8000,
            bound_via_state_id=None,
        )
        self.store.select_tenant_repository(
            github_repository_id,
            tenant_id=tenant_id,
            github_installation_id=installation_id,
            owner_login=f"display-{suffix}",
            name=f"display-repository-{suffix}",
        )
        self.store.ensure_repository(organization_id, repository_id)
        if token_id:
            self.store.create_service_token(
                token_id, "secret-hash-must-not-appear", organization_id,
                repository_id, environment=None, description="sensitive label",
                scope="ci")
            self.store.record_tenant_repository_ci_token(
                github_repository_id,
                tenant_id=tenant_id,
                ci_token_id=token_id,
                delivery="display_once",
                issued_at=datetime.now(timezone.utc),
            )
        return tenant_id

    def test_audit_classifies_mapped_unmapped_partial_and_mixed_roots(self):
        mapped = self._tenant_repository(
            "mapped", 301, 3001, "mapped-root", "one", "token-mapped")
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_ci_token_id, verified_at) "
            "VALUES ('mapped-root', %s, 'ci_token_binding', 3001, "
            "        'token-mapped', now())", (mapped,))

        self.store.ensure_repository("unmapped-root", "legacy")

        self._tenant_repository(
            "partial", 302, 3002, "partial-root", "matched", "token-partial")
        self.store.ensure_repository("partial-root", "unmatched")

        self._tenant_repository(
            "mixed-a", 303, 3003, "mixed-root", "first", "token-mixed-a")
        self._tenant_repository(
            "mixed-b", 304, 3004, "mixed-root", "second", "token-mixed-b")

        report = self.store.tenant_operational_ownership_audit()

        self.assertEqual(
            [row["organization_id"] for row in report["mapped_roots"]],
            ["mapped-root"],
        )
        self.assertIn("unmapped-root", report["unmapped_roots"])
        reasons = {
            row["organization_id"]: row["reason"]
            for row in report["ambiguous_roots"]
        }
        self.assertEqual(reasons["partial-root"], "partial_mapping")
        self.assertEqual(reasons["mixed-root"], "multiple_tenants")

    def test_audit_reports_cross_tenant_installation_and_logical_orphans(self):
        first = self._tenant_repository(
            "first", 305, 3005, "root", "repo", "token-first")
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_ci_token_id, verified_at) "
            "VALUES ('root', %s, 'ci_token_binding', 3005, "
            "        'token-first', now())", (first,))
        second = self.store.upsert_tenant_for_clerk_organization(
            "clerk-second", organization_name="Second")["tenant_id"]
        # Existing rows can violate a NOT VALID constraint. Simulate production
        # history by recreating that constraint around an older inconsistent row.
        self.store.connection.execute(
            "ALTER TABLE tenant_repositories DROP CONSTRAINT "
            "tenant_repositories_installation_tenant_fk")
        self.store.connection.execute(
            "UPDATE tenant_repositories SET tenant_id=%s "
            "WHERE github_repository_id=3005", (second,))
        self.store.connection.execute(
            "ALTER TABLE tenant_repositories ADD CONSTRAINT "
            "tenant_repositories_installation_tenant_fk "
            "FOREIGN KEY (github_installation_id, tenant_id) "
            "REFERENCES tenant_github_installations "
            "(github_installation_id, tenant_id) ON DELETE CASCADE NOT VALID")
        self.store.connection.execute(
            "INSERT INTO audit_events "
            "(organization_id, repository_id, actor, event_type) "
            "VALUES ('missing-root', 'missing-repo', 'system', 'test')")

        report = self.store.tenant_operational_ownership_audit()

        kinds = {row["kind"] for row in report["cross_tenant_inconsistencies"]}
        self.assertIn("repository_installation_tenant_mismatch", kinds)
        self.assertIn("mapping_source_repository_tenant_mismatch", kinds)
        self.assertIn("mapping_source_ci_token_mismatch", kinds)
        repository_mismatch = next(
            row for row in report["cross_tenant_inconsistencies"]
            if row["kind"] == "repository_installation_tenant_mismatch")
        self.assertEqual(repository_mismatch["examples"], [{
            "github_repository_id": 3005,
            "github_installation_id": 305,
            "tenant_id": second,
        }])
        self.assertEqual(report["orphan_counts"]["audit_events"], 1)
        serialized = json.dumps(report, default=str)
        self.assertNotIn("secret-hash-must-not-appear", serialized)
        self.assertNotIn("sensitive label", serialized)
        self.assertNotEqual(first, second)

    def test_reconciliation_is_dry_run_by_default_and_guarded_on_apply(self):
        tenant_id = self._tenant_repository(
            "eligible", 306, 3006, "eligible-root", "repo", "token-eligible")

        preview = self.store.reconcile_tenant_operational_roots(apply=False)
        self.assertEqual(preview["eligible_count"], 1)
        self.assertEqual(preview["applied_count"], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots"
        ).fetchone()["n"], 0)

        applied = self.store.reconcile_tenant_operational_roots(apply=True)
        self.assertEqual(applied["eligible_count"], 1)
        self.assertEqual(applied["applied_count"], 1)
        row = self.store.connection.execute(
            "SELECT tenant_id FROM tenant_operational_roots "
            "WHERE organization_id='eligible-root'").fetchone()
        self.assertEqual(row["tenant_id"], tenant_id)

        replay = self.store.reconcile_tenant_operational_roots(apply=True)
        self.assertEqual(replay["applied_count"], 0)

    def test_reconciliation_keeps_multi_repository_provenance_correlated(self):
        tenant_id = self._tenant_repository(
            "pair-a", 309, 3009, "pair-root", "a-repository", "z-token")
        self.store.record_github_installation(
            310, github_app_id=1, github_account_id=9310,
            github_account_login="account-pair-b",
            github_account_type="Organization",
            repository_selection="selected", status="active")
        self.store.bind_github_installation_to_tenant(
            310, tenant_id=tenant_id, bound_by_clerk_user_id="user-pair-b",
            verified_github_user_id=8310, bound_via_state_id=None)
        self.store.select_tenant_repository(
            3010, tenant_id=tenant_id, github_installation_id=310,
            owner_login="display-pair-b", name="display-pair-b")
        self.store.ensure_repository("pair-root", "z-repository")
        self.store.create_service_token(
            "a-token", "a" * 64, "pair-root", "z-repository",
            environment=None, description="test", scope="ci")
        self.store.record_tenant_repository_ci_token(
            3010, tenant_id=tenant_id, ci_token_id="a-token",
            delivery="display_once", issued_at=datetime.now(timezone.utc))

        self.store.reconcile_tenant_operational_roots(apply=True)

        row = self.store.connection.execute(
            "SELECT source_github_repository_id, source_ci_token_id "
            "FROM tenant_operational_roots WHERE organization_id='pair-root'"
        ).fetchone()
        self.assertEqual(dict(row), {
            "source_github_repository_id": 3009,
            "source_ci_token_id": "z-token",
        })

    def test_ci_token_rotation_preserves_historical_mapping_provenance(self):
        tenant_id = self._tenant_repository(
            "rotation", 311, 3011, "rotation-root", "repo", "old-token")
        self.store.reconcile_tenant_operational_roots(apply=True)
        self.store.create_service_token(
            "new-token", "n" * 64, "rotation-root", "repo",
            environment=None, description="replacement", scope="ci")

        self.store.record_tenant_repository_ci_token_and_bind_root(
            3011, tenant_id=tenant_id, ci_token_id="new-token",
            delivery="display_once", issued_at=datetime.now(timezone.utc),
            verified_at=datetime.now(timezone.utc))
        self.store.revoke_service_token("old-token")

        mapping = self.store.connection.execute(
            "SELECT source_ci_token_id FROM tenant_operational_roots "
            "WHERE organization_id='rotation-root'").fetchone()
        self.assertEqual(mapping["source_ci_token_id"], "old-token")
        report = self.store.tenant_operational_ownership_audit()
        kinds = {row["kind"] for row in report["cross_tenant_inconsistencies"]}
        self.assertNotIn("mapping_source_ci_token_mismatch", kinds)

    def test_mapped_root_conflict_examples_are_bounded(self):
        owner = self.store.upsert_tenant_for_clerk_organization(
            "clerk-owner", organization_name="Owner")["tenant_id"]
        candidate = self._tenant_repository(
            "candidate", 312, 3012, "conflict-0", "repo-0", "token-0")
        self.store.connection.execute(
            "INSERT INTO organizations (organization_id) "
            "SELECT 'conflict-' || n FROM generate_series(1, 100) n")
        self.store.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "SELECT 'conflict-' || n, 'repo-' || n "
            "FROM generate_series(1, 100) n")
        self.store.connection.execute(
            "INSERT INTO api_service_tokens "
            "(token_id, secret_hash, organization_id, repository_id, scope) "
            "SELECT 'token-' || n, repeat('a', 64), 'conflict-' || n, "
            "       'repo-' || n, 'ci' FROM generate_series(1, 100) n")
        self.store.connection.execute(
            "INSERT INTO tenant_repositories "
            "(github_repository_id, tenant_id, github_installation_id, "
            " owner_login, name, ci_token_id) "
            "SELECT 3012 + n, %s, 312, 'display', 'repo-' || n, 'token-' || n "
            "FROM generate_series(1, 100) n", (candidate,))
        self.store.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_ci_token_id, verified_at) "
            "SELECT 'conflict-' || n, %s, 'ci_token_binding', 3012 + n, "
            "       'token-' || n, now() FROM generate_series(0, 100) n",
            (owner,))

        report = self.store.tenant_operational_ownership_audit()
        conflicts = [
            row for row in report["cross_tenant_inconsistencies"]
            if row["kind"] == "mapped_root_provenance_conflict"]

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["count"], 101)
        self.assertEqual(len(conflicts[0]["examples"]), 100)
        self.assertTrue(conflicts[0]["examples_truncated"])

    def test_cli_defaults_to_read_only_json_without_secret_material(self):
        self._tenant_repository(
            "cli", 307, 3007, "cli-root", "repo", "token-cli")
        from agent.cli import cli

        result = CliRunner().invoke(
            cli,
            ["tenant-ownership-audit", "--json"],
            env={"RELIUM_DATABASE_URL": DSN},
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["reconciliation"]["applied_count"], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots"
        ).fetchone()["n"], 0)
        self.assertNotIn("secret-hash-must-not-appear", result.output)
        self.assertNotIn("sensitive label", result.output)

    def test_cli_reports_filesystem_ownership_without_reading_contents(self):
        self._tenant_repository(
            "files", 308, 3008, "files-root", "repo", "token-files")
        from agent.cli import cli

        with tempfile.TemporaryDirectory() as directory:
            owned = os.path.join(directory, "3008")
            unmapped = os.path.join(directory, "9999")
            os.makedirs(owned)
            os.makedirs(unmapped)
            with open(os.path.join(owned, "manifest.json"), "w") as handle:
                handle.write("filesystem-secret-must-not-appear")

            result = CliRunner().invoke(
                cli,
                ["tenant-ownership-audit", "--json", "--storage-root", directory],
                env={"RELIUM_DATABASE_URL": DSN},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            payload["filesystem"]["mapped_repository_directories"], 1)
        self.assertEqual(
            payload["filesystem"]["unmapped_repository_directories"], 1)
        self.assertEqual(payload["filesystem"]["mapped_files"], 1)
        self.assertNotIn("filesystem-secret-must-not-appear", result.output)


if __name__ == "__main__":
    unittest.main()
