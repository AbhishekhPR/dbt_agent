import json
import re
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from agent.groq_client import call_llm_json
from agent.incident_reporter import create_incident_report
from agent.metrics_store import record_table_metrics
from agent.root_cause_engine import analyze_root_cause
from agent.slack import send_slack_alert

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

BASELINE_PATH = Path(__file__).resolve().parent.parent / "quality_baselines"
BASELINE_PATH.mkdir(exist_ok=True)
DEFAULT_FRESHNESS_THRESHOLD_MINUTES = 24 * 60

SYSTEM_PROMPT = """
You are a senior data quality engineer. You analyze data pipeline 
metrics and identify anomalies that indicate silent data corruption.
You always respond with valid JSON only. No explanation outside JSON.
"""


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier without treating it as SQL."""
    return '"' + identifier.replace('"', '""') + '"'


def _singular_table_name(table_name: str) -> str:
    name = table_name
    if name.startswith("raw_"):
        name = name[4:]
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name


def infer_duplicate_key(columns: list, table_name: str) -> list:
    """Choose business-key columns for duplicate detection when available."""
    if "order_id" in columns:
        return ["order_id"]
    if "id" in columns:
        return ["id"]

    singular_id = f"{_singular_table_name(table_name)}_id"
    if singular_id in columns:
        return [singular_id]

    return []


def infer_freshness_column(columns: list) -> str | None:
    preferred = [
        "updated_at",
        "created_at",
        "ingested_at",
        "loaded_at",
        "event_time",
        "timestamp",
    ]
    for name in preferred:
        if name in columns:
            return name
    for col in columns:
        if col.endswith("_at"):
            return col
    for col in columns:
        if "date" in col.lower():
            return col
    return None


def _parse_sqlite_datetime(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _schema_from_table_info(table_info: list) -> list:
    return [
        {
            "name": row[1],
            "data_type": row[2] or "",
            "nullable": not bool(row[3]),
            "primary_key": bool(row[5]),
        }
        for row in table_info
    ]


def _schema_hash(schema: list) -> str:
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _duplicate_count_sql(table_name: str, columns: list) -> str:
    table = quote_identifier(table_name)
    distinct_columns = ", ".join(quote_identifier(col) for col in columns)
    return f"""
        SELECT
            (SELECT COUNT(*) FROM {table})
            -
            (SELECT COUNT(*) FROM (
                SELECT DISTINCT {distinct_columns}
                FROM {table}
            ))
    """


def get_table_metrics(db_path: str, table_name: str) -> dict:
    """
    Pulls quality metrics from a single table.
    Row count, null rates, duplicate rates, min/max values.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    metrics = {"table": table_name}
    quoted_table = quote_identifier(table_name)

    try:
        # Row count
        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
        metrics["row_count"] = cursor.fetchone()[0]

        # Get columns
        cursor.execute(f"PRAGMA table_info({quoted_table})")
        table_info = cursor.fetchall()
        columns = [row[1] for row in table_info]
        metrics["columns"] = columns
        schema = _schema_from_table_info(table_info)
        metrics["schema"] = schema
        metrics["schema_hash"] = _schema_hash(schema)

        # Null rates per column
        null_rates = {}
        for col in columns:
            quoted_col = quote_identifier(col)
            cursor.execute(f"""
                SELECT 
                    ROUND(100.0 * SUM(CASE WHEN {quoted_col} IS NULL THEN 1 ELSE 0 END) 
                    / COUNT(*), 2)
                FROM {quoted_table}
            """)
            null_rates[col] = cursor.fetchone()[0] or 0.0
        metrics["null_rates"] = null_rates

        freshness_column = infer_freshness_column(columns)
        metrics["freshness_column"] = freshness_column
        metrics["last_updated"] = None
        metrics["freshness_minutes"] = None

        if freshness_column:
            try:
                quoted_freshness_col = quote_identifier(freshness_column)
                cursor.execute(f"SELECT MAX({quoted_freshness_col}) FROM {quoted_table}")
                last_updated = cursor.fetchone()[0]
                metrics["last_updated"] = last_updated
                if last_updated:
                    parsed = _parse_sqlite_datetime(last_updated)
                    now = datetime.utcnow()
                    if parsed.tzinfo is not None:
                        parsed = parsed.replace(tzinfo=None)
                    metrics["freshness_minutes"] = round((now - parsed).total_seconds() / 60, 2)
            except Exception as exc:
                metrics["freshness_error"] = str(exc)

        # Duplicate rate. Store aggregate counts and method only, never row values.
        duplicate_key = infer_duplicate_key(columns, table_name)
        if duplicate_key:
            duplicate_method = f"key:{','.join(duplicate_key)}"
        else:
            pk_columns = [row[1] for row in table_info if row[5]]
            duplicate_key = pk_columns or columns
            duplicate_method = f"key:{','.join(duplicate_key)}" if pk_columns else "full_row"

        if duplicate_key:
            cursor.execute(_duplicate_count_sql(table_name, duplicate_key))
            duplicate_rows = cursor.fetchone()[0] or 0
        else:
            duplicate_rows = 0

        row_count = metrics["row_count"] or 0
        duplicate_rate = round(100.0 * duplicate_rows / row_count, 2) if row_count else 0.0
        metrics["duplicate_rows"] = duplicate_rows
        metrics["duplicate_rate"] = duplicate_rate
        metrics["duplicate_check_method"] = duplicate_method
        metrics["duplicate_key_columns"] = duplicate_key

        # Numeric column stats
        numeric_stats = {}
        for col in columns:
            try:
                quoted_col = quote_identifier(col)
                cursor.execute(f"""
                    SELECT 
                        MIN(CAST({quoted_col} AS REAL)),
                        MAX(CAST({quoted_col} AS REAL)),
                        AVG(CAST({quoted_col} AS REAL))
                    FROM {quoted_table}
                    WHERE {quoted_col} IS NOT NULL
                """)
                row = cursor.fetchone()
                if row and row[0] is not None:
                    numeric_stats[col] = {
                        "min": round(row[0], 2),
                        "max": round(row[1], 2),
                        "avg": round(row[2], 2)
                    }
            except Exception:
                pass
        metrics["numeric_stats"] = numeric_stats

        # Distinct value counts per column
        distinct_counts = {}
        for col in columns:
            quoted_col = quote_identifier(col)
            cursor.execute(f"SELECT COUNT(DISTINCT {quoted_col}) FROM {quoted_table}")
            distinct_counts[col] = cursor.fetchone()[0]
        metrics["distinct_counts"] = distinct_counts

    except Exception as e:
        metrics["error"] = str(e)
    finally:
        conn.close()

    return metrics


def load_baseline(table_name: str) -> dict:
    """Load saved baseline metrics for a table"""
    baseline_file = BASELINE_PATH / f"{table_name}.json"
    if not baseline_file.exists():
        return {}
    with open(baseline_file) as f:
        return json.load(f)


def save_baseline(table_name: str, metrics: dict):
    """Save current metrics as new baseline"""
    baseline_file = BASELINE_PATH / f"{table_name}.json"
    with open(baseline_file, "w") as f:
        json.dump(metrics, f, indent=2)


def detect_anomalies(current: dict, baseline: dict) -> list:
    """
    Compares current metrics against baseline.
    Returns list of anomalies found.
    """
    anomalies = []
    table = current.get("table", "unknown")

    if not baseline:
        return anomalies

    # Schema drift — source/table structure changed since the baseline
    current_schema = current.get("schema") or []
    baseline_schema = baseline.get("schema") or []
    if current_schema and baseline_schema:
        anomalies.extend(_detect_schema_drift(table, current_schema, baseline_schema))

    # Row count anomaly — more than 20% change is suspicious
    current_rows = current.get("row_count", 0)
    baseline_rows = baseline.get("row_count", 0)

    if baseline_rows > 0:
        row_change_pct = abs(current_rows - baseline_rows) / baseline_rows * 100
        if row_change_pct > 20:
            direction = "dropped" if current_rows < baseline_rows else "spiked"
            anomalies.append({
                "type": "row_count_anomaly",
                "severity": "critical" if row_change_pct > 50 else "high",
                "table": table,
                "message": f"Row count {direction} by {round(row_change_pct, 1)}%",
                "detail": f"Expected ~{baseline_rows} rows, got {current_rows}",
                "impact": "Possible data loss or duplication in pipeline"
            })

    # Null rate explosion — any column with >10% more nulls than baseline
    current_nulls = current.get("null_rates", {})
    baseline_nulls = baseline.get("null_rates", {})

    for col, current_null_rate in current_nulls.items():
        baseline_null_rate = baseline_nulls.get(col, 0)
        null_increase = current_null_rate - baseline_null_rate

        if null_increase > 10:
            anomalies.append({
                "type": "null_explosion",
                "severity": "critical" if null_increase > 30 else "high",
                "table": table,
                "message": f"Null rate on '{col}' jumped by {round(null_increase, 1)}%",
                "detail": f"Was {baseline_null_rate}% null, now {current_null_rate}% null",
                "impact": f"Aggregations on '{col}' will return wrong results silently"
            })

    # Duplicate explosion
    current_dupes = current.get("duplicate_rows", 0)
    baseline_dupes = baseline.get("duplicate_rows", 0)
    duplicate_increase = current_dupes - baseline_dupes
    current_duplicate_rate = current.get("duplicate_rate", 0)

    if duplicate_increase > 0 and (
        duplicate_increase >= 10
        or current_duplicate_rate >= 5.0
    ):
        method = current.get("duplicate_check_method", "unknown")
        if method.startswith("key:"):
            detail_method = f"key: {method.split(':', 1)[1]}"
        else:
            detail_method = method
        anomalies.append({
            "type": "duplicate_explosion",
            "severity": "critical" if current_duplicate_rate >= 20.0 or duplicate_increase >= 100 else "high",
            "table": table,
            "message": f"Duplicate rows increased from {baseline_dupes} to {current_dupes}",
            "detail": f"Duplicate rate is {current_duplicate_rate}% using {detail_method}",
            "impact": "Duplicate records may inflate COUNT, SUM, revenue, and downstream metrics."
        })

    # Freshness anomaly — latest update timestamp is outside expected window
    freshness_minutes = current.get("freshness_minutes")
    freshness_threshold = baseline.get(
        "freshness_threshold_minutes",
        DEFAULT_FRESHNESS_THRESHOLD_MINUTES,
    )
    if freshness_minutes is not None and freshness_minutes > freshness_threshold:
        stale_hours = round(freshness_minutes / 60, 1)
        anomalies.append({
            "type": "freshness_anomaly",
            "severity": "critical" if freshness_minutes > 48 * 60 else "high",
            "table": table,
            "message": f"Table is stale by {stale_hours} hours",
            "detail": (
                f"Latest {current.get('freshness_column')} value is "
                f"{current.get('last_updated')}"
            ),
            "impact": "Downstream models may be using outdated data"
        })

    # Cardinality explosion — distinct values suddenly way higher
    current_distinct = current.get("distinct_counts", {})
    baseline_distinct = baseline.get("distinct_counts", {})

    for col, current_count in current_distinct.items():
        baseline_count = baseline_distinct.get(col, 0)
        if baseline_count > 0:
            cardinality_change = (current_count - baseline_count) / baseline_count * 100
            if cardinality_change > 200:
                anomalies.append({
                    "type": "cardinality_explosion",
                    "severity": "medium",
                    "table": table,
                    "message": f"Distinct values in '{col}' increased by {round(cardinality_change, 1)}%",
                    "detail": f"Was {baseline_count} distinct values, now {current_count}",
                    "impact": "GROUP BY queries on this column may return unexpected granularity"
                })

    return anomalies


def _detect_schema_drift(table: str, current_schema: list, baseline_schema: list) -> list:
    anomalies = []
    current_by_name = {col["name"]: col for col in current_schema}
    baseline_by_name = {col["name"]: col for col in baseline_schema}

    for column_name, baseline_col in baseline_by_name.items():
        if column_name not in current_by_name:
            anomalies.append({
                "type": "schema_drift",
                "severity": "critical",
                "table": table,
                "message": f"Schema drift detected: column '{column_name}' was removed",
                "detail": "Column existed in baseline but is missing in current schema",
                "impact": "Downstream models referencing this column may fail or produce incorrect results",
                "schema_change": {
                    "change_type": "removed_column",
                    "column": column_name
                }
            })

    for column_name in current_by_name:
        if column_name not in baseline_by_name:
            anomalies.append({
                "type": "schema_drift",
                "severity": "medium",
                "table": table,
                "message": f"Schema drift detected: column '{column_name}' was added",
                "detail": "Column did not exist in baseline but exists in current schema",
                "impact": "Downstream SELECT * models may change shape unexpectedly",
                "schema_change": {
                    "change_type": "added_column",
                    "column": column_name
                }
            })

    for column_name, baseline_col in baseline_by_name.items():
        current_col = current_by_name.get(column_name)
        if not current_col:
            continue

        old_type = baseline_col.get("data_type") or ""
        new_type = current_col.get("data_type") or ""
        if old_type != new_type:
            anomalies.append({
                "type": "schema_drift",
                "severity": "high",
                "table": table,
                "message": (
                    f"Schema drift detected: column '{column_name}' changed type "
                    f"from {old_type} to {new_type}"
                ),
                "detail": "Column data type changed between baseline and current schema",
                "impact": "Aggregations, joins, and casts may behave incorrectly",
                "schema_change": {
                    "change_type": "type_change",
                    "column": column_name,
                    "old_type": old_type,
                    "new_type": new_type
                }
            })

        old_nullable = baseline_col.get("nullable")
        new_nullable = current_col.get("nullable")
        if old_nullable is not None and new_nullable is not None and old_nullable != new_nullable:
            direction = "nullable" if new_nullable else "not nullable"
            anomalies.append({
                "type": "schema_drift",
                "severity": "high" if new_nullable else "medium",
                "table": table,
                "message": f"Schema drift detected: column '{column_name}' is now {direction}",
                "detail": "Column nullability changed between baseline and current schema",
                "impact": "Downstream assumptions about required values may be incorrect",
                "schema_change": {
                    "change_type": "nullable_change",
                    "column": column_name,
                    "old_nullable": old_nullable,
                    "new_nullable": new_nullable
                }
            })

    return anomalies


def ask_claude_about_anomalies(anomalies: list, metrics: dict) -> dict:
    """
    Sends anomalies to Claude for deeper analysis and recommendations.
    """
    if not anomalies:
        return {}

    prompt = f"""
A data quality check found these anomalies in a production table:

## ANOMALIES DETECTED
{json.dumps(anomalies, indent=2)}

## CURRENT TABLE METRICS
{json.dumps(metrics, indent=2)}

Analyze these anomalies and return a JSON object with:
{{
  "root_cause_hypothesis": "most likely cause of these anomalies",
  "immediate_actions": ["action 1", "action 2", "action 3"],
  "queries_to_investigate": ["SQL query 1 to run", "SQL query 2 to run"],
  "should_halt_pipeline": true or false,
  "confidence": "high | medium | low"
}}
"""
    return call_llm_json(prompt=prompt, system=SYSTEM_PROMPT)


def print_root_cause_summary(rca_report: dict):
    """Prints a compact local RCA summary for a detected anomaly."""
    print("\n  🧠 Root Cause Analysis")

    causes = rca_report.get("likely_causes", [])
    if causes:
        print("\n     Likely Causes:")
        for idx, cause in enumerate(causes[:4], 1):
            print(f"     {idx}. {cause.get('cause', 'unknown')}")
            print(f"        Confidence: {cause.get('confidence', 0):.2f}")
            print(f"        Reason: {cause.get('reason', 'N/A')}")
    else:
        print("\n     Likely Causes: No deterministic cause identified")

    affected = rca_report.get("affected_models", [])
    print("\n     Affected Models:")
    if affected:
        for model in affected:
            print(f"     - {model}")
    else:
        print("     - None found")

    actions = rca_report.get("recommended_actions", [])
    if actions:
        print("\n     Recommended Actions:")
        for action in actions:
            print(f"     - {action}")


def run_quality_check(project_name: str, db_path: str):
    """
    Main entry point.
    Runs quality checks on all tables, compares to baseline,
    fires alerts on anomalies.
    """
    print(f"\n🔬 Running data quality checks for '{project_name}'...\n")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not tables:
        print("⚠️  No tables found in database.")
        return

    all_anomalies = []

    for table in tables:
        print(f"  → Checking {table}...")
        current_metrics = get_table_metrics(db_path, table)
        baseline_metrics = load_baseline(table)

        if not baseline_metrics:
            print(f"    📸 No baseline for '{table}' — saving current as baseline.")
            save_baseline(table, current_metrics)
            record_table_metrics(project_name, table, current_metrics)
            continue

        anomalies = detect_anomalies(current_metrics, baseline_metrics)

        if not anomalies:
            print(f"    ✅ {table} — all metrics within normal range.")
            save_baseline(table, current_metrics)
            record_table_metrics(project_name, table, current_metrics)
            continue

        print(f"    🚨 {table} — {len(anomalies)} anomaly/anomalies detected!")
        all_anomalies.extend(anomalies)

        # Local metadata-only RCA. No LLM call.
        for anomaly in anomalies:
            rca_report = analyze_root_cause({
                **anomaly,
                "type": anomaly.get("type"),
                "table": table,
                "project_path": project_name,
                "message": anomaly.get("message", "")
            })
            anomaly["root_cause_analysis"] = rca_report
            incident_report_path = create_incident_report(
                project_name,
                anomaly,
                anomaly["root_cause_analysis"]
            )
            anomaly["incident_report_path"] = incident_report_path

        # Print anomalies
        print(f"\n{'━' * 55}")
        print(f"  Table: {table}")
        print(f"{'━' * 55}")

        for anomaly in anomalies:
            severity_emoji = {
                "critical": "🔴",
                "high":     "🟠",
                "medium":   "🟡",
                "low":      "🟢"
            }.get(anomaly.get("severity"), "⚪")

            print(f"\n  {severity_emoji} [{anomaly['severity'].upper()}] {anomaly['message']}")
            print(f"     Detail: {anomaly['detail']}")
            print(f"     Impact: {anomaly['impact']}")
            print_root_cause_summary(anomaly.get("root_cause_analysis", {}))

        # Fire Slack alert for critical/high anomalies
        critical_anomalies = [
            a for a in anomalies
            if a.get("severity") in ("critical", "high")
        ]

        for anomaly in critical_anomalies:
            rca = anomaly.get("root_cause_analysis", {})
            top_cause = (rca.get("likely_causes") or [{}])[0]
            actions = rca.get("recommended_actions", [])
            affected_models = rca.get("affected_models", [])
            impact_count = rca.get("impact_count", len(affected_models))
            report_path = anomaly.get("incident_report_path")
            diagnosis = {
                "root_cause": top_cause.get("cause", anomaly["message"]),
                "affected_file": table,
                "affected_line": anomaly["type"],
                "explanation": (
                    f"Anomaly: {anomaly['message']}\n"
                    f"Evidence: {_format_anomaly_evidence(anomaly)}"
                ),
                "suggested_fix": (
                    actions[0]
                    if actions
                    else "Investigate upstream pipeline for recent changes"
                ),
                "severity": anomaly["severity"],
                "data_loss_risk": anomaly["severity"] == "critical",
                "impact_count": impact_count,
                "affected_models": affected_models,
                "incident_report": report_path
            }
            send_slack_alert(f"DATA QUALITY — {table}", diagnosis)

        # Update baseline after alerting
        save_baseline(table, current_metrics)
        record_table_metrics(project_name, table, current_metrics)

    print()
    if all_anomalies:
        print(f"🚨 Total anomalies found: {len(all_anomalies)} across {len(tables)} tables.")
    else:
        print("✅ All tables passed quality checks.")
    print()


def _format_anomaly_evidence(anomaly: dict) -> str:
    detail = anomaly.get("detail")
    if detail:
        evidence = detail.replace("got", "observed").rstrip(".")
        evidence = re.sub(r"(observed\s+\d+(?:\.\d+)?)(?!\s+rows?)\b", r"\1 rows", evidence)
        return evidence + "."
    return anomaly.get("impact", "No additional metric evidence available.").rstrip(".") + "."
