from __future__ import annotations

import hashlib
import os
import re
import threading
import unittest
from pathlib import Path

from click.testing import CliRunner


MIGRATIONS = Path("agent/migrations/postgres")
MIGRATION = MIGRATIONS / "0021_tenant_operational_roots.sql"
BACKFILL = MIGRATIONS / "0022_tenant_operational_root_backfill.sql"
DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")


class TenantOperationalOwnershipMigrationContractTests(unittest.TestCase):
    def test_migration_runner_fails_closed_without_autocommit(self):
        from agent.postgres_migrate import apply_migrations

        class NonAutocommitConnection:
            autocommit = False

            def execute(self, *_args, **_kwargs):
                self.fail("migration runner must reject before executing SQL")

        with self.assertRaisesRegex(RuntimeError, "autocommit connection"):
            apply_migrations(NonAutocommitConnection())

    def test_migration_runner_serializes_startup_before_reading_versions(self):
        source = Path("agent/postgres_migrate.py").read_text(encoding="utf-8")

        self.assertIn("pg_advisory_lock", source)
        self.assertIn("pg_advisory_unlock", source)
        self.assertLess(
            source.index("pg_advisory_lock"),
            source.index("CREATE TABLE IF NOT EXISTS schema_migrations"),
        )

    def test_inventory_classifies_every_postgres_table(self):
        from agent.tenant_operational_ownership import (
            EXCLUDED_SHARED_TABLES,
            LEGACY_OPERATIONAL_TABLES,
            TENANT_OWNED_TABLES,
        )

        discovered = {"schema_migrations"}
        for path in MIGRATIONS.glob("*.sql"):
            discovered.update(re.findall(
                r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_]+)",
                path.read_text(encoding="utf-8"), flags=re.IGNORECASE))
            discovered.update(re.findall(
                r"CREATE OR REPLACE VIEW\s+([a-z_]+)",
                path.read_text(encoding="utf-8"), flags=re.IGNORECASE))
        classified = (set(TENANT_OWNED_TABLES)
                      | set(LEGACY_OPERATIONAL_TABLES)
                      | set(EXCLUDED_SHARED_TABLES))

        self.assertEqual(discovered, classified)

    def test_migration_defines_restrictive_single_tenant_root_bridge(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE tenant_operational_roots", sql)
        self.assertIn("organization_id TEXT PRIMARY KEY", sql)
        self.assertIn(
            "REFERENCES organizations (organization_id) ON DELETE RESTRICT", sql)
        self.assertIn("REFERENCES tenants (tenant_id) ON DELETE RESTRICT", sql)
        self.assertIn("ci_token_binding", sql)
        self.assertIn("verified_github_repository", sql)
        self.assertNotIn("owner_login", sql)
        self.assertNotIn("organization_name", sql)
        self.assertNotIn("tenants.role", sql)

    def test_migration_adds_not_valid_installation_tenant_constraints(self):
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn(
            "FOREIGN KEY (github_installation_id, tenant_id)", sql)
        self.assertGreaterEqual(sql.count("NOT VALID"), 2)

    def test_ddl_is_bounded_and_backfill_runs_after_locks_are_released(self):
        ddl = MIGRATION.read_text(encoding="utf-8")
        backfill = BACKFILL.read_text(encoding="utf-8")

        self.assertIn("lock_timeout = '5s'", ddl)
        self.assertIn("statement_timeout = '30s'", ddl)
        self.assertNotIn("WITH repository_candidates", ddl)
        self.assertIn("WITH repository_candidates", backfill)


@unittest.skipUnless(
    DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class TenantOperationalOwnershipPostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row

        self.connection = psycopg.connect(
            DSN, autocommit=True, row_factory=dict_row)
        self.connection.execute("DROP SCHEMA public CASCADE")
        self.connection.execute("CREATE SCHEMA public")
        self._apply_through(20)

    def tearDown(self):
        self.connection.close()

    def _apply_through(self, version):
        self.connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            current = int(path.name[:4])
            if current > version:
                continue
            sql = path.read_text(encoding="utf-8")
            self.connection.execute(sql)
            self.connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (current, hashlib.sha256(sql.encode("utf-8")).hexdigest()),
            )

    def _tenant(self, suffix):
        tenant_id = "ten_" + suffix * 32
        self.connection.execute(
            "INSERT INTO tenants (tenant_id, clerk_organization_id, organization_name) "
            "VALUES (%s, %s, %s)",
            (tenant_id, f"clerk_{suffix}", f"Tenant {suffix}"),
        )
        return tenant_id

    def _installation(self, tenant_id, installation_id):
        self.connection.execute(
            "INSERT INTO github_installations "
            "(github_installation_id, github_account_id, github_account_login, "
            " github_account_type) VALUES (%s, %s, %s, 'Organization')",
            (installation_id, installation_id + 1000, f"account-{installation_id}"),
        )
        self.connection.execute(
            "INSERT INTO tenant_github_installations "
            "(github_installation_id, tenant_id, bound_by_clerk_user_id, "
            " verified_github_user_id) VALUES (%s, %s, 'user', 99)",
            (installation_id, tenant_id),
        )

    def _linked_repository(self, tenant_id, installation_id, repository_id,
                           organization, repository, token_id):
        self.connection.execute(
            "INSERT INTO organizations (organization_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (organization,))
        self.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "VALUES (%s, %s)", (organization, repository))
        self.connection.execute(
            "INSERT INTO api_service_tokens "
            "(token_id, secret_hash, organization_id, repository_id, scope) "
            "VALUES (%s, %s, %s, %s, 'ci')",
            (token_id, "f" * 64, organization, repository),
        )
        self.connection.execute(
            "INSERT INTO tenant_repositories "
            "(github_repository_id, tenant_id, github_installation_id, owner_login, "
            " name, ci_token_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (repository_id, tenant_id, installation_id, organization, repository,
             token_id),
        )

    def _apply_latest(self):
        from agent.postgres_migrate import apply_migrations

        self.assertEqual(apply_migrations(self.connection), [21, 22, 23])

    def test_dry_run_audit_never_applies_a_pending_migration(self):
        from agent.cli import cli

        result = CliRunner().invoke(
            cli, ["tenant-ownership-audit", "--json"],
            env={"RELIUM_DATABASE_URL": DSN})

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("migration 0022", result.output)
        versions = [row["version"] for row in self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertEqual(versions[-1], 20)
        self.assertIsNone(self.connection.execute(
            "SELECT to_regclass('public.tenant_operational_roots') AS relation"
        ).fetchone()["relation"])

    def test_concurrent_startup_migration_runners_are_serialized(self):
        import psycopg
        from psycopg.rows import dict_row
        from agent.postgres_migrate import apply_migrations

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def migrate():
            connection = psycopg.connect(
                DSN, autocommit=True, row_factory=dict_row)
            try:
                barrier.wait(timeout=5)
                results.append(apply_migrations(connection))
            except Exception as exc:  # captured for the parent assertion
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=migrate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results, key=len), [[], [21, 22, 23]])

    def test_complete_exact_ci_chain_is_backfilled(self):
        tenant_id = self._tenant("a")
        self._installation(tenant_id, 101)
        self._linked_repository(
            tenant_id, 101, 1001, "legacy-root", "repo-one", "token-one")

        self._apply_latest()

        row = self.connection.execute(
            "SELECT tenant_id, mapping_basis, source_ci_token_id "
            "FROM tenant_operational_roots WHERE organization_id='legacy-root'"
        ).fetchone()
        self.assertEqual(dict(row), {
            "tenant_id": tenant_id,
            "mapping_basis": "ci_token_binding",
            "source_ci_token_id": "token-one",
        })

    def test_multi_repository_backfill_keeps_provenance_pair_correlated(self):
        tenant_id = self._tenant("a")
        self._installation(tenant_id, 107)
        self._linked_repository(
            tenant_id, 107, 1009, "pair-root", "a-repository", "z-token")
        self._linked_repository(
            tenant_id, 107, 1010, "pair-root", "z-repository", "a-token")

        self._apply_latest()

        row = self.connection.execute(
            "SELECT source_github_repository_id, source_ci_token_id "
            "FROM tenant_operational_roots WHERE organization_id='pair-root'"
        ).fetchone()
        self.assertEqual(dict(row), {
            "source_github_repository_id": 1009,
            "source_ci_token_id": "z-token",
        })

    def test_partially_matched_root_remains_unmapped(self):
        tenant_id = self._tenant("b")
        self._installation(tenant_id, 102)
        self._linked_repository(
            tenant_id, 102, 1002, "partial-root", "matched", "token-two")
        self.connection.execute(
            "INSERT INTO repositories (organization_id, repository_id) "
            "VALUES ('partial-root', 'unmatched')")

        self._apply_latest()

        count = self.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots "
            "WHERE organization_id='partial-root'").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_mixed_tenant_root_remains_unmapped(self):
        first = self._tenant("c")
        second = self._tenant("d")
        self._installation(first, 103)
        self._installation(second, 104)
        self._linked_repository(
            first, 103, 1003, "mixed-root", "first", "token-three")
        self._linked_repository(
            second, 104, 1004, "mixed-root", "second", "token-four")

        self._apply_latest()

        count = self.connection.execute(
            "SELECT count(*) AS n FROM tenant_operational_roots "
            "WHERE organization_id='mixed-root'").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_root_cannot_be_reassigned_and_parent_deletes_are_restricted(self):
        first = self._tenant("e")
        second = self._tenant("f")
        self.connection.execute(
            "INSERT INTO organizations (organization_id) VALUES ('owned-root')")
        self._apply_latest()
        self.connection.execute(
            "INSERT INTO tenant_operational_roots "
            "(organization_id, tenant_id, mapping_basis, "
            " source_github_repository_id, source_github_installation_id, verified_at) "
            "VALUES ('owned-root', %s, 'verified_github_repository', 5001, 501, now())",
            (first,),
        )

        with self.assertRaises(Exception):
            self.connection.execute(
                "INSERT INTO tenant_operational_roots "
                "(organization_id, tenant_id, mapping_basis, "
                " source_github_repository_id, source_github_installation_id, verified_at) "
                "VALUES ('owned-root', %s, 'verified_github_repository', 5002, 502, now())",
                (second,),
            )
        with self.assertRaises(Exception):
            self.connection.execute(
                "DELETE FROM tenants WHERE tenant_id=%s", (first,))
        with self.assertRaises(Exception):
            self.connection.execute(
                "DELETE FROM organizations WHERE organization_id='owned-root'")

    def test_not_valid_repository_constraint_preserves_history_but_guards_writes(self):
        first = self._tenant("a")
        second = self._tenant("b")
        self._installation(first, 105)
        self.connection.execute(
            "INSERT INTO tenant_repositories "
            "(github_repository_id, tenant_id, github_installation_id, "
            " owner_login, name) VALUES (1005, %s, 105, 'legacy', 'old')",
            (second,),
        )

        self._apply_latest()

        constraint = self.connection.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname='tenant_repositories_installation_tenant_fk'"
        ).fetchone()
        self.assertFalse(constraint["convalidated"])
        with self.assertRaises(Exception):
            self.connection.execute(
                "INSERT INTO tenant_repositories "
                "(github_repository_id, tenant_id, github_installation_id, "
                " owner_login, name) "
                "VALUES (1006, %s, 105, 'legacy', 'new')",
                (second,),
            )

    def test_not_valid_detection_constraint_preserves_history_but_guards_writes(self):
        first = self._tenant("c")
        second = self._tenant("d")
        self._installation(first, 106)
        self.connection.execute(
            "INSERT INTO tenant_repository_dbt_detection "
            "(tenant_id, github_repository_id, github_installation_id, "
            " owner_login, name, default_branch) "
            "VALUES (%s, 1007, 106, 'legacy', 'old', 'main')",
            (second,),
        )

        self._apply_latest()

        constraint = self.connection.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname="
            "'tenant_repository_detection_installation_tenant_fk'"
        ).fetchone()
        self.assertFalse(constraint["convalidated"])
        with self.assertRaises(Exception):
            self.connection.execute(
                "INSERT INTO tenant_repository_dbt_detection "
                "(tenant_id, github_repository_id, github_installation_id, "
                " owner_login, name, default_branch) "
                "VALUES (%s, 1008, 106, 'legacy', 'new', 'main')",
                (second,),
            )


if __name__ == "__main__":
    unittest.main()
