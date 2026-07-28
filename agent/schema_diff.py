import json
import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from agent.slack import send_slack_alert

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SCHEMA_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "schema_snapshots"
SCHEMA_SNAPSHOT_PATH.mkdir(exist_ok=True)


def get_sqlite_schema(db_path: str) -> dict:
    """
    Pulls current schema from a SQLite database.
    Returns dict of {table_name: [{name, type}]}
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    schema = {}
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [
            {"name": row[1], "type": row[2]}
            for row in cursor.fetchall()
        ]
        schema[table] = columns

    conn.close()
    return schema


def load_snapshot(project_name: str) -> dict:
    """Load last saved schema snapshot"""
    snapshot_file = SCHEMA_SNAPSHOT_PATH / f"{project_name}.json"
    if not snapshot_file.exists():
        return {}
    with open(snapshot_file) as f:
        return json.load(f)


def save_snapshot(project_name: str, schema: dict):
    """Save current schema as new snapshot"""
    snapshot_file = SCHEMA_SNAPSHOT_PATH / f"{project_name}.json"
    with open(snapshot_file, "w") as f:
        json.dump(schema, f, indent=2)
    print(f"✅ Schema snapshot saved for {project_name}")


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


def run_schema_diff(project_name: str, db_path: str):
    """
    Main entry point.
    Pulls current schema, diffs against snapshot, alerts on changes.
    """
    print(f"\n🔍 Checking schema for '{project_name}'...\n")

    current_schema = get_sqlite_schema(db_path)
    previous_schema = load_snapshot(project_name)

    if not previous_schema:
        print("📸 No previous snapshot found. Saving current schema as baseline.")
        save_snapshot(project_name, current_schema)
        print("✅ Baseline saved. Run again after a schema change to detect diffs.\n")
        return

    changes = diff_schemas(previous_schema, current_schema)

    if not changes:
        print("✅ No schema changes detected. All clear.\n")
        save_snapshot(project_name, current_schema)
        return

    # Print and alert each change
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

        # Send Slack alert for critical and high changes
        if change["severity"] in ("critical", "high"):
            diagnosis = {
                "root_cause": change["message"],
                "affected_file": f"Table: {change['table']}",
                "affected_line": change["type"],
                "explanation": change["risk"],
                "suggested_fix": "Review all dbt models referencing this table before next run.",
                "severity": change["severity"],
                "data_loss_risk": change["severity"] == "critical"
            }
            send_slack_alert(f"SCHEMA CHANGE — {change['table']}", diagnosis)

    # Save new snapshot only after alerting
    save_snapshot(project_name, current_schema)
    