"""PostgreSQL authoritative lifecycle-store boundary.

The local SQLite store implements the same contract for deterministic tests;
this adapter refuses to operate without an explicitly supplied DSN.
"""

from pathlib import Path

from agent.sqlite_lifecycle_store import SQLiteLifecycleStore


class PostgresLifecycleStore:
    provider = "postgresql"

    def __init__(self, dsn: str | None):
        if not dsn:
            raise RuntimeError("POSTGRES lifecycle store is BLOCKED BY CREDENTIALS")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL lifecycle store requires psycopg") from exc
        self.connection = psycopg.connect(dsn)
        self.connection.execute(Path(__file__).with_name("lifecycle_schema.sql").read_text(encoding="utf-8"))
        self.connection.commit()

