"""Migration and storage semantics for Clerk tenancy, on a real PostgreSQL.

Everything asserted here is a property of the database, not of the application:
constraint enforcement, upgrade from the previous schema version, and what
happens when several connections insert the same tenant at the same instant.
None of it can be demonstrated with a mock, and substituting SQLite would test
different semantics from the ones production runs on.

Skipped rather than failed when RELIUM_TEST_POSTGRES_DSN is unset, matching
test_postgres_lifecycle_store.py.
"""
from __future__ import annotations

import os
import threading
import unittest

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

#: The schema version that existed before Clerk tenancy.
PREVIOUS_LATEST = 13
NEW_LATEST = 14


def _reset_schema(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _apply_up_to(dsn, highest_version):
    """Bring a database to exactly one schema version, no further.

    Used to reproduce a deployment sitting on the previous release, so the
    upgrade path is exercised rather than only the from-empty path. Production
    never applies migrations from empty; it always upgrades.
    """
    import hashlib

    import psycopg

    from agent.postgres_migrate import _migration_files, _version_of

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in _migration_files():
            version = _version_of(path)
            if version > highest_version:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, hashlib.sha256(sql.encode("utf-8")).hexdigest()))


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ClerkTenantMigrationTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(DSN)

    def _store(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        return PostgresLifecycleStore(DSN)

    def test_migration_from_an_empty_database_creates_the_tenancy_tables(self):
        store = self._store()
        try:
            tables = {
                row["table_name"]
                for row in store.connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'").fetchall()
            }
            self.assertIn("tenants", tables)
            self.assertIn("tenant_onboarding_state", tables)
        finally:
            store.close()

    def test_upgrade_from_the_previous_latest_version(self):
        """The path production will actually take."""
        from agent.postgres_migrate import applied_versions

        _apply_up_to(DSN, PREVIOUS_LATEST)
        store = self._store()
        try:
            versions = applied_versions(store.connection)
            self.assertIn(NEW_LATEST, versions)
            self.assertEqual(max(versions), NEW_LATEST)
            store.connection.execute("SELECT 1 FROM tenants LIMIT 1")
        finally:
            store.close()

    def test_upgrade_preserves_data_written_before_it(self):
        """A migration that loses pilot data is not a migration."""
        _apply_up_to(DSN, PREVIOUS_LATEST)
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("INSERT INTO organizations (organization_id) VALUES ('acme')")
            conn.execute("INSERT INTO repositories (organization_id, repository_id) "
                         "VALUES ('acme', 'analytics')")

        store = self._store()
        try:
            row = store.connection.execute(
                "SELECT organization_id, repository_id FROM repositories").fetchone()
            self.assertEqual((row["organization_id"], row["repository_id"]),
                             ("acme", "analytics"))
        finally:
            store.close()

    def test_applying_migrations_twice_is_a_no_op(self):
        from agent.postgres_migrate import applied_versions, apply_migrations

        store = self._store()
        try:
            before = applied_versions(store.connection)
            self.assertEqual(apply_migrations(store.connection), [])
            self.assertEqual(applied_versions(store.connection), before)
        finally:
            store.close()

    def test_a_second_store_against_the_same_database_is_safe(self):
        """Two application instances start against one database, as on Railway."""
        first = self._store()
        second = self._store()
        try:
            from agent.postgres_migrate import applied_versions

            self.assertEqual(applied_versions(first.connection),
                             applied_versions(second.connection))
        finally:
            first.close()
            second.close()


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ClerkTenantConstraintTests(unittest.TestCase):
    """The constraints the application relies on, asserted directly."""

    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        self.store.connection.execute("DELETE FROM tenants")

    def test_clerk_organization_id_is_unique(self):
        """The constraint that makes workspace creation idempotent."""
        self.store.connection.execute(
            "INSERT INTO tenants (tenant_id, clerk_organization_id, organization_name) "
            "VALUES (%s, 'org_2acme', 'Acme')", (f"ten_{'a' * 32}",))
        with self.assertRaises(Exception) as caught:
            self.store.connection.execute(
                "INSERT INTO tenants (tenant_id, clerk_organization_id, organization_name) "
                "VALUES (%s, 'org_2acme', 'Acme Again')", (f"ten_{'b' * 32}",))
        self.assertIn("UniqueViolation", type(caught.exception).__name__)

    def test_a_malformed_tenant_id_is_rejected(self):
        for bad in ("acme", "ten_", "org_2acme", "ten_XYZ", f"ten_{'a' * 31}",
                    f"ten_{'a' * 33}", f"TEN_{'a' * 32}"):
            with self.assertRaises(Exception, msg=bad):
                self.store.connection.execute(
                    "INSERT INTO tenants (tenant_id, clerk_organization_id, "
                    "organization_name) VALUES (%s, %s, 'X')", (bad, f"org-{bad}"))

    def test_a_blank_organization_name_is_rejected_by_the_database(self):
        """Defence in depth: the API validates, and so does the schema."""
        for blank in ("", "   ", "\t"):
            with self.assertRaises(Exception, msg=repr(blank)):
                self.store.connection.execute(
                    "INSERT INTO tenants (tenant_id, clerk_organization_id, "
                    "organization_name) VALUES (%s, %s, %s)",
                    (f"ten_{'c' * 32}", f"org-{len(blank)}", blank))

    def test_an_unknown_onboarding_step_is_rejected(self):
        """An enum-like CHECK, not free text: an unknown step would reach the
        router and render as a broken screen."""
        tenant_id = f"ten_{'d' * 32}"
        self.store.connection.execute(
            "INSERT INTO tenants (tenant_id, clerk_organization_id, organization_name) "
            "VALUES (%s, 'org_2steps', 'Steps')", (tenant_id,))
        self.store.connection.execute(
            "INSERT INTO tenant_onboarding_state (tenant_id) VALUES (%s)", (tenant_id,))
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE tenant_onboarding_state SET current_step='wat' "
                "WHERE tenant_id=%s", (tenant_id,))

    def test_completion_cannot_disagree_with_the_current_step(self):
        """Marked complete while pointing at an unfinished step would give the
        router two contradictory answers."""
        tenant_id = f"ten_{'e' * 32}"
        self.store.connection.execute(
            "INSERT INTO tenants (tenant_id, clerk_organization_id, organization_name) "
            "VALUES (%s, 'org_2complete', 'Complete')", (tenant_id,))
        self.store.connection.execute(
            "INSERT INTO tenant_onboarding_state (tenant_id) VALUES (%s)", (tenant_id,))
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "UPDATE tenant_onboarding_state SET completed_at=now() "
                "WHERE tenant_id=%s", (tenant_id,))

    def test_onboarding_state_cannot_exist_without_a_tenant(self):
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO tenant_onboarding_state (tenant_id) VALUES (%s)",
                (f"ten_{'f' * 32}",))

    def test_deleting_a_tenant_removes_its_onboarding_state(self):
        tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_2cascade", organization_name="Cascade")
        self.store.connection.execute("DELETE FROM tenants WHERE tenant_id=%s",
                                      (tenant["tenant_id"],))
        self.assertIsNone(self.store.onboarding_state_for_tenant(tenant["tenant_id"]))


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class ClerkTenantStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        self.store.connection.execute("DELETE FROM tenants")

    def test_creation_also_creates_the_onboarding_row(self):
        """A tenant must never exist without state to read."""
        tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        state = self.store.onboarding_state_for_tenant(tenant["tenant_id"])
        self.assertIsNotNone(state)
        self.assertEqual(state["current_step"], "github")
        self.assertIsNone(state["completed_at"])

    def test_upsert_is_idempotent(self):
        first = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        second = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        self.assertEqual(first["tenant_id"], second["tenant_id"])

    def test_upsert_does_not_rewind_onboarding_progress(self):
        """Re-submitting the workspace step must not undo later progress."""
        tenant = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        self.store.connection.execute(
            "UPDATE tenant_onboarding_state SET current_step='dbt' WHERE tenant_id=%s",
            (tenant["tenant_id"],))
        again = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme Renamed")
        self.assertEqual(again["current_step"], "dbt")

    def test_upsert_updates_the_timestamp(self):
        first = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        second = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme Analytics")
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_lookup_is_by_clerk_organization_and_finds_nothing_for_another(self):
        self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")
        self.assertIsNone(self.store.tenant_by_clerk_organization("org_2globex"))
        self.assertIsNone(self.store.tenant_by_clerk_organization(""))

    def test_concurrent_upserts_from_separate_connections_create_one_tenant(self):
        """The strongest form of the concurrency claim.

        Each thread opens its OWN PostgreSQL connection and they release
        together. A read-then-write would let several of them see "no tenant"
        and insert; the UNIQUE constraint plus ON CONFLICT DO UPDATE is what
        makes every one of them return the same row instead.

        The thread count is bounded by the application role's CONNECTION LIMIT
        — 10 in CI and in the local setup script — with headroom for the
        connection this class already holds. Raising it past the limit tests
        PostgreSQL's connection cap, not our concurrency.
        """
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        thread_count = 6
        barrier = threading.Barrier(thread_count)
        results = []
        errors = []
        lock = threading.Lock()

        def create():
            store = None
            try:
                store = PostgresLifecycleStore(DSN)
                barrier.wait(timeout=30)
                tenant = store.upsert_tenant_for_clerk_organization(
                    "org_2thundering", organization_name="Thundering Herd")
                with lock:
                    results.append(tenant["tenant_id"])
            except Exception as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=create) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(errors, [], f"concurrent upserts raised: {errors}")
        self.assertEqual(len(results), thread_count)
        self.assertEqual(len(set(results)), 1,
                         f"tenant ids diverged under concurrency: {set(results)}")

        rows = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM tenants WHERE clerk_organization_id=%s",
            ("org_2thundering",)).fetchone()["c"]
        self.assertEqual(rows, 1)

        states = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM tenant_onboarding_state").fetchone()["c"]
        self.assertEqual(states, 1, "duplicate onboarding rows under concurrency")


if __name__ == "__main__":
    unittest.main()
