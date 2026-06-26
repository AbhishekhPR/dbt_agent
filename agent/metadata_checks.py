from dataclasses import dataclass, field


@dataclass
class MetadataCheckResult:
    model_name: str
    row_count: int
    null_count: int
    duplicate_count: int
    freshness_timestamp: str | None
    schema_column_count: int
    anomalies: list[str] = field(default_factory=list)


def get_row_count(conn, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def get_null_count(conn, table_name: str, key_columns: list[str]) -> int:
    if not key_columns:
        return 0
    predicates = " OR ".join(f"{column} IS NULL" for column in key_columns)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {predicates}"
        ).fetchone()[0]
    )


def get_duplicate_count(conn, table_name: str, key_columns: list[str]) -> int:
    if not key_columns:
        return 0
    group_by = ", ".join(key_columns)
    non_null = " AND ".join(f"{column} IS NOT NULL" for column in key_columns)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(group_count - 1), 0)
        FROM (
            SELECT COUNT(*) AS group_count
            FROM {table_name}
            WHERE {non_null}
            GROUP BY {group_by}
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return int(row[0] or 0)


def get_freshness_timestamp(conn, table_name: str) -> str | None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    freshness_column = None
    if "updated_at" in columns:
        freshness_column = "updated_at"
    elif "created_at" in columns:
        freshness_column = "created_at"
    if not freshness_column:
        return None
    value = conn.execute(
        f"SELECT MAX({freshness_column}) FROM {table_name}"
    ).fetchone()[0]
    return str(value) if value is not None else None


def get_schema_column_count(conn, table_name: str) -> int:
    return len(list(conn.execute(f"PRAGMA table_info({table_name})")))


def run_metadata_checks(conn, table_name: str, key_columns: list[str]) -> MetadataCheckResult:
    row_count = get_row_count(conn, table_name)
    null_count = get_null_count(conn, table_name, key_columns)
    duplicate_count = get_duplicate_count(conn, table_name, key_columns)
    freshness_timestamp = get_freshness_timestamp(conn, table_name)
    schema_column_count = get_schema_column_count(conn, table_name)

    anomalies = []
    if row_count == 0:
        anomalies.append("Output table is empty")
    if null_count > 0:
        anomalies.append(f"{null_count} key-column nulls detected")
    if duplicate_count > 0:
        anomalies.append(f"{duplicate_count} duplicate key rows detected")
    if freshness_timestamp is None:
        anomalies.append("No freshness timestamp column found")

    return MetadataCheckResult(
        model_name=table_name,
        row_count=row_count,
        null_count=null_count,
        duplicate_count=duplicate_count,
        freshness_timestamp=freshness_timestamp,
        schema_column_count=schema_column_count,
        anomalies=anomalies,
    )
