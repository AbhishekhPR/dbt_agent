"""Create the isolated E2E databases and apply the lifecycle migrations.

Two separate databases on the isolated cluster:

  * ``relium_lifecycle`` - Relium's own PostgreSQL lifecycle store.
  * ``fixture_warehouse`` - the fake customer warehouse the producer mutates.

They are separate databases so the fixture producer physically cannot reach a
Relium table with the credential it is given. That boundary is the point.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402

from agent.postgres_migrate import apply_migrations  # noqa: E402

ADMIN_DSN = "postgresql://relium_e2e@127.0.0.1:55433/postgres"
LIFECYCLE_DSN = "postgresql://relium_e2e@127.0.0.1:55433/relium_lifecycle"
WAREHOUSE_DSN = "postgresql://relium_e2e@127.0.0.1:55433/fixture_warehouse"


def ensure_databases() -> list[str]:
    created = []
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        for name in ("relium_lifecycle", "fixture_warehouse"):
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}"')
                created.append(name)
    return created


def migrate() -> list[int]:
    with psycopg.connect(LIFECYCLE_DSN, autocommit=True,
                         row_factory=psycopg.rows.dict_row) as conn:
        return apply_migrations(conn)


def main() -> int:
    created = ensure_databases()
    print(f"databases created: {created or 'none (already present)'}")
    applied = migrate()
    print(f"migrations applied: {applied or 'none (already current)'}")

    with psycopg.connect(LIFECYCLE_DSN, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        ).fetchall()
    names = [r["table_name"] for r in rows]
    print(f"lifecycle tables: {len(names)}")
    for expected in ("reviews", "review_attempts", "review_lifecycle_transitions",
                     "collection_requests", "metadata_snapshots", "snapshot_relations",
                     "snapshot_columns", "outbox_events", "rca_reports", "incidents"):
        marker = "ok" if expected in names else "MISSING"
        print(f"  [{marker}] {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
