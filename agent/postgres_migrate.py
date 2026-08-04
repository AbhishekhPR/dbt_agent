"""Versioned, transactional PostgreSQL schema migrations for the lifecycle store."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations") / "postgres"

_VERSION_RE = re.compile(r"^(\d{4})_")


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def _version_of(path: Path) -> int:
    match = _VERSION_RE.match(path.name)
    if not match:
        raise ValueError(f"Migration file {path.name} must start with a 4-digit version prefix")
    return int(match.group(1))


def pending_migrations(applied_versions: set[int]) -> list[Path]:
    return [p for p in _migration_files() if _version_of(p) not in applied_versions]


def apply_migrations(connection) -> list[int]:
    """Apply every pending migration in order, each in its own transaction.

    Returns the list of newly applied version numbers. Safe to call repeatedly:
    already-applied versions are skipped. A migration that fails is rolled back
    and no later migration is attempted.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )

    applied = {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    newly_applied = []
    for path in pending_migrations(applied):
        version = _version_of(path)
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        # Each migration file runs in its own transaction: a failure rolls back
        # only that file, and it is never recorded as applied.
        with connection.transaction():
            connection.execute(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
        newly_applied.append(version)
    return newly_applied


def applied_versions(connection) -> list[int]:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    return sorted(row["version"] for row in connection.execute("SELECT version FROM schema_migrations").fetchall())
