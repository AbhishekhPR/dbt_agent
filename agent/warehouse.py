"""Provider-neutral, read-only warehouse metadata boundary."""

from __future__ import annotations

import re
import sqlite3
from typing import Any


class WarehouseSafetyError(ValueError):
    pass


class WarehouseCredentialsError(RuntimeError):
    pass


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.]*$")


class SQLiteWarehouseAdapter:
    provider = "sqlite"

    def __init__(self, connection: sqlite3.Connection, *, allowlist: set[str], timeout_seconds: float = 5.0):
        self.connection = connection
        self.allowlist = {str(value).lower() for value in allowlist}
        self.timeout_seconds = timeout_seconds

    def observe(self, table: str, *, key_columns: list[str], kpi_columns: list[str] | None = None) -> dict[str, Any]:
        _validate_identifier(table, self.allowlist)
        for column in key_columns + list(kpi_columns or []):
            _validate_identifier(column, None)
        try:
            row_count = self.connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.Error as exc:
            return {"status": "NOT EVALUATED", "reason": f"Missing or inaccessible table: {table}", "provider": self.provider}
        key_expr = ", ".join(f'"{column}"' for column in key_columns)
        distinct = self.connection.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {key_expr} FROM \"{table}\")").fetchone()[0] if key_columns else None
        duplicate = row_count - distinct if distinct is not None else None
        null_rates = {}
        for column in key_columns + list(kpi_columns or []):
            nulls = self.connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL').fetchone()[0]
            null_rates[column] = nulls / row_count if row_count else 0.0
        return {"status": "EVALUATED", "provider": self.provider, "table": table, "row_count": row_count, "distinct_key_count": distinct, "duplicate_key_count": duplicate, "null_rates": null_rates}


class PostgresWarehouseAdapter:
    provider = "postgresql"

    def __init__(self, *, dsn: str | None, allowlist: set[str], timeout_seconds: float = 5.0, max_rows: int = 1_000_000):
        self.dsn = dsn
        self.allowlist = {str(value).lower() for value in allowlist}
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows

    def observe(self, table: str, *, key_columns: list[str], kpi_columns: list[str] | None = None) -> dict[str, Any]:
        _validate_identifier(table, self.allowlist)
        if not self.dsn:
            raise WarehouseCredentialsError("REAL WAREHOUSE VALIDATION: BLOCKED BY CREDENTIALS")
        try:
            import psycopg
        except ImportError as exc:
            raise WarehouseCredentialsError("PostgreSQL adapter requires psycopg credentials and driver") from exc
        # The production adapter intentionally issues only bounded, aggregate reads.
        with psycopg.connect(self.dsn, connect_timeout=int(self.timeout_seconds), options=f"-c statement_timeout={int(self.timeout_seconds * 1000)}") as connection:
            connection.set_session(readonly=True, autocommit=True)
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                row_count = cursor.fetchone()[0]
        if row_count > self.max_rows:
            return {"status": "NOT EVALUATED", "reason": "Query cost/row bound exceeded", "provider": self.provider}
        return {"status": "EVALUATED", "provider": self.provider, "table": table, "row_count": row_count}


def _validate_identifier(identifier: str, allowlist: set[str] | None) -> None:
    if not _IDENTIFIER.fullmatch(str(identifier)):
        raise WarehouseSafetyError("Unsafe warehouse identifier")
    if allowlist is not None and str(identifier).lower() not in allowlist:
        raise WarehouseSafetyError("Warehouse relation is not allowlisted")
