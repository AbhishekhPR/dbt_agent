"""The closed signal vocabulary, and the locally generated queries for it.

This module is the collector's security boundary. Relium describes *what* it
needs in terms of named signals; it never sends SQL, and the collector never
accepts any. Every statement executed against a customer warehouse is built
here, from an identifier that has been validated and quoted locally.

Three outcomes are possible for a requested signal, and they are deliberately
distinct:

  * supported     - the collector computes it
  * unimplemented - a signal Relium may legitimately ask for that this version
                    cannot compute. Reported as UNSUPPORTED evidence, never as
                    a silent pass. The decision engine already treats
                    UNSUPPORTED as "not a pass".
  * unknown       - a signal name outside the vocabulary entirely. This fails
                    closed, because it means the collector and the control
                    plane disagree about what is being asked, and guessing
                    would be worse than stopping.
"""
from __future__ import annotations

import re

# Signals this version computes. Mirrors the profile evidence level, plus the
# three pure aggregates from the full level.
SUPPORTED_SIGNALS = frozenset({
    "relation_exists",
    "column_exists",
    "data_type",
    "is_nullable",
    "schema_fingerprint",
    "row_count",
    "null_rate",
    "freshness",
    "duplicate_rate",
    "distinct_count",
    "cardinality",
})

# Known to the vocabulary, deliberately not computed in v0. min_max is the
# only signal whose value is an actual cell from a customer table; it is left
# out until there is a reason to carry customer values across the boundary.
UNIMPLEMENTED_SIGNALS = frozenset({"min_max"})

KNOWN_SIGNALS = SUPPORTED_SIGNALS | UNIMPLEMENTED_SIGNALS

# Signals answered from the catalog rather than from the data.
STRUCTURE_SIGNALS = frozenset({
    "relation_exists", "column_exists", "data_type", "is_nullable",
    "schema_fingerprint",
})

# Signals that require reading the table, and are computed as aggregates only.
PROFILE_SIGNALS = frozenset({
    "row_count", "null_rate", "duplicate_rate", "distinct_count", "cardinality",
})

# `freshness` is satisfied at snapshot level, by observed_at and ttl_seconds -
# which is exactly what classify_freshness() consumes. It requires no query.
SNAPSHOT_LEVEL_SIGNALS = frozenset({"freshness"})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class UnknownSignalError(ValueError):
    """A requested signal is outside the vocabulary. Fail closed."""


class UnsafeIdentifierError(ValueError):
    """A relation or column name is not a plain SQL identifier."""


def classify_signals(required_signals):
    """Split requested signals into (supported, unimplemented).

    Raises UnknownSignalError if any name is outside the vocabulary. The
    collector must not guess at a signal it does not recognise.
    """
    requested = [str(s) for s in (required_signals or [])]
    unknown = sorted({s for s in requested if s not in KNOWN_SIGNALS})
    if unknown:
        raise UnknownSignalError(
            f"unknown signal(s) {', '.join(unknown)}; this collector supports "
            f"{', '.join(sorted(KNOWN_SIGNALS))}")
    supported = sorted({s for s in requested if s in SUPPORTED_SIGNALS})
    unimplemented = sorted({s for s in requested if s in UNIMPLEMENTED_SIGNALS})
    return supported, unimplemented


def split_relation(relation_name, *, relation_schema=None):
    """Resolve a target to validated (schema, table) parts.

    ``relation_schema`` comes from the collection request and is authoritative
    when present: the request already carries the schema dbt resolved, so the
    remainder of ``relation_name`` is the physical identifier. Splitting on the
    first dot instead would misread any schema that contains one.

    Validation happens here rather than at query-build time, so an unsafe name
    can never reach a statement whatever the caller does next.
    """
    text = str(relation_name or "").strip()
    if not text:
        raise UnsafeIdentifierError("relation name is empty")

    schema = str(relation_schema).strip() if relation_schema else None
    if schema and text.lower().startswith(schema.lower() + "."):
        table = text[len(schema) + 1:]
    else:
        parts = text.split(".")
        if len(parts) == 1:
            schema, table = schema or "public", parts[0]
        elif len(parts) == 2:
            schema, table = parts
        else:
            # Three-part names would mean cross-database access. Refuse rather
            # than silently reinterpret what the request meant.
            raise UnsafeIdentifierError(
                f"relation {text!r} is not a schema-qualified name")

    for part in (schema, table):
        if not _IDENTIFIER.fullmatch(part or ""):
            raise UnsafeIdentifierError(f"unsafe identifier: {part!r}")
    return schema, table


def validate_column(column_name):
    name = str(column_name or "").strip()
    if not _IDENTIFIER.fullmatch(name):
        raise UnsafeIdentifierError(f"unsafe column identifier: {name!r}")
    return name


def quote(identifier):
    """Quote a *previously validated* identifier."""
    if not _IDENTIFIER.fullmatch(str(identifier)):
        raise UnsafeIdentifierError(f"refusing to quote {identifier!r}")
    return '"' + str(identifier) + '"'


def structure_query():
    """Catalog lookup for one relation's requested columns.

    Parameterised: the schema, table and column list are bound values, not
    interpolated text.
    """
    return (
        "SELECT column_name, data_type, is_nullable, ordinal_position "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )


def relation_exists_query():
    """Visibility through information_schema, which is privilege-filtered."""
    return (
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s"
    )


def relation_in_catalog_query():
    """Existence through pg_catalog, which is NOT privilege-filtered.

    The two together separate two very different facts. information_schema
    hides a relation the current role cannot read, so a missing GRANT looks
    exactly like a dropped table - and reporting a dropped table produces a
    BLOCK. A permissions gap must never be laundered into a schema finding.
    """
    return (
        "SELECT count(*) FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s "
        "AND c.relkind IN ('r', 'v', 'm', 'f', 'p')"
    )


def profile_query(schema, table, columns, signals):
    """One bounded aggregate statement for a relation.

    Returns (sql, result_plan). Every projected expression is an aggregate, so
    the result set is a single row of numbers and can never carry a customer
    record. There is no SELECT *, no LIMIT-of-rows, and no predicate built
    from customer input.
    """
    schema_q, table_q = quote(schema), quote(table)
    selects = ["count(*) AS row_count"]
    plan = ["row_count"]

    wants_nulls = "null_rate" in signals
    wants_distinct = bool({"distinct_count", "cardinality", "duplicate_rate"} & set(signals))

    for column in columns:
        column_q = quote(validate_column(column))
        if wants_nulls:
            selects.append(
                f"count(*) FILTER (WHERE {column_q} IS NULL) AS "
                f"{quote('nulls__' + column)}")
            plan.append("nulls__" + column)
        if wants_distinct:
            selects.append(f"count(DISTINCT {column_q}) AS "
                           f"{quote('distinct__' + column)}")
            plan.append("distinct__" + column)

    sql = "SELECT " + ", ".join(selects) + f" FROM {schema_q}.{table_q}"
    return sql, plan


def schema_fingerprint(columns):
    """A stable fingerprint of a relation's shape.

    Names, types and nullability only - never values. Two warehouses with the
    same shape produce the same fingerprint.
    """
    import hashlib

    material = ";".join(
        f"{c['column_name']}:{c.get('data_type')}:{c.get('is_nullable')}"
        for c in sorted(columns, key=lambda c: str(c["column_name"]))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
