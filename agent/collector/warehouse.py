"""Read-only PostgreSQL metadata reader for the collector.

This is a genuine execution path, not a stub, but it is honest about what it
is: PostgreSQL. It is not a Snowflake, BigQuery or Databricks adapter, and it
must not be described as one.

Every connection is opened read-only with a statement timeout. Every statement
is either a parameterised catalog lookup or a locally built aggregate over an
explicitly requested relation. Nothing this class returns contains a row.
"""
from __future__ import annotations

from agent.collector.signals import (
    UnsafeIdentifierError,
    profile_query,
    relation_exists_query,
    relation_in_catalog_query,
    schema_fingerprint,
    split_relation,
    structure_query,
    validate_column,
)


class WarehouseUnavailable(RuntimeError):
    """The warehouse could not be reached or refused the session.

    Carries no DSN and no credential material - only what went wrong.
    """


class RelationMissing(LookupError):
    """A requested relation does not exist. A fact, not a failure."""


class RelationNotReadable(RuntimeError):
    """The relation exists but these credentials cannot read it.

    Distinct from RelationMissing on purpose. Absence is evidence and produces
    a BLOCK; a missing GRANT is a collector misconfiguration and must be fixed
    by the operator, not reported to Relium as a schema finding.
    """


class PostgresMetadataReader:
    """Collects requested metadata from a PostgreSQL-compatible warehouse."""

    provider = "postgres"

    def __init__(self, dsn, *, statement_timeout_ms=30_000, connect_timeout=15):
        self._dsn = dsn
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._connect_timeout = int(connect_timeout)

    def __repr__(self):  # pragma: no cover - trivial
        return f"PostgresMetadataReader(provider={self.provider!r}, dsn=<redacted>)"

    __str__ = __repr__

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise WarehouseUnavailable(
                "the PostgreSQL driver (psycopg) is not installed") from exc
        try:
            connection = psycopg.connect(
                self._dsn,
                connect_timeout=self._connect_timeout,
                # A read-only session is enforced by the server, not by
                # convention: even a defect in query construction cannot write.
                options=f"-c statement_timeout={self._statement_timeout_ms} "
                        f"-c default_transaction_read_only=on "
                        f"-c application_name=relium-collector",
            )
        except Exception as exc:
            # Deliberately does not interpolate the exception's own text, which
            # in some drivers echoes the connection string.
            raise WarehouseUnavailable(
                f"could not connect to the warehouse ({type(exc).__name__})") from None
        connection.read_only = True
        return connection

    def collect_relation(self, *, relation_name, columns, signals):
        """Return the metadata for exactly one requested relation.

        ``columns`` and ``signals`` are the ones named in the collection
        request. Nothing else is read, and no other relation is touched.
        """
        schema, table = split_relation(relation_name)
        wanted_columns = [validate_column(c) for c in (columns or [])]
        signal_set = set(signals or ())

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(relation_exists_query(), (schema, table))
                visible = bool(cursor.fetchone()[0])
                if not visible:
                    # information_schema is privilege-filtered. Ask the catalog
                    # before concluding the relation is gone.
                    cursor.execute(relation_in_catalog_query(), (schema, table))
                    if cursor.fetchone()[0]:
                        raise RelationNotReadable(
                            f"relation {schema}.{table} exists but these "
                            f"credentials cannot read it; grant SELECT on it "
                            f"to the collector role")
                    raise RelationMissing(
                        f"relation {schema}.{table} does not exist in this warehouse")

                cursor.execute(structure_query(), (schema, table))
                catalog = [
                    {"column_name": row[0], "data_type": row[1],
                     "is_nullable": str(row[2]).upper() == "YES",
                     "ordinal_position": row[3]}
                    for row in cursor.fetchall()
                ]
                present = {c["column_name"]: c for c in catalog}

                # Profile only the requested columns that actually exist. A
                # missing column is reported as a fact; it is never invented,
                # and it must not abort the rest of the collection.
                profilable = [c for c in wanted_columns if c in present]
                aggregates = {}
                if signal_set & {"row_count", "null_rate", "duplicate_rate",
                                 "distinct_count", "cardinality"}:
                    sql, plan = profile_query(schema, table, profilable, signal_set)
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    aggregates = dict(zip(plan, row))

        return self._assemble(
            schema=schema, table=table, relation_name=relation_name,
            catalog=catalog, present=present, wanted_columns=wanted_columns,
            aggregates=aggregates, signals=signal_set)

    def _assemble(self, *, schema, table, relation_name, catalog, present,
                  wanted_columns, aggregates, signals):
        row_count = aggregates.get("row_count")
        columns = []
        for name in wanted_columns:
            entry = present.get(name)
            if entry is None:
                # The column genuinely is not there. That is exactly the
                # finding Relium needs, so it is reported, not hidden.
                columns.append({
                    "column_name": name,
                    "collection_status": "COLLECTED",
                    "data_type": None,
                    "exists": False,
                })
                continue
            column = {"column_name": name, "collection_status": "COLLECTED"}
            if "data_type" in signals:
                column["data_type"] = entry["data_type"]
            if "is_nullable" in signals:
                column["is_nullable"] = entry["is_nullable"]
            if "column_exists" in signals:
                column["ordinal_position"] = entry["ordinal_position"]

            nulls = aggregates.get("nulls__" + name)
            if "null_rate" in signals and nulls is not None:
                column["null_count"] = int(nulls)
                column["null_rate"] = (float(nulls) / row_count) if row_count else 0.0
            distinct = aggregates.get("distinct__" + name)
            if distinct is not None:
                if "distinct_count" in signals:
                    column["distinct_count"] = int(distinct)
                if "cardinality" in signals and row_count:
                    column["cardinality"] = float(distinct) / row_count
                if "duplicate_rate" in signals and row_count:
                    duplicates = max(int(row_count) - int(distinct), 0)
                    column["duplicate_count"] = duplicates
                    column["duplicate_rate"] = float(duplicates) / row_count
            columns.append(column)

        relation = {
            "relation_name": relation_name,
            "relation_schema": schema,
            "relation_type": "table",
            "exists_in_production": True,
            "collection_status": "COLLECTED",
            "columns": columns,
        }
        if "row_count" in signals:
            relation["row_count"] = int(row_count) if row_count is not None else None
        if "schema_fingerprint" in signals:
            relation["schema_fingerprint"] = schema_fingerprint(catalog)
        return relation

    @staticmethod
    def missing_relation(relation_name):
        """The snapshot fragment for a relation that is absent from production.

        Absence is evidence. The decision engine reads exists_in_production
        directly, so this must be reported rather than omitted.
        """
        return {
            "relation_name": relation_name,
            "exists_in_production": False,
            "collection_status": "COLLECTED",
            "columns": [],
        }
