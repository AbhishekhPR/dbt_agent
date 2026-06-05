import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from agent.metrics_store import record_freshness
from agent.slack import send_slack_alert

# Default freshness thresholds in hours
DEFAULT_THRESHOLDS = {
    "stale":    6,   # warn after 6 hours
    "critical": 24   # critical after 24 hours
}


def check_table_freshness(
    db_path: str,
    table: str,
    freshness_col: str = None,
    thresholds: dict = None
) -> dict:
    """
    Checks how recently a table was updated.
    Tries common timestamp column names if none specified.
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Auto-detect freshness column if not specified
    if not freshness_col:
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [row[1].lower() for row in cursor.fetchall()]
        candidates = [
            'updated_at', 'created_at', 'inserted_at',
            'modified_at', 'timestamp', 'event_time', 'date'
        ]
        freshness_col = next(
            (c for c in candidates if c in cols), None
        )

    if not freshness_col:
        conn.close()
        return {
            "table": table,
            "status": "unknown",
            "reason": "No timestamp column found for freshness check",
            "freshness_col": None,
            "last_updated": None,
            "hours_since_update": None
        }

    try:
        cursor.execute(
            f"SELECT MAX({freshness_col}) FROM {table}"
        )
        result = cursor.fetchone()
        last_updated_str = result[0] if result else None

        if not last_updated_str:
            conn.close()
            return {
                "table": table,
                "status": "unknown",
                "reason": f"No data in {freshness_col}",
                "freshness_col": freshness_col,
                "last_updated": None,
                "hours_since_update": None
            }

        # Parse the timestamp
        try:
            last_updated = datetime.fromisoformat(
                str(last_updated_str).replace('Z', '+00:00')
            )
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(
                    tzinfo=timezone.utc
                )
        except ValueError:
            conn.close()
            return {
                "table": table,
                "status": "unknown",
                "reason": f"Could not parse timestamp: {last_updated_str}",
                "freshness_col": freshness_col,
                "last_updated": str(last_updated_str),
                "hours_since_update": None
            }

        now = datetime.now(timezone.utc)
        hours_since = (now - last_updated).total_seconds() / 3600

        # Determine status
        if hours_since >= thresholds["critical"]:
            status = "critical"
        elif hours_since >= thresholds["stale"]:
            status = "stale"
        else:
            status = "ok"

        conn.close()
        return {
            "table": table,
            "status": status,
            "freshness_col": freshness_col,
            "last_updated": last_updated.isoformat(),
            "hours_since_update": round(hours_since, 2),
            "threshold_stale_hours": thresholds["stale"],
            "threshold_critical_hours": thresholds["critical"]
        }

    except Exception as e:
        conn.close()
        return {
            "table": table,
            "status": "error",
            "reason": str(e),
            "freshness_col": freshness_col,
            "last_updated": None,
            "hours_since_update": None
        }


def run_freshness_check(
    project_name: str,
    db_path: str,
    thresholds: dict = None
):
    """
    Checks freshness for all tables in the database.
    Stores results historically. Alerts on stale/critical.
    """
    print(f"\n🕐 Running freshness check for '{project_name}'...\n")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    results = []
    for table in tables:
        result = check_table_freshness(
            db_path, table, thresholds=thresholds
        )
        results.append(result)

        # Store in history
        record_freshness(project_name, table, result)

        # Print result
        status = result["status"]
        hours = result.get("hours_since_update")
        col = result.get("freshness_col", "unknown")

        status_display = {
            "ok":       "✅ Fresh",
            "stale":    "🟡 Stale",
            "critical": "🔴 Critical",
            "unknown":  "⚪ Unknown",
            "error":    "⚠️  Error"
        }.get(status, status)

        print(f"  {status_display:<18} {table}")
        if hours is not None:
            print(
                f"               last updated "
                f"{hours:.1f}h ago via '{col}'"
            )
        elif result.get("reason"):
            print(f"               {result['reason']}")
        print()

        # Alert on stale or critical
        if status in ("stale", "critical"):
            diagnosis = {
                "root_cause": (
                    f"Table '{table}' has not been updated in "
                    f"{hours:.1f} hours"
                ),
                "affected_file": f"Table: {table}",
                "affected_line": f"Column: {col}",
                "explanation": (
                    f"Expected update within "
                    f"{thresholds['stale'] if thresholds else 6}h. "
                    f"Last seen: {result.get('last_updated', 'unknown')}"
                ),
                "suggested_fix": (
                    "Check upstream ETL job status. "
                    "Verify data source is delivering on schedule."
                ),
                "severity": (
                    "critical" if status == "critical" else "medium"
                ),
                "data_loss_risk": status == "critical"
            }
            send_slack_alert(f"FRESHNESS — {table}", diagnosis)

    # Summary
    stale_count = sum(
        1 for r in results
        if r["status"] in ("stale", "critical")
    )
    if stale_count:
        print(
            f"  🚨 {stale_count} table(s) are stale "
            f"or critical — Slack alerted.\n"
        )
    else:
        print("  ✅ All tables are fresh.\n")

    return results