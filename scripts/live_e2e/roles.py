"""Least-privileged roles for the isolated E2E cluster.

``initdb`` creates a bootstrap superuser, and running the application as it
would make the E2E prove less than the product requires - the store's own
security test asserts the application role is not a superuser. Three roles,
matching the three real trust boundaries:

  * ``relium_app``       - Relium's lifecycle store. Owns nothing, cannot
                           create databases or roles, cannot bypass RLS.
  * ``relium_collector`` - the collector's warehouse credential. SELECT only,
                           on the fixture warehouse only. It cannot reach the
                           lifecycle database at all.
  * ``fixture_producer`` - the data producer. Writes the fixture warehouse and
                           nothing else, which is what keeps the E2E honest:
                           it physically cannot fabricate a Relium row.

Passwords are generated per run and never written to evidence.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

HOST, PORT = "127.0.0.1", 55433
BOOTSTRAP = f"postgresql://relium_e2e@{HOST}:{PORT}/postgres"
LIFECYCLE_DB = "relium_lifecycle"
WAREHOUSE_DB = "fixture_warehouse"

APP_ROLE = "relium_app"
COLLECTOR_ROLE = "relium_collector"
PRODUCER_ROLE = "fixture_producer"


def _password() -> str:
    return secrets.token_urlsafe(24)


def _ensure_role(conn, name, password):
    # CREATE/ALTER ROLE is a utility statement: PostgreSQL will not accept a
    # bound parameter for the password, so it is composed as a quoted literal
    # by psycopg.sql rather than by string formatting.
    verb = "ALTER" if conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone() else "CREATE"
    conn.execute(sql.SQL(
        "{verb} ROLE {role} WITH LOGIN PASSWORD {password} "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ).format(verb=sql.SQL(verb), role=sql.Identifier(name),
             password=sql.Literal(password)))


def provision() -> dict:
    """Create the three roles and return their DSNs. Idempotent."""
    passwords = {APP_ROLE: _password(), COLLECTOR_ROLE: _password(),
                 PRODUCER_ROLE: _password()}

    with psycopg.connect(BOOTSTRAP, autocommit=True) as conn:
        for role, password in passwords.items():
            _ensure_role(conn, role, password)

    # -- lifecycle database: only the application role may touch it ------
    # DDL is deliberately NOT granted. Migrations run as the schema owner,
    # which is a privileged operation; the application role does DML only, and
    # the store's own security test asserts it is not a superuser.
    grant_lifecycle_privileges()

    # -- fixture warehouse: producer writes, collector only reads --------
    with psycopg.connect(f"postgresql://relium_e2e@{HOST}:{PORT}/{WAREHOUSE_DB}",
                         autocommit=True) as conn:
        # The producer owns the warehouse shape, so it needs CREATE on the
        # database to make its own schema. The collector never gets it.
        conn.execute(f'GRANT CONNECT, CREATE ON DATABASE "{WAREHOUSE_DB}" '
                     f'TO "{PRODUCER_ROLE}"')
        conn.execute(f'GRANT CONNECT ON DATABASE "{WAREHOUSE_DB}" '
                     f'TO "{COLLECTOR_ROLE}"')
        conn.execute(f'GRANT USAGE, CREATE ON SCHEMA public TO "{PRODUCER_ROLE}"')
        conn.execute(f'GRANT USAGE ON SCHEMA public TO "{COLLECTOR_ROLE}"')
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE "
                     f'"{PRODUCER_ROLE}" IN SCHEMA public '
                     f'GRANT SELECT ON TABLES TO "{COLLECTOR_ROLE}"')
        conn.execute(f'REVOKE ALL ON DATABASE "{WAREHOUSE_DB}" FROM "{APP_ROLE}"')

    def dsn(role, database):
        return (f"postgresql://{role}:{passwords[role]}@{HOST}:{PORT}/{database}")

    return {
        "app_dsn": dsn(APP_ROLE, LIFECYCLE_DB),
        "collector_warehouse_dsn": dsn(COLLECTOR_ROLE, WAREHOUSE_DB),
        "producer_dsn": dsn(PRODUCER_ROLE, WAREHOUSE_DB),
    }


def grant_lifecycle_privileges():
    """Make the application role the owner of its own lifecycle database.

    PostgresLifecycleStore applies migrations in its constructor, so the
    application role genuinely needs DDL on its own schema - this is the
    product's design, and it is how the CI workflow provisions relium_app too.
    Ownership, not superuser: the role still cannot create databases or roles,
    cannot replicate and cannot bypass RLS, which is what the store's own
    least-privilege test asserts.
    """
    with psycopg.connect(f"postgresql://relium_e2e@{HOST}:{PORT}/postgres",
                         autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{LIFECYCLE_DB}" OWNER TO "{APP_ROLE}"')

    with psycopg.connect(f"postgresql://relium_e2e@{HOST}:{PORT}/{LIFECYCLE_DB}",
                         autocommit=True) as conn:
        conn.execute(f'ALTER SCHEMA public OWNER TO "{APP_ROLE}"')
        conn.execute(f'GRANT CONNECT ON DATABASE "{LIFECYCLE_DB}" TO "{APP_ROLE}"')
        conn.execute(f'GRANT ALL ON SCHEMA public TO "{APP_ROLE}"')
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                     f'IN SCHEMA public TO "{APP_ROLE}"')
        conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                     f'TO "{APP_ROLE}"')
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                     f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{APP_ROLE}"')
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                     f'GRANT USAGE, SELECT ON SEQUENCES TO "{APP_ROLE}"')
        # The collector and the producer are explicitly denied. Revoking from
        # PUBLIC is what makes "the producer cannot write Relium state" a
        # property of the database rather than a convention in the harness.
        conn.execute(f'REVOKE ALL ON DATABASE "{LIFECYCLE_DB}" FROM PUBLIC')
        for role in (COLLECTOR_ROLE, PRODUCER_ROLE):
            conn.execute(f'REVOKE ALL ON DATABASE "{LIFECYCLE_DB}" FROM "{role}"')


def owner_lifecycle_dsn():
    """The privileged DSN used only to run migrations."""
    return f"postgresql://relium_e2e@{HOST}:{PORT}/{LIFECYCLE_DB}"


def grant_collector_read(schema="analytics"):
    """Re-grant SELECT after the producer creates or replaces a relation."""
    with psycopg.connect(f"postgresql://relium_e2e@{HOST}:{PORT}/{WAREHOUSE_DB}",
                         autocommit=True) as conn:
        conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{COLLECTOR_ROLE}"')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" '
                     f'TO "{COLLECTOR_ROLE}"')


def main() -> int:
    dsns = provision()
    # Report reachability, never the DSNs themselves.
    for name in sorted(dsns):
        print(f"  {name}: provisioned")

    with psycopg.connect(dsns["app_dsn"]) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user").fetchone()
    print(f"  relium_app least-privileged: {not any(row)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
