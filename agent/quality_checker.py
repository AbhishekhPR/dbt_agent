import json
import sqlite3
from pathlib import Path
from agent.logging_config import get_logger
from agent.slack import send_slack_alert

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from agent.groq_client import call_llm_json
except ImportError:
    call_llm_json = None

logger = get_logger(__name__)

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

BASELINE_PATH = Path(__file__).resolve().parent.parent / "quality_baselines"
BASELINE_PATH.mkdir(exist_ok=True)

SYSTEM_PROMPT = """
You are a senior data quality engineer. You analyze data pipeline
metrics and identify anomalies that indicate silent data corruption.
You always respond with valid JSON only. No explanation outside JSON.
"""


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite identifier to prevent injection."""
    return '"' + identifier.replace('"', '""') + '"'


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
        quoted_table = _quote_identifier(table_name)
        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
        metrics["row_count"] = cursor.fetchone()[0]

        # Get columns
        cursor.execute(f"PRAGMA table_info({quoted_table})")
        columns = [row[1] for row in cursor.fetchall()]
        metrics["columns"] = columns

        # Null rates per column
        null_rates = {}
        for col in columns:
            quoted_col = _quote_identifier(col)
            cursor.execute(f"""
                SELECT
                    ROUND(100.0 * SUM(CASE WHEN {quoted_col} IS NULL THEN 1 ELSE 0 END)
                    / COUNT(*), 2)
                FROM {quoted_table}
            """)
            null_rates[col] = cursor.fetchone()[0] or 0.0
        metrics["null_rates"] = null_rates

        # Duplicate rate
        cursor.execute(f"""
            SELECT COUNT(*) - COUNT(DISTINCT rowid)
            FROM {quoted_table}
        """)
        metrics["duplicate_rows"] = cursor.fetchone()[0]

        # Numeric column stats
        numeric_stats = {}
        for col in columns:
            try:
                quoted_col = _quote_identifier(col)
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
            except Exception as e:
                logger.debug(f"Could not compute numeric stats for {col}: {e}")
        metrics["numeric_stats"] = numeric_stats

        # Distinct value counts per column
        distinct_counts = {}
        for col in columns:
            quoted_col = _quote_identifier(col)
            cursor.execute(f"SELECT COUNT(DISTINCT {quoted_col}) FROM {quoted_table}")
            distinct_counts[col] = cursor.fetchone()[0]
        metrics["distinct_counts"] = distinct_counts

    except Exception as e:
        logger.error(f"Error getting metrics for {table_name}: {e}")
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

    if current_dupes > baseline_dupes + 10:
        anomalies.append({
            "type": "duplicate_explosion",
            "severity": "high",
            "table": table,
            "message": f"Duplicate rows jumped from {baseline_dupes} to {current_dupes}",
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


def ask_claude_about_anomalies(anomalies: list, metrics: dict) -> dict:
    """
    Sends anomalies to Claude for deeper analysis and recommendations.
    """
    if not anomalies or not call_llm_json:
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
    try:
        return call_llm_json(prompt=prompt, system=SYSTEM_PROMPT)
    except Exception as e:
        logger.warning(f"Could not get Claude analysis for anomalies: {e}")
        return {}


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
            continue

        anomalies = detect_anomalies(current_metrics, baseline_metrics)

        if not anomalies:
            print(f"    ✅ {table} — all metrics within normal range.")
            save_baseline(table, current_metrics)
            continue

        print(f"    🚨 {table} — {len(anomalies)} anomaly/anomalies detected!")
        all_anomalies.extend(anomalies)

        # Ask Claude for deeper analysis
        claude_analysis = ask_claude_about_anomalies(anomalies, current_metrics)

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

        if claude_analysis:
            print(f"\n  🤖 Claude's Assessment:")
            print(f"     Hypothesis: {claude_analysis.get('root_cause_hypothesis', 'N/A')}")
            print(f"     Halt pipeline: {'🛑 YES' if claude_analysis.get('should_halt_pipeline') else '✅ No'}")
            print(f"     Confidence: {claude_analysis.get('confidence', 'N/A')}")

            actions = claude_analysis.get("immediate_actions", [])
            if actions:
                print(f"\n  📋 Immediate Actions:")
                for action in actions:
                    print(f"     • {action}")

            queries = claude_analysis.get("queries_to_investigate", [])
            if queries:
                print(f"\n  🔍 Queries to Run:")
                for query in queries:
                    print(f"     {query}")

        # Fire Slack alert for critical/high anomalies
        critical_anomalies = [
            a for a in anomalies
            if a.get("severity") in ("critical", "high")
        ]

        for anomaly in critical_anomalies:
            diagnosis = {
                "root_cause": anomaly["message"],
                "affected_file": f"Table: {table}",
                "affected_line": anomaly["type"],
                "explanation": anomaly["impact"],
                "suggested_fix": claude_analysis.get(
                    "root_cause_hypothesis",
                    "Investigate upstream pipeline for recent changes"
                ),
                "severity": anomaly["severity"],
                "data_loss_risk": anomaly["severity"] == "critical"
            }
            send_slack_alert(f"DATA QUALITY — {table}", diagnosis)

        # Update baseline after alerting
        save_baseline(table, current_metrics)

    print()
    if all_anomalies:
        print(f"🚨 Total anomalies found: {len(all_anomalies)} across {len(tables)} tables.")
    else:
        print("✅ All tables passed quality checks.")
    print()
