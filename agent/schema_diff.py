import json
import os
import sqlite3
import tempfile
from pathlib import Path


_INVALID_SNAPSHOT_NAME_CHARACTERS = frozenset('<>:"/\\|?*\0')
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


class SchemaDiffError(Exception):
    """Raised when schema comparison cannot safely proceed."""


def get_sqlite_schema(db_path: str | Path) -> dict:
    """
    Pull the current schema from an existing read-only SQLite database.
    Returns dict of {table_name: [{name, type}]}
    """
    database_path = Path(db_path).expanduser().resolve()
    try:
        if not database_path.exists():
            raise SchemaDiffError(
                f"SQLite database not found: {database_path}"
            )
        if not database_path.is_file():
            raise SchemaDiffError(
                f"SQLite database is not a file: {database_path}"
            )
        if database_path.stat().st_size == 0:
            raise SchemaDiffError(
                f"SQLite database is empty: {database_path}"
            )
    except OSError as error:
        raise SchemaDiffError(
            f"Could not access SQLite database '{database_path}': {error}"
        ) from error

    connection = None
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
        )
        cursor = connection.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        schema = {}
        for table in tables:
            quoted_table = table.replace('"', '""')
            cursor.execute(f'PRAGMA table_info("{quoted_table}")')
            schema[table] = [
                {"name": row[1], "type": row[2]}
                for row in cursor.fetchall()
            ]
        return schema
    except (OSError, sqlite3.Error) as error:
        raise SchemaDiffError(
            f"Could not read SQLite database '{database_path}': {error}"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def snapshot_path(project_name: str, snapshot_dir: str | Path) -> Path:
    """Return the selected project's snapshot path inside snapshot_dir."""
    project_text = str(project_name)
    reserved_stem = project_text.split(".", 1)[0].upper()
    if (
        not project_text
        or ".." in project_text
        or any(
            character in _INVALID_SNAPSHOT_NAME_CHARACTERS
            for character in project_text
        )
        or project_text.endswith((".", " "))
        or reserved_stem in _WINDOWS_RESERVED_NAMES
    ):
        raise SchemaDiffError(
            "Project name must be a single safe snapshot filename component."
        )

    directory = Path(snapshot_dir).expanduser().resolve()
    path = (directory / f"{project_text}.json").resolve()
    try:
        path.relative_to(directory)
    except ValueError as error:
        raise SchemaDiffError(
            "Snapshot path must remain inside --snapshot-dir."
        ) from error
    return path


def load_snapshot(project_name: str, snapshot_dir: str | Path) -> dict:
    """Load an existing schema snapshot without changing it."""
    path = snapshot_path(project_name, snapshot_dir)
    try:
        if not path.exists():
            raise SchemaDiffError(
                "No schema snapshot exists. "
                "Run again with --update-snapshot to create one."
            )
        if not path.is_file():
            raise SchemaDiffError(f"Schema snapshot is not a file: {path}")
    except OSError as error:
        raise SchemaDiffError(
            f"Could not access schema snapshot '{path}': {error}"
        ) from error

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SchemaDiffError(
            f"Schema snapshot is not valid JSON: {path}"
        ) from error
    except OSError as error:
        raise SchemaDiffError(
            f"Could not read schema snapshot '{path}': {error}"
        ) from error

    if not isinstance(snapshot, dict):
        raise SchemaDiffError(
            f"Schema snapshot must contain a JSON object: {path}"
        )
    return _validate_snapshot(snapshot, path)


def _validate_snapshot(snapshot: dict, path: Path) -> dict:
    for table_name, columns in snapshot.items():
        if not isinstance(table_name, str) or not table_name:
            raise _invalid_snapshot(path, "table names must be non-empty strings")
        if not isinstance(columns, list):
            raise _invalid_snapshot(
                path,
                f"table '{table_name}' must contain a list of columns",
            )

        column_names = set()
        for index, column in enumerate(columns):
            if not isinstance(column, dict):
                raise _invalid_snapshot(
                    path,
                    f"column {index} in table '{table_name}' must be an object",
                )

            name = column.get("name")
            column_type = column.get("type")
            if not isinstance(name, str) or not name:
                raise _invalid_snapshot(
                    path,
                    f"column {index} in table '{table_name}' "
                    "must have a non-empty string name",
                )
            if not isinstance(column_type, str):
                raise _invalid_snapshot(
                    path,
                    f"column '{name}' in table '{table_name}' "
                    "must have a string type",
                )

            normalized_name = name.casefold()
            if normalized_name in column_names:
                raise _invalid_snapshot(
                    path,
                    f"table '{table_name}' contains duplicate column '{name}'",
                )
            column_names.add(normalized_name)

    return snapshot


def _invalid_snapshot(path: Path, detail: str) -> SchemaDiffError:
    return SchemaDiffError(
        f"Schema snapshot has invalid structure '{path}': {detail}."
    )


def save_snapshot(
    project_name: str,
    schema: dict,
    snapshot_dir: str | Path,
) -> Path:
    """Atomically create or replace the explicitly selected snapshot."""
    path = snapshot_path(project_name, snapshot_dir)
    directory = path.parent
    temporary_path = None
    descriptor = None

    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        temporary_file = os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        )
        descriptor = None
        with temporary_file as file:
            json.dump(schema, file, indent=2, sort_keys=True)
            file.write("\n")
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
        raise SchemaDiffError(
            f"Could not write schema snapshot '{path}': {error}"
        ) from error

    return path


def diff_schemas(old: dict, new: dict) -> list:
    """
    Compares two schemas and returns a list of changes.
    Each change is a dict describing what happened.
    """
    changes = []

    for table in new:
        if table not in old:
            changes.append({
                "type": "new_table",
                "table": table,
                "severity": "low",
                "message": f"New table detected: '{table}'",
                "risk": "No immediate risk but downstream models may need updating."
            })
            continue

        old_cols = {c["name"]: c["type"] for c in old[table]}
        new_cols = {c["name"]: c["type"] for c in new[table]}

        # Dropped columns — most dangerous
        for col in old_cols:
            if col not in new_cols:
                changes.append({
                    "type": "column_dropped",
                    "table": table,
                    "column": col,
                    "severity": "critical",
                    "message": f"Column '{col}' dropped from '{table}'",
                    "risk": f"Any model referencing '{col}' will crash. Immediate action required."
                })

        # Renamed columns (dropped + added in same table)
        added = [c for c in new_cols if c not in old_cols]
        dropped = [c for c in old_cols if c not in new_cols]
        if added and dropped:
            changes.append({
                "type": "column_renamed",
                "table": table,
                "old_column": dropped,
                "new_column": added,
                "severity": "critical",
                "message": f"Possible rename in '{table}': {dropped} → {added}",
                "risk": "Models using old column names will silently return 0 rows or crash."
            })

        # Type changes — silent killers
        for col in old_cols:
            if col in new_cols and old_cols[col] != new_cols[col]:
                changes.append({
                    "type": "type_changed",
                    "table": table,
                    "column": col,
                    "old_type": old_cols[col],
                    "new_type": new_cols[col],
                    "severity": "high",
                    "message": f"Type changed on '{table}.{col}': {old_cols[col]} → {new_cols[col]}",
                    "risk": "Aggregations or filters on this column may return nulls or wrong results silently."
                })

        # New nullable columns
        for col in added:
            changes.append({
                "type": "column_added",
                "table": table,
                "column": col,
                "severity": "low",
                "message": f"New column '{col}' added to '{table}'",
                "risk": "Check if downstream models need to handle nulls for this column."
            })

    # Dropped tables
    for table in old:
        if table not in new:
            changes.append({
                "type": "table_dropped",
                "table": table,
                "severity": "critical",
                "message": f"Table '{table}' no longer exists",
                "risk": "All models referencing this table will crash immediately."
            })

    return changes


def run_schema_diff(
    project_name: str,
    db_path: str | Path,
    snapshot_dir: str | Path,
    *,
    update_snapshot: bool = False,
) -> list:
    """
    Compare against an existing snapshot, or explicitly replace that snapshot.
    """
    print(f"\n🔍 Checking schema for '{project_name}'...\n")

    current_schema = get_sqlite_schema(db_path)
    if update_snapshot:
        written_path = save_snapshot(
            project_name,
            current_schema,
            snapshot_dir,
        )
        print(f"✅ Schema snapshot written: {written_path}")
        return []

    previous_schema = load_snapshot(project_name, snapshot_dir)

    changes = diff_schemas(previous_schema, current_schema)

    if not changes:
        print("✅ No schema changes detected. All clear.\n")
        return []

    print(f"🚨 Detected {len(changes)} schema change(s):\n")
    for change in changes:
        severity_emoji = {
            "critical": "🔴",
            "high":     "🟠",
            "medium":   "🟡",
            "low":      "🟢"
        }.get(change["severity"], "⚪")

        print(f"{severity_emoji} [{change['severity'].upper()}] {change['message']}")
        print(f"   Risk: {change['risk']}\n")

    return changes
