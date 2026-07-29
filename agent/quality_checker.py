import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path


_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_REQUIRED_METRIC_FIELDS = frozenset(
    {
        "table",
        "row_count",
        "columns",
        "null_rates",
        "duplicate_rows",
        "numeric_stats",
        "distinct_counts",
    }
)
_NUMERIC_TYPE_MARKERS = (
    "INT",
    "REAL",
    "FLOA",
    "DOUB",
    "NUM",
    "DEC",
)


class QualityCheckError(Exception):
    """Raised when a quality comparison cannot safely proceed."""


class _DuplicateJsonKeyError(ValueError):
    """Raised when persisted JSON contains an ambiguous duplicate key."""


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _database_path(db_path: str | Path) -> Path:
    try:
        path = Path(db_path).expanduser().resolve()
        if not path.exists():
            raise QualityCheckError(f"SQLite database not found: {path}")
        if not path.is_file():
            raise QualityCheckError(
                f"SQLite database is not a file: {path}"
            )
        if path.stat().st_size == 0:
            raise QualityCheckError(f"SQLite database is empty: {path}")
    except QualityCheckError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise QualityCheckError(
            f"Could not access SQLite database '{db_path}': {error}"
        ) from error
    return path


def _open_readonly_database(db_path: str | Path) -> sqlite3.Connection:
    path = _database_path(db_path)
    try:
        return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as error:
        raise QualityCheckError(
            f"Could not open SQLite database read-only '{path}': {error}"
        ) from error


def collect_database_metrics(db_path: str | Path) -> dict:
    """Collect deterministic metrics from every table in a read-only database."""
    path = _database_path(db_path)
    connection = None
    try:
        connection = _open_readonly_database(path)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        return {
            table: _collect_table_metrics(cursor, table)
            for table in tables
        }
    except QualityCheckError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise QualityCheckError(
            f"Could not read SQLite database '{path}': {error}"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def get_table_metrics(db_path: str | Path, table_name: str) -> dict:
    """Return deterministic metrics for one table from a read-only database."""
    path = _database_path(db_path)
    connection = None
    try:
        connection = _open_readonly_database(path)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table_name,),
        )
        if cursor.fetchone() is None:
            raise QualityCheckError(
                f"Table not found in SQLite database: {table_name}"
            )
        return _collect_table_metrics(cursor, table_name)
    except QualityCheckError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise QualityCheckError(
            f"Could not read table '{table_name}' from "
            f"SQLite database '{path}': {error}"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _collect_table_metrics(
    cursor: sqlite3.Cursor,
    table_name: str,
) -> dict:
    quoted_table = _quote_identifier(table_name)
    cursor.execute(f"PRAGMA table_info({quoted_table})")
    column_rows = cursor.fetchall()
    if not column_rows:
        raise QualityCheckError(
            f"Could not read columns for table '{table_name}'."
        )

    columns = [row[1] for row in column_rows]
    declared_types = {row[1]: (row[2] or "").upper() for row in column_rows}

    cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
    row_count = cursor.fetchone()[0]

    null_rates = {}
    distinct_counts = {}
    numeric_stats = {}
    for column in columns:
        quoted_column = _quote_identifier(column)
        cursor.execute(
            f"SELECT COUNT(*) FROM {quoted_table} "
            f"WHERE {quoted_column} IS NULL"
        )
        null_count = cursor.fetchone()[0]
        null_rates[column] = (
            round(100.0 * null_count / row_count, 2)
            if row_count
            else 0.0
        )

        cursor.execute(
            f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table}"
        )
        distinct_counts[column] = cursor.fetchone()[0]

        declared_type = declared_types[column]
        if any(marker in declared_type for marker in _NUMERIC_TYPE_MARKERS):
            cursor.execute(
                f"SELECT MIN({quoted_column}), MAX({quoted_column}), "
                f"AVG({quoted_column}) FROM {quoted_table} "
                f"WHERE {quoted_column} IS NOT NULL"
            )
            minimum, maximum, average = cursor.fetchone()
            if minimum is not None:
                values = (minimum, maximum, average)
                if any(not _is_finite_number(value) for value in values):
                    raise QualityCheckError(
                        f"Numeric metrics for table '{table_name}', "
                        f"column '{column}' are not finite."
                    )
                numeric_stats[column] = {
                    "min": round(minimum, 2),
                    "max": round(maximum, 2),
                    "avg": round(average, 2),
                }

    quoted_columns = ", ".join(
        _quote_identifier(column) for column in columns
    )
    cursor.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT 1 FROM {quoted_table} GROUP BY {quoted_columns}"
        f")"
    )
    distinct_rows = cursor.fetchone()[0]

    return {
        "table": table_name,
        "row_count": row_count,
        "columns": columns,
        "null_rates": null_rates,
        "duplicate_rows": row_count - distinct_rows,
        "numeric_stats": numeric_stats,
        "distinct_counts": distinct_counts,
    }


def baseline_path(project_id: str, baseline_dir: str | Path) -> Path:
    """Return the project baseline path inside the selected directory."""
    identity = str(project_id)
    reserved_stem = identity.upper()
    if (
        not identity
        or not identity.isascii()
        or identity != identity.casefold()
        or not identity[0].isalnum()
        or any(
            not (character.isalnum() or character in "_-")
            for character in identity
        )
        or reserved_stem in _WINDOWS_RESERVED_NAMES
    ):
        raise QualityCheckError(
            "Project ID must be a single lowercase safe baseline "
            "filename component using letters, numbers, '-' or '_'."
        )

    try:
        selected_directory = Path(baseline_dir).expanduser()
        absolute_directory = Path(os.path.abspath(selected_directory))
        _reject_redirecting_path_components(absolute_directory)
        directory = absolute_directory.resolve()
        path = (directory / f"{identity}.json").resolve()
        path.relative_to(directory)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise QualityCheckError(
            "Quality baseline path must remain inside --baseline-dir."
        ) from error
    return path


def _reject_redirecting_path_components(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            is_junction = (
                component.is_junction()
                if hasattr(component, "is_junction")
                else False
            )
            if component.is_symlink() or is_junction:
                raise QualityCheckError(
                    "Quality baseline directory must not contain "
                    "symlinks or junctions."
                )
        except OSError as error:
            raise QualityCheckError(
                f"Could not inspect quality baseline directory "
                f"'{path}': {error}"
            ) from error


def load_baseline(
    project_id: str,
    baseline_dir: str | Path,
) -> dict:
    """Load and validate an existing project baseline without changing it."""
    path = baseline_path(project_id, baseline_dir)
    try:
        if not path.exists():
            raise QualityCheckError(
                "No quality baseline exists. "
                "Run again with --update-baseline to create one."
            )
        if not path.is_file():
            raise QualityCheckError(
                f"Quality baseline is not a file: {path}"
            )
        document = path.read_text(encoding="utf-8")
    except QualityCheckError:
        raise
    except OSError as error:
        raise QualityCheckError(
            f"Could not read quality baseline '{path}': {error}"
        ) from error

    try:
        baseline = json.loads(
            document,
            object_pairs_hook=_unique_json_object,
        )
    except _DuplicateJsonKeyError as error:
        raise _invalid_baseline(path, str(error)) from error
    except (json.JSONDecodeError, UnicodeError) as error:
        raise QualityCheckError(
            f"Quality baseline is not valid JSON: {path}"
        ) from error

    return _validate_baseline(baseline, path)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON key '{key}'")
        result[key] = value
    return result


def _validate_baseline(baseline: object, path: Path) -> dict:
    if not isinstance(baseline, dict):
        raise _invalid_baseline(path, "the top level must be an object")

    normalized_table_names = set()
    for table_name, metrics in baseline.items():
        if not isinstance(table_name, str) or not table_name:
            raise _invalid_baseline(
                path,
                "table names must be non-empty strings",
            )
        normalized_table_name = table_name.casefold()
        if normalized_table_name in normalized_table_names:
            raise _invalid_baseline(
                path,
                f"table identity '{table_name}' collides by case",
            )
        normalized_table_names.add(normalized_table_name)
        _validate_metrics(table_name, metrics, path)
    return baseline


def _validate_metrics(
    table_name: str,
    metrics: object,
    path: Path,
) -> None:
    if not isinstance(metrics, dict):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' metrics must be an object",
        )
    if set(metrics) != _REQUIRED_METRIC_FIELDS:
        missing = sorted(_REQUIRED_METRIC_FIELDS - set(metrics))
        unexpected = sorted(set(metrics) - _REQUIRED_METRIC_FIELDS)
        detail = []
        if missing:
            detail.append(f"missing fields {missing}")
        if unexpected:
            detail.append(f"unsupported fields {unexpected}")
        raise _invalid_baseline(
            path,
            f"table '{table_name}' has {' and '.join(detail)}",
        )
    if metrics["table"] != table_name:
        raise _invalid_baseline(
            path,
            f"table key '{table_name}' does not match its metric identity",
        )

    row_count = metrics["row_count"]
    duplicate_rows = metrics["duplicate_rows"]
    if not _is_nonnegative_integer(row_count):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' row_count must be a non-negative integer",
        )
    if (
        not _is_nonnegative_integer(duplicate_rows)
        or duplicate_rows > row_count
    ):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' duplicate_rows is invalid",
        )

    columns = metrics["columns"]
    if (
        not isinstance(columns, list)
        or any(not isinstance(column, str) or not column for column in columns)
        or len({column.casefold() for column in columns}) != len(columns)
    ):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' columns must be unique non-empty strings",
        )
    column_set = set(columns)

    null_rates = metrics["null_rates"]
    if not isinstance(null_rates, dict) or set(null_rates) != column_set:
        raise _invalid_baseline(
            path,
            f"table '{table_name}' null_rates must match columns",
        )
    for column, value in null_rates.items():
        if not _is_finite_number(value) or not 0 <= value <= 100:
            raise _invalid_baseline(
                path,
                f"table '{table_name}' null rate for '{column}' is invalid",
            )

    distinct_counts = metrics["distinct_counts"]
    if (
        not isinstance(distinct_counts, dict)
        or set(distinct_counts) != column_set
        or any(
            not _is_nonnegative_integer(value)
            or value > row_count
            for value in distinct_counts.values()
        )
    ):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' distinct_counts is invalid",
        )

    numeric_stats = metrics["numeric_stats"]
    if not isinstance(numeric_stats, dict):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' numeric_stats must be an object",
        )
    if not set(numeric_stats).issubset(column_set):
        raise _invalid_baseline(
            path,
            f"table '{table_name}' numeric_stats contains unknown columns",
        )
    for column, values in numeric_stats.items():
        if (
            not isinstance(values, dict)
            or set(values) != {"min", "max", "avg"}
            or any(not _is_finite_number(value) for value in values.values())
        ):
            raise _invalid_baseline(
                path,
                f"table '{table_name}' numeric stats for '{column}' are invalid",
            )


def _is_nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _invalid_baseline(path: Path, detail: str) -> QualityCheckError:
    return QualityCheckError(
        f"Quality baseline has invalid structure '{path}': {detail}."
    )


def save_baseline(
    project_id: str,
    metrics_by_table: dict,
    baseline_dir: str | Path,
) -> Path:
    """Atomically create or replace an explicitly selected project baseline."""
    path = baseline_path(project_id, baseline_dir)
    _validate_baseline(metrics_by_table, path)

    try:
        serialized = (
            json.dumps(
                metrics_by_table,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, ValueError) as error:
        raise QualityCheckError(
            f"Could not write quality baseline '{path}': {error}"
        ) from error

    temporary_path = None
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        file = os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        )
        descriptor = None
        with file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise QualityCheckError(
            f"Could not write quality baseline '{path}': {error}"
        ) from error

    return path


def detect_anomalies(current: dict, baseline: dict) -> list:
    """Compare current deterministic metrics with a validated baseline."""
    anomalies = []
    table = current["table"]

    current_rows = current["row_count"]
    baseline_rows = baseline["row_count"]
    if baseline_rows > 0:
        row_change_pct = abs(current_rows - baseline_rows) / baseline_rows * 100
        if row_change_pct > 20:
            direction = "dropped" if current_rows < baseline_rows else "spiked"
            anomalies.append(
                {
                    "type": "row_count_anomaly",
                    "severity": "critical" if row_change_pct > 50 else "high",
                    "table": table,
                    "message": (
                        f"Row count {direction} by "
                        f"{round(row_change_pct, 1)}%"
                    ),
                    "detail": (
                        f"Expected ~{baseline_rows} rows, got {current_rows}"
                    ),
                    "impact": "Possible data loss or duplication in pipeline",
                }
            )

    for column, current_null_rate in current["null_rates"].items():
        baseline_null_rate = baseline["null_rates"][column]
        null_increase = current_null_rate - baseline_null_rate
        if null_increase > 10:
            anomalies.append(
                {
                    "type": "null_explosion",
                    "severity": "critical" if null_increase > 30 else "high",
                    "table": table,
                    "message": (
                        f"Null rate on '{column}' jumped by "
                        f"{round(null_increase, 1)}%"
                    ),
                    "detail": (
                        f"Was {baseline_null_rate}% null, "
                        f"now {current_null_rate}% null"
                    ),
                    "impact": (
                        f"Aggregations on '{column}' will return "
                        "wrong results silently"
                    ),
                }
            )

    current_duplicates = current["duplicate_rows"]
    baseline_duplicates = baseline["duplicate_rows"]
    if current_duplicates > baseline_duplicates + 10:
        anomalies.append(
            {
                "type": "duplicate_explosion",
                "severity": "high",
                "table": table,
                "message": (
                    "Duplicate rows jumped from "
                    f"{baseline_duplicates} to {current_duplicates}"
                ),
                "detail": "Possible fan-out from a bad JOIN upstream",
                "impact": "Metrics like SUM(revenue) will be inflated",
            }
        )

    for column, current_count in current["distinct_counts"].items():
        baseline_count = baseline["distinct_counts"][column]
        if baseline_count > 0:
            change = (current_count - baseline_count) / baseline_count * 100
            if change > 200:
                anomalies.append(
                    {
                        "type": "cardinality_explosion",
                        "severity": "medium",
                        "table": table,
                        "message": (
                            f"Distinct values in '{column}' increased by "
                            f"{round(change, 1)}%"
                        ),
                        "detail": (
                            f"Was {baseline_count} distinct values, "
                            f"now {current_count}"
                        ),
                        "impact": (
                            "GROUP BY queries on this column may return "
                            "unexpected granularity"
                        ),
                    }
                )

    return anomalies


def _validate_comparable(current: dict, baseline: dict) -> None:
    current_tables = set(current)
    baseline_tables = set(baseline)
    if current_tables != baseline_tables:
        missing = sorted(current_tables - baseline_tables)
        unexpected = sorted(baseline_tables - current_tables)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unexpected:
            detail.append(f"unexpected {unexpected}")
        raise QualityCheckError(
            "Quality baseline tables do not match the database: "
            + "; ".join(detail)
            + "."
        )

    for table_name in sorted(current):
        current_columns = current[table_name]["columns"]
        baseline_columns = baseline[table_name]["columns"]
        if current_columns != baseline_columns:
            raise QualityCheckError(
                f"Quality baseline columns do not match table '{table_name}'."
            )


def run_quality_check(
    project_id: str,
    db_path: str | Path,
    baseline_dir: str | Path,
    *,
    update_baseline: bool = False,
) -> list:
    """Compare local metrics, or explicitly replace the selected baseline."""
    print(f"\n🔬 Running local data quality checks for '{project_id}'...\n")
    current_metrics = collect_database_metrics(db_path)

    if not current_metrics:
        print("⚠️  No tables found in database.")
        return []

    if update_baseline:
        written_path = save_baseline(
            project_id,
            current_metrics,
            baseline_dir,
        )
        print(f"✅ Quality baseline written: {written_path}")
        return []

    baseline_metrics = load_baseline(project_id, baseline_dir)
    _validate_comparable(current_metrics, baseline_metrics)

    all_anomalies = []
    for table in sorted(current_metrics):
        print(f"  → Checking {table}...")
        anomalies = detect_anomalies(
            current_metrics[table],
            baseline_metrics[table],
        )
        if not anomalies:
            print(f"    ✅ {table} — all metrics within normal range.")
            continue

        print(f"    🚨 {table} — {len(anomalies)} anomaly/anomalies detected!")
        all_anomalies.extend(anomalies)
        print(f"\n{'━' * 55}")
        print(f"  Table: {table}")
        print(f"{'━' * 55}")
        for anomaly in anomalies:
            severity_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "🟢",
            }.get(anomaly["severity"], "⚪")
            print(
                f"\n  {severity_emoji} "
                f"[{anomaly['severity'].upper()}] {anomaly['message']}"
            )
            print(f"     Detail: {anomaly['detail']}")
            print(f"     Impact: {anomaly['impact']}")

    print()
    if all_anomalies:
        print(
            f"🚨 Total anomalies found: {len(all_anomalies)} "
            f"across {len(current_metrics)} tables."
        )
    else:
        print("✅ All tables passed quality checks.")
    print()
    return all_anomalies
