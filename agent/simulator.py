import json
import shutil
import sqlite3
from pathlib import Path

from agent import quality_checker
from agent.quality_checker import get_table_metrics, quote_identifier, run_quality_check


SUPPORTED_ANOMALIES = {
    "row_count_drop",
    "row_count_spike",
    "null_explosion",
    "cardinality_explosion",
    "duplicate_explosion",
}


def run_simulation(
    project_name: str,
    db_path: str,
    table: str,
    anomaly_type: str,
    restore_after: bool = True,
    sync_baseline: bool = True
) -> None:
    if anomaly_type not in SUPPORTED_ANOMALIES:
        supported = ", ".join(sorted(SUPPORTED_ANOMALIES))
        raise ValueError(f"Unsupported simulation type '{anomaly_type}'. Supported: {supported}")

    db_file = Path(db_path)
    baseline_file = quality_checker.BASELINE_PATH / f"{table}.json"
    db_backup = Path(str(db_file) + ".sim_backup")
    baseline_backup = baseline_file.with_suffix(baseline_file.suffix + ".sim_backup")

    print("Running Relium simulation")
    print()
    print(f"Project: {project_name}")
    print(f"Table: {table}")
    print(f"Simulation: {anomaly_type}")
    print(f"Restore after run: {'yes' if restore_after else 'no'}")
    print()

    pre_simulation_metrics = get_table_metrics(db_path, table)
    if sync_baseline:
        ensure_clean_baseline(project_name, db_path, table)
        print("Baseline synced to current table metrics.")

    pre_simulation_baseline = _load_baseline(baseline_file)
    _backup_file(db_file, db_backup)
    _backup_file(baseline_file, baseline_backup)
    print("Backup created.")

    try:
        _apply_simulation(db_path, table, anomaly_type, baseline_file)
        print("Simulation applied.")
        print("Running quality check...")
        print()

        run_quality_check(project_name, db_path)
    finally:
        if restore_after:
            _restore_file(db_backup, db_file)
            _restore_file(baseline_backup, baseline_file)
            print("Restored database and baseline.")
            if _verify_restore(db_path, table, baseline_file, pre_simulation_metrics, pre_simulation_baseline):
                print("Restore verification passed.")
            else:
                print("Restore verification failed.")
        else:
            print("Backups retained for manual restore.")

    print("Simulation complete.")


def ensure_clean_baseline(project_name: str, db_path: str, table: str):
    metrics = get_table_metrics(db_path, table)
    quality_checker.save_baseline(table, metrics)


def _backup_file(source: Path, destination: Path):
    if not source.exists():
        raise FileNotFoundError(f"Cannot simulate because '{source}' does not exist")
    shutil.copy2(source, destination)


def _restore_file(backup: Path, destination: Path):
    shutil.copy2(backup, destination)
    backup.unlink()


def _verify_restore(
    db_path: str,
    table: str,
    baseline_file: Path,
    expected_metrics: dict,
    expected_baseline: dict,
) -> bool:
    restored_metrics = get_table_metrics(db_path, table)
    restored_baseline = _load_baseline(baseline_file)
    return (
        restored_metrics.get("row_count") == expected_metrics.get("row_count")
        and restored_metrics.get("duplicate_rows") == expected_metrics.get("duplicate_rows")
        and restored_baseline.get("duplicate_rows") == expected_baseline.get("duplicate_rows")
    )


def _apply_simulation(db_path: str, table: str, anomaly_type: str, baseline_file: Path):
    if anomaly_type == "row_count_drop":
        _simulate_row_count_drop(db_path, table, baseline_file)
    elif anomaly_type == "row_count_spike":
        _simulate_row_count_spike(db_path, table, baseline_file)
    elif anomaly_type == "null_explosion":
        _simulate_null_explosion(db_path, table, baseline_file)
    elif anomaly_type == "cardinality_explosion":
        _simulate_cardinality_explosion(db_path, table, baseline_file)
    elif anomaly_type == "duplicate_explosion":
        _simulate_duplicate_explosion(db_path, table, baseline_file)


def _simulate_row_count_drop(db_path: str, table: str, baseline_file: Path):
    baseline = get_table_metrics(db_path, table)
    current_rows = _row_count(db_path, table)
    baseline["row_count"] = max(200, current_rows * 25)
    _save_baseline(baseline_file, baseline)


def _simulate_row_count_spike(db_path: str, table: str, baseline_file: Path):
    baseline = get_table_metrics(db_path, table)
    baseline["row_count"] = 1
    _save_baseline(baseline_file, baseline)


def _simulate_null_explosion(db_path: str, table: str, baseline_file: Path):
    columns = _table_columns(db_path, table)
    column = _pick_null_column(columns)
    if not column:
        raise ValueError(f"No suitable column found for null_explosion on '{table}'")

    baseline = get_table_metrics(db_path, table)
    baseline.setdefault("null_rates", {})[column["name"]] = 0.0
    _save_baseline(baseline_file, baseline)

    quoted_table = quote_identifier(table)
    quoted_column = quote_identifier(column["name"])
    conn = sqlite3.connect(db_path)
    try:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        limit = max(1, row_count // 2)
        conn.execute(
            f"""
            UPDATE {quoted_table}
            SET {quoted_column} = NULL
            WHERE rowid IN (
                SELECT rowid FROM {quoted_table}
                ORDER BY rowid
                LIMIT ?
            )
            """,
            (limit,),
        )
        conn.commit()
    finally:
        conn.close()


def _simulate_cardinality_explosion(db_path: str, table: str, baseline_file: Path):
    columns = _table_columns(db_path, table)
    column = _pick_cardinality_column(columns)
    if not column:
        raise ValueError(f"No suitable column found for cardinality_explosion on '{table}'")

    baseline = get_table_metrics(db_path, table)
    baseline.setdefault("distinct_counts", {})[column["name"]] = 1
    _save_baseline(baseline_file, baseline)

    quoted_table = quote_identifier(table)
    quoted_column = quote_identifier(column["name"])
    conn = sqlite3.connect(db_path)
    try:
        rowids = [row[0] for row in conn.execute(f"SELECT rowid FROM {quoted_table} ORDER BY rowid")]
        conn.executemany(
            f"UPDATE {quoted_table} SET {quoted_column} = ? WHERE rowid = ?",
            [(f"{column['name']}_{idx}", rowid) for idx, rowid in enumerate(rowids, 1)],
        )
        conn.commit()
    finally:
        conn.close()


def _simulate_duplicate_explosion(db_path: str, table: str, baseline_file: Path):
    quoted_table = quote_identifier(table)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"INSERT INTO {quoted_table} SELECT * FROM {quoted_table} LIMIT 5")
        conn.commit()
    finally:
        conn.close()

    current_metrics = get_table_metrics(db_path, table)
    baseline = _load_baseline(baseline_file)
    baseline["row_count"] = current_metrics.get("row_count", 0)
    baseline["duplicate_rows"] = 0
    baseline["duplicate_rate"] = 0
    _save_baseline(baseline_file, baseline)


def _load_baseline(path: Path) -> dict:
    return json.loads(path.read_text())


def _save_baseline(path: Path, baseline: dict):
    path.write_text(json.dumps(baseline, indent=2))


def _row_count(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
    finally:
        conn.close()


def _table_columns(db_path: str, table: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
        return [
            {
                "name": row[1],
                "type": (row[2] or "").upper(),
                "pk": bool(row[5]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _pick_null_column(columns: list) -> dict:
    by_name = {column["name"]: column for column in columns}
    for name in ("order_status", "status", "email"):
        if name in by_name:
            return by_name[name]
    for column in columns:
        name = column["name"].lower()
        if not column["pk"] and not name.endswith("_id") and name != "id":
            return column
    return {}


def _pick_cardinality_column(columns: list) -> dict:
    by_name = {column["name"]: column for column in columns}
    for name in ("order_status", "status"):
        if name in by_name:
            return by_name[name]
    for column in columns:
        if "TEXT" in column["type"] or "CHAR" in column["type"] or "VARCHAR" in column["type"]:
            return column
    return {}
