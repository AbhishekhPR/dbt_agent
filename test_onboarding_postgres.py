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
#: Clerk tenants and onboarding state.
CLERK_TENANCY = 14
#: GitHub App installation binding.
INSTALLATION_BINDING = 15
#: Repository selection, dbt configuration and CI state.
NEW_LATEST = 16


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
            for table in ("tenants", "tenant_onboarding_state",
                          "github_installation_states", "clerk_github_identities",
                          "github_installations", "tenant_github_installations",
                          "tenant_repositories"):
                self.assertIn(table, tables)
        finally:
            store.close()

    def test_upgrade_from_the_previous_latest_version(self):
        """The path production will actually take."""
        from agent.postgres_migrate import applied_versions

        _apply_up_to(DSN, PREVIOUS_LATEST)
        store = self._store()
        try:
            versions = applied_versions(store.connection)
            # 0014 must be applied immediately after 13. Deliberately NOT
            # "14 is the highest version": it was when this was written, and
            # every later migration would otherwise break a test whose subject
            # is 0014 and nothing else.
            self.assertIn(CLERK_TENANCY, versions)
            self.assertEqual(versions[versions.index(CLERK_TENANCY) - 1],
                             PREVIOUS_LATEST)
            store.connection.execute("SELECT 1 FROM tenants LIMIT 1")
        finally:
            store.close()

    def test_upgrade_from_clerk_tenancy_to_installation_binding(self):
        """14 -> 15, the path a deployment already on Phase 1 will take."""
        from agent.postgres_migrate import applied_versions

        _apply_up_to(DSN, CLERK_TENANCY)
        store = self._store()
        try:
            versions = applied_versions(store.connection)
            self.assertIn(INSTALLATION_BINDING, versions)
            self.assertEqual(
                versions[versions.index(INSTALLATION_BINDING) - 1],
                CLERK_TENANCY)
            for table in ("github_installation_states", "clerk_github_identities",
                          "github_installations", "tenant_github_installations"):
                self.assertIsNotNone(
                    store.connection.execute(
                        "SELECT to_regclass(%s) AS n", (f"public.{table}",)
                    ).fetchone()["n"], table)
        finally:
            store.close()

    def test_upgrade_to_installation_binding_preserves_tenants(self):
        """A migration that loses Phase 1 tenants is not a migration."""
        _apply_up_to(DSN, CLERK_TENANCY)
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, clerk_organization_id, "
                "organization_name) VALUES (%s, 'org_2kept', 'Kept')",
                (f"ten_{'a' * 32}",))
            conn.execute(
                "INSERT INTO tenant_onboarding_state (tenant_id) VALUES (%s)",
                (f"ten_{'a' * 32}",))

        store = self._store()
        try:
            row = store.tenant_by_clerk_organization("org_2kept")
            self.assertIsNotNone(row)
            self.assertEqual(row["organization_name"], "Kept")
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


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class GitHubInstallationConstraintTests(unittest.TestCase):
    """Constraints the binding logic relies on, asserted directly."""

    @classmethod
    def setUpClass(cls):
        _reset_schema(DSN)
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        self.store.connection.execute("DELETE FROM tenant_github_installations")
        self.store.connection.execute("DELETE FROM github_installations")
        self.store.connection.execute("DELETE FROM github_installation_states")
        self.store.connection.execute("DELETE FROM tenants")
        self.acme = self.store.upsert_tenant_for_clerk_organization(
            "org_2acme", organization_name="Acme")["tenant_id"]
        self.globex = self.store.upsert_tenant_for_clerk_organization(
            "org_2globex", organization_name="Globex")["tenant_id"]

    def _facts(self, installation_id):
        self.store.record_github_installation(
            installation_id, github_account_id=5001,
            github_account_login="acme", github_account_type="Organization")

    def test_an_installation_belongs_to_exactly_one_tenant(self):
        """Enforced by PRIMARY KEY, not by application logic."""
        self._facts(4001)
        self.store.bind_github_installation_to_tenant(
            4001, tenant_id=self.acme, bound_by_clerk_user_id="u",
            verified_github_user_id=1)
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO tenant_github_installations "
                "(github_installation_id, tenant_id, bound_by_clerk_user_id, "
                " verified_github_user_id) VALUES (4001, %s, 'u', 1)",
                (self.globex,))

    def test_a_tenant_may_hold_many_installations(self):
        for installation_id in (4101, 4102, 4103):
            self._facts(installation_id)
            self.store.bind_github_installation_to_tenant(
                installation_id, tenant_id=self.acme,
                bound_by_clerk_user_id="u", verified_github_user_id=1)
        self.assertEqual(len(self.store.tenant_github_installations(self.acme)), 3)

    def test_a_cross_tenant_rebind_raises_rather_than_repointing(self):
        from agent.postgres_lifecycle_store import TenantInstallationConflict

        self._facts(4201)
        self.store.bind_github_installation_to_tenant(
            4201, tenant_id=self.acme, bound_by_clerk_user_id="u",
            verified_github_user_id=1)
        with self.assertRaises(TenantInstallationConflict):
            self.store.bind_github_installation_to_tenant(
                4201, tenant_id=self.globex, bound_by_clerk_user_id="v",
                verified_github_user_id=2)
        self.assertEqual(self.store.tenant_for_github_installation(4201),
                         self.acme)

    def test_an_account_login_is_not_unique(self):
        """An account can uninstall and reinstall; both rows are legitimate."""
        for installation_id in (4301, 4302):
            self.store.record_github_installation(
                installation_id, github_account_id=7001,
                github_account_login="same-account",
                github_account_type="Organization")
        count = self.store.connection.execute(
            "SELECT COUNT(*) AS c FROM github_installations "
            "WHERE github_account_login = 'same-account'").fetchone()["c"]
        self.assertEqual(count, 2)

    def test_an_unknown_account_type_is_rejected(self):
        with self.assertRaises(Exception):
            self.store.record_github_installation(
                4401, github_account_id=1, github_account_login="x",
                github_account_type="Robot")

    def test_a_binding_cannot_reference_an_unknown_installation(self):
        from agent.postgres_lifecycle_store import TenantInstallationConflict

        with self.assertRaises((Exception, TenantInstallationConflict)):
            self.store.bind_github_installation_to_tenant(
                999999, tenant_id=self.acme, bound_by_clerk_user_id="u",
                verified_github_user_id=1)

    def test_a_state_hash_is_unique(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        digest = "a" * 64
        self.store.create_github_installation_state(
            state_hash=digest, tenant_id=self.acme, clerk_user_id="u",
            created_at=now, expires_at=now + timedelta(minutes=10))
        with self.assertRaises(Exception):
            self.store.create_github_installation_state(
                state_hash=digest, tenant_id=self.globex, clerk_user_id="v",
                created_at=now, expires_at=now + timedelta(minutes=10))

    def test_a_state_hash_must_be_a_sha256_hex_digest(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        for bad in ("short", "z" * 64, "A" * 64, ""):
            with self.assertRaises(Exception, msg=bad):
                self.store.create_github_installation_state(
                    state_hash=bad, tenant_id=self.acme, clerk_user_id="u",
                    created_at=now, expires_at=now + timedelta(minutes=10))

    def test_an_unknown_state_purpose_is_rejected(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        with self.assertRaises(Exception):
            self.store.create_github_installation_state(
                state_hash="b" * 64, tenant_id=self.acme, clerk_user_id="u",
                created_at=now, expires_at=now + timedelta(minutes=10), purpose="something_else")

    def test_deleting_a_tenant_removes_its_states_and_bindings(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        self._facts(4501)
        self.store.bind_github_installation_to_tenant(
            4501, tenant_id=self.acme, bound_by_clerk_user_id="u",
            verified_github_user_id=1)
        self.store.create_github_installation_state(
            state_hash="c" * 64, tenant_id=self.acme, clerk_user_id="u",
            created_at=now, expires_at=now + timedelta(minutes=10))

        self.store.connection.execute("DELETE FROM tenants WHERE tenant_id = %s",
                                      (self.acme,))
        self.assertIsNone(self.store.tenant_for_github_installation(4501))
        self.assertIsNone(self.store.github_installation_state("c" * 64))
        # The installation FACTS survive: GitHub still has the App installed.
        self.assertIsNotNone(self.store.github_installation(4501))


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; PostgreSQL suite requires a real server")
class RepositoryOnboardingMigrationTests(unittest.TestCase):
    """Migration 0016, from empty and from the Phase 2 schema."""

    def setUp(self):
        _reset_schema(DSN)

    def _store(self):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        return PostgresLifecycleStore(DSN)

    def test_upgrade_from_installation_binding_to_repository_onboarding(self):
        """15 -> 16, the path a deployment already on Phase 2 will take."""
        from agent.postgres_migrate import applied_versions

        _apply_up_to(DSN, INSTALLATION_BINDING)
        store = self._store()
        try:
            versions = applied_versions(store.connection)
            self.assertIn(NEW_LATEST, versions)
            self.assertEqual(versions[versions.index(NEW_LATEST) - 1],
                             INSTALLATION_BINDING)
            self.assertIsNotNone(store.connection.execute(
                "SELECT to_regclass('public.tenant_repositories') AS n"
            ).fetchone()["n"])
            # Completion columns are added to the existing table rather than
            # duplicating tenant_onboarding_state.completed_at.
            columns = {row["column_name"] for row in store.connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'tenant_onboarding_state'").fetchall()}
            self.assertIn("completed_repository_id", columns)
            self.assertIn("completed_by_clerk_user_id", columns)
        finally:
            store.close()

    def test_upgrade_preserves_phase_two_bindings(self):
        """A migration that loses verified installation bindings is broken."""
        import psycopg

        _apply_up_to(DSN, INSTALLATION_BINDING)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenants (tenant_id, clerk_organization_id, "
                "organization_name) VALUES (%s, 'org_2kept', 'Kept')",
                (f"ten_{'b' * 32}",))
            conn.execute(
                "INSERT INTO github_installations (github_installation_id, "
                "github_account_id, github_account_login, github_account_type) "
                "VALUES (5150, 1, 'kept', 'Organization')")
            conn.execute(
                "INSERT INTO tenant_github_installations "
                "(github_installation_id, tenant_id, bound_by_clerk_user_id, "
                " verified_github_user_id) VALUES (5150, %s, 'u', 1)",
                (f"ten_{'b' * 32}",))

        store = self._store()
        try:
            self.assertEqual(
                store.tenant_for_github_installation(5150), f"ten_{'b' * 32}")
        finally:
            store.close()

    def test_a_repository_belongs_to_exactly_one_tenant(self):
        from agent.postgres_lifecycle_store import TenantRepositoryConflict

        store = self._store()
        try:
            first = store.upsert_tenant_for_clerk_organization(
                "org_2a", organization_name="A")["tenant_id"]
            second = store.upsert_tenant_for_clerk_organization(
                "org_2b", organization_name="B")["tenant_id"]
            store.record_github_installation(
                7001, github_account_id=1, github_account_login="a",
                github_account_type="Organization")
            store.select_tenant_repository(
                8001, tenant_id=first, github_installation_id=7001,
                owner_login="a", name="repo")
            with self.assertRaises(TenantRepositoryConflict):
                store.select_tenant_repository(
                    8001, tenant_id=second, github_installation_id=7001,
                    owner_login="a", name="repo")
        finally:
            store.close()

    def test_removing_an_installation_removes_its_repositories(self):
        """A repository reachable only through an installation that is gone
        must not stay configured."""
        store = self._store()
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_2c", organization_name="C")["tenant_id"]
            store.record_github_installation(
                7002, github_account_id=1, github_account_login="c",
                github_account_type="Organization")
            store.select_tenant_repository(
                8002, tenant_id=tenant, github_installation_id=7002,
                owner_login="c", name="repo")
            store.connection.execute(
                "DELETE FROM github_installations "
                "WHERE github_installation_id = 7002")
            self.assertIsNone(store.tenant_repository(tenant, 8002))
        finally:
            store.close()

    def test_the_database_refuses_an_escaping_manifest_path(self):
        """Defence in depth: the API validates, and so does the schema."""
        store = self._store()
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_2d", organization_name="D")["tenant_id"]
            store.record_github_installation(
                7003, github_account_id=1, github_account_login="d",
                github_account_type="Organization")
            store.select_tenant_repository(
                8003, tenant_id=tenant, github_installation_id=7003,
                owner_login="d", name="repo")
            for bad in ("/etc/passwd", "C:/win/m.json", "../escape.json",
                        "a/../b.json", "a//b.json", "a/"):
                with self.assertRaises(Exception, msg=bad):
                    store.connection.execute(
                        "UPDATE tenant_repositories SET manifest_path = %s "
                        "WHERE github_repository_id = 8003", (bad,))
        finally:
            store.close()

    def test_an_unknown_enforcement_mode_is_refused_by_the_database(self):
        store = self._store()
        try:
            tenant = store.upsert_tenant_for_clerk_organization(
                "org_2e", organization_name="E")["tenant_id"]
            store.record_github_installation(
                7004, github_account_id=1, github_account_login="e",
                github_account_type="Organization")
            store.select_tenant_repository(
                8004, tenant_id=tenant, github_installation_id=7004,
                owner_login="e", name="repo")
            with self.assertRaises(Exception):
                store.connection.execute(
                    "UPDATE tenant_repositories SET enforcement_mode = 'block' "
                    "WHERE github_repository_id = 8004")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
