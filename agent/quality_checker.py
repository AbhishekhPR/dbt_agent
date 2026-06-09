import json
import re
import sqlite3
from datetime import datetime
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from agent.incident_reporter import create_incident_report
from agent.metrics_store import record_table_metrics
from agent.root_cause_engine import analyze_root_cause
from agent.slack import send_slack_alert

DEFAULT_FRESHNESS_THRESHOLD_MINUTES = 24 * 60

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

BASELINE_PATH = Path(__file__).resolve().parent.parent / "quality_baselines"
BASELINE_PATH.mkdir(exist_ok=True)

SYSTEM_PROMPT = None


def quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite identifier such as a table or column name."""
    return '"' + identifier.replace('"', '""') + '"'


def infer_freshness_column(columns: list[str]) -> str | None:
    """Pick the most likely timestamp column for freshness checks."""
    preferred = [
        "created_at",
        "updated_at",
        "loaded_at",
        "ingested_at",
        "_loaded_at",
        "_relium_sim_updated_at",
    ]
    lower_to_original = {column.lower(): column for column in columns}
    for name in preferred:
        if name in lower_to_original:
            return lower_to_original[name]
    for column in columns:
        lowered = column.lower()
        if lowered.endswith("_at") or "timestamp" in lowered or "date" in lowered:
            return column
    return None


def get_table_metrics(db_path: str, table_name: str) -> dict:
    """
    Pulls quality metrics from a single table.
    Row count, null rates, duplicate rates, min/max values.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    metrics = {"table": table_name}

    try:
        # Row count
        quoted_table = quote_identifier(table_name)

        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
        metrics["row_count"] = cursor.fetchone()[0]

        # Get columns
        cursor.execute(f"PRAGMA table_info({quoted_table})")
        table_info = cursor.fetchall()
        columns = [row[1] for row in table_info]
        metrics["columns"] = columns
        schema = [
            {
                "name": row[1],
                "data_type": row[2],
                "nullable": not bool(row[3]),
                "primary_key": bool(row[5]),
            }
            for row in table_info
        ]
        metrics["schema"] = schema
        metrics["schema_hash"] = hashlib.sha256(
            json.dumps(schema, sort_keys=True).encode("utf-8")
        ).hexdigest()

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

        duplicate_key_columns = _infer_duplicate_key_columns(columns)
        duplicate_rows = _count_duplicate_rows(cursor, quoted_table, duplicate_key_columns)
        metrics["duplicate_rows"] = duplicate_rows
        metrics["duplicate_rate"] = round(100.0 * duplicate_rows / metrics["row_count"], 2) if metrics["row_count"] else 0.0
        metrics["duplicate_key_columns"] = duplicate_key_columns
        metrics["duplicate_check_method"] = (
            "key:" + ",".join(duplicate_key_columns)
            if _has_business_key(columns)
            else "full_row"
        )

        # Numeric column stats
        numeric_stats = {}
        for col in columns:
            quoted_col = quote_identifier(col)
            try:
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
            cursor.execute(f"SELECT COUNT(DISTINCT {quote_identifier(col)}) FROM {quoted_table}")
            distinct_counts[col] = cursor.fetchone()[0]
        metrics["distinct_counts"] = distinct_counts

        freshness_column = infer_freshness_column(columns)
        if freshness_column:
            cursor.execute(
                f"SELECT MAX({quote_identifier(freshness_column)}) FROM {quoted_table}"
            )
            last_updated = cursor.fetchone()[0]
            metrics["freshness_column"] = freshness_column
            metrics["last_updated"] = last_updated
            freshness_minutes = _freshness_minutes(last_updated)
            if freshness_minutes is not None:
                metrics["freshness_minutes"] = freshness_minutes

    except Exception as e:
        metrics["error"] = str(e)
    finally:
        conn.close()

    return metrics


def _has_business_key(columns: list[str]) -> bool:
    return bool(_business_key_columns(columns))


def _infer_duplicate_key_columns(columns: list[str]) -> list[str]:
    business_keys = _business_key_columns(columns)
    return business_keys or columns


def _business_key_columns(columns: list[str]) -> list[str]:
    lowered = {column.lower(): column for column in columns}
    for candidate in ("id", "order_id", "customer_id", "event_id", "product_id"):
        if candidate in lowered:
            return [lowered[candidate]]
    return []


def _count_duplicate_rows(cursor, quoted_table: str, columns: list[str]) -> int:
    if not columns:
        return 0
    grouped = ", ".join(quote_identifier(column) for column in columns)
    cursor.execute(
        f"""
        SELECT COALESCE(SUM(group_count - 1), 0)
        FROM (
            SELECT COUNT(*) AS group_count
            FROM {quoted_table}
            GROUP BY {grouped}
            HAVING COUNT(*) > 1
        )
        """
    )
    return cursor.fetchone()[0] or 0


def _freshness_minutes(last_updated) -> int | None:
    parsed = _parse_datetime(last_updated)
    if parsed is None:
        return None
    return int((datetime.utcnow() - parsed).total_seconds() // 60)


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


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
    Returns list of deterministic anomaly dicts with keys:
    metric, current_value, baseline_value, change_percent, severity, explanation, recommendation
    """
    anomalies = []
    table = current.get("table", "unknown")

    if not baseline:
        return anomalies

    # Row count anomaly — more than 20% change is suspicious
    current_rows = current.get("row_count", 0)
    baseline_rows = baseline.get("row_count", 0)

    if baseline_rows > 0:
        row_change_pct = (current_rows - baseline_rows) / baseline_rows * 100
        if abs(row_change_pct) > 20:
            severity = "critical" if abs(row_change_pct) > 50 else "high"
            msg = f"Row count {'dropped' if row_change_pct<0 else 'spiked'} by {abs(round(row_change_pct,2))}%"
            anomalies.append({
                "metric": "row_count",
                "type": "row_count",
                "current_value": current_rows,
                "baseline_value": baseline_rows,
                "change_percent": round(row_change_pct, 2),
                "severity": severity,
                "explanation": msg,
                "recommendation": "Investigate recent upstream changes and seeds; compare run histories",
                "message": msg,
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
            severity = "critical" if null_increase > 30 else "high"
            msg = f"Null rate on '{col}' jumped by {round(null_increase,1)} percentage points"
            anomalies.append({
                "metric": f"null_rate:{col}",
                "type": "null_rate",
                "current_value": current_null_rate,
                "baseline_value": baseline_null_rate,
                "change_percent": round(null_increase, 2),
                "severity": severity,
                "explanation": msg,
                "recommendation": "Check recent upstream schema/ingestion changes and defaults",
                "message": msg,
                "detail": f"Was {baseline_null_rate}% null, now {current_null_rate}% null",
                "impact": f"Aggregations on '{col}' may return incorrect results"
            })

    # Duplicate explosion
    current_dupes = current.get("duplicate_rows", 0)
    baseline_dupes = baseline.get("duplicate_rows", 0)

    if current_dupes > baseline_dupes + 10:
        msg = f"Duplicate rows jumped from {baseline_dupes} to {current_dupes}"
        anomalies.append({
            "metric": "duplicate_rows",
            "type": "duplicate_rows",
            "current_value": current_dupes,
            "baseline_value": baseline_dupes,
            "change_percent": None,
            "severity": "high",
            "explanation": "Duplicate row count increased significantly",
            "recommendation": "Investigate recent JOINs or upstream dedup steps",
            "message": msg,
            "detail": "Possible fan-out from a bad JOIN upstream",
            "impact": "Metrics like SUM(revenue) will be inflated"
        })

    # Cardinality explosion — distinct values suddenly way higher
    current_distinct = current.get("distinct_counts", {})
    baseline_distinct = baseline.get("distinct_counts", {})

    for col, current_count in current_distinct.items():
        baseline_count = baseline_distinct.get(col, 0)
        if baseline_count > 0:
            cardinality_change = (current_count - baseline_count) / baseline_count * 100
            if cardinality_change >= 200:
                msg = f"Distinct values in '{col}' increased by {round(cardinality_change, 1)}%"
                anomalies.append({
                    "metric": f"distinct_count:{col}",
                    "type": "distinct_count",
                    "current_value": current_count,
                    "baseline_value": baseline_count,
                    "change_percent": round(cardinality_change, 2),
                    "severity": "medium",
                    "explanation": "Distinct value count increased dramatically",
                    "recommendation": "Check for new data sources, bad joins, or format changes",
                    "message": msg,
                    "detail": f"Was {baseline_count} distinct values, now {current_count}",
                    "impact": "GROUP BY queries on this column may return unexpected granularity"
                })

    # Freshness anomaly: use a baseline-specific threshold when available,
    # otherwise default to 24 hours.
    freshness_minutes = current.get("freshness_minutes")
    threshold_minutes = baseline.get(
        "freshness_threshold_minutes",
        DEFAULT_FRESHNESS_THRESHOLD_MINUTES,
    )
    if freshness_minutes is not None and freshness_minutes > threshold_minutes:
        stale_hours = round(freshness_minutes / 60, 1)
        msg = f"Table is stale by {stale_hours} hours"
        anomalies.append({
            "metric": "freshness",
            "type": "freshness_anomaly",
            "current_value": freshness_minutes,
            "baseline_value": threshold_minutes,
            "change_percent": None,
            "severity": "critical" if freshness_minutes > threshold_minutes * 2 else "high",
            "explanation": msg,
            "recommendation": "Check ingestion freshness and upstream scheduled jobs",
            "message": msg,
            "detail": f"Latest {current.get('freshness_column', 'freshness column')} value is {current.get('last_updated')}",
            "impact": "Downstream models may be using outdated data",
        })

    anomalies.extend(_detect_schema_drift(current, baseline))

    return anomalies


def _detect_schema_drift(current: dict, baseline: dict) -> list:
    current_schema = {column["name"]: column for column in current.get("schema", [])}
    baseline_schema = {column["name"]: column for column in baseline.get("schema", [])}
    anomalies = []

    for name, old_column in baseline_schema.items():
        if name not in current_schema:
            anomalies.append(_schema_anomaly("removed_column", name, old_column, None, "critical"))

    for name, new_column in current_schema.items():
        if name not in baseline_schema:
            anomalies.append(_schema_anomaly("added_column", name, None, new_column, "medium"))

    for name, old_column in baseline_schema.items():
        new_column = current_schema.get(name)
        if not new_column:
            continue
        old_type = (old_column.get("data_type") or "").upper()
        new_type = (new_column.get("data_type") or "").upper()
        if old_type != new_type:
            anomalies.append(_schema_anomaly("type_change", name, old_column, new_column, "high"))

    return anomalies


def _schema_anomaly(change_type: str, column_name: str, old_column: dict | None, new_column: dict | None, severity: str) -> dict:
    if change_type == "removed_column":
        message = f"Schema drift detected: column '{column_name}' was removed"
    elif change_type == "added_column":
        message = f"Schema drift detected: column '{column_name}' was added"
    else:
        message = f"Schema drift detected: column '{column_name}' changed type"

    schema_change = {
        "change_type": change_type,
        "column": column_name,
    }
    if old_column:
        schema_change["old_type"] = old_column.get("data_type")
    if new_column:
        schema_change["new_type"] = new_column.get("data_type")

    return {
        "metric": "schema",
        "type": "schema_drift",
        "current_value": schema_change.get("new_type"),
        "baseline_value": schema_change.get("old_type"),
        "change_percent": None,
        "severity": severity,
        "explanation": message,
        "recommendation": "Review downstream SQL models before deploying this schema change",
        "message": message,
        "detail": message,
        "impact": "Downstream SQL models may fail or silently produce incorrect metrics",
        "schema_change": schema_change,
    }


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
        for anomaly in anomalies:
            anomaly["table"] = table
        all_anomalies.extend(anomalies)

        # Local metadata-only RCA. No LLM call.
        for anomaly in anomalies:
            rca_report = analyze_root_cause({
                **anomaly,
                "type": anomaly.get("type"),
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
