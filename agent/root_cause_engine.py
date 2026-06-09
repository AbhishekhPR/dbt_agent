import json
import re
import sqlite3
from pathlib import Path
from statistics import median

from agent import metrics_store
from agent.blast_radius import calculate_blast_radius


ROW_DROP_ACTIONS = [
    "Check upstream ingestion job for {table}",
    "Compare latest row count with previous successful run",
    "Review recent WHERE clause/filter changes",
    "Check whether joins are removing unmatched records",
    "Check whether source table was truncated or partially loaded",
]

ROW_SPIKE_ACTIONS = [
    "Check for duplicate ingestion",
    "Inspect JOIN conditions for fan-out",
    "Review SQL for accidental CROSS JOIN",
    "Validate deduplication logic",
]

NULL_EXPLOSION_ACTIONS = [
    "Check source schema changes",
    "Validate join keys",
    "Inspect upstream transformations",
]

DUPLICATE_EXPLOSION_ACTIONS = [
    "Check ingestion job for repeated loads",
    "Verify unique key constraints",
    "Inspect recent joins for fan-out",
    "Validate deduplication logic",
]

CARDINALITY_EXPLOSION_ACTIONS = [
    "Review grouping dimensions",
    "Inspect recent join logic changes",
    "Check for malformed or newly introduced dimension values",
]

FRESHNESS_ANOMALY_ACTIONS = [
    "Check whether the scheduled ingestion job for {table} ran successfully",
    "Verify the latest source sync timestamp",
    "Check whether the source connector is paused or delayed",
    "Review orchestration logs for failed, skipped, or delayed jobs",
    "Confirm whether the source system is producing new records",
    "Validate the expected freshness SLA for this table",
]

SCHEMA_DRIFT_ACTIONS = [
    "Check recent upstream schema changes",
    "Review source connector schema sync history",
    "Search downstream SQL models for references to changed columns",
    "Validate dbt model contracts or tests",
    "Confirm whether the schema change was intentional",
]


def analyze_root_cause(anomaly: dict) -> dict:
    """
    Analyze a quality anomaly using local metadata only.

    This function is deterministic and does not call any LLM or external API.
    """
    table = anomaly.get("table")
    anomaly_type = anomaly.get("type") or anomaly.get("anomaly")
    project_path = anomaly.get("project_path") or anomaly.get("project") or "test_project"

    history = _load_metric_history(table)
    previous, current = _split_baseline_and_current(history)
    signal = _build_signal(anomaly_type, anomaly, previous, current)

    likely_causes = _rank_causes(anomaly_type, signal)
    changed_columns = _changed_columns_from_anomaly(anomaly) if anomaly_type == "schema_drift" else []
    blast = _safe_blast_radius(project_path, table, changed_columns)
    affected_models = _affected_model_names(blast)

    return {
        "table": table,
        "anomaly": anomaly_type,
        "direction": signal.get("direction", "unknown"),
        "change_pct": signal.get("change_pct", 0),
        "likely_causes": likely_causes,
        "affected_models": affected_models,
        "impact_count": blast.get("total_affected") or len(affected_models),
        "recommended_actions": _recommended_actions(anomaly_type, signal, table),
    }


def print_root_cause_report(report: dict):
    """Pretty print a root cause report for the CLI."""
    print("\n🧠 Root Cause Analysis\n")
    print("Table:")
    print(report.get("table", "unknown"))
    print("\nAnomaly:")
    print(report.get("anomaly", "unknown"))
    print("\nDirection:")
    print(report.get("direction", "unknown"))
    print("\nChange:")
    change_pct = report.get("change_pct", 0)
    print(f"{float(change_pct):.1f}%")

    print("\nLikely Causes:\n")
    causes = report.get("likely_causes", [])
    if not causes:
        print("No deterministic cause identified from available metadata.\n")
    for idx, item in enumerate(causes, 1):
        print(f"{idx}. {item['cause']}")
        print(f"   Confidence: {item['confidence']:.2f}")
        print(f"   Reason: {item['reason']}\n")

    affected = report.get("affected_models", [])
    print("Affected Models:\n")
    if affected:
        for model in affected:
            print(f"- {model}")
    else:
        print("- None found")

    print("\nRecommended Actions:\n")
    for action in report.get("recommended_actions", []):
        print(f"- {action}")
    print()


def run_root_cause(project: str, table: str, anomaly: str, message: str = "") -> dict:
    report = analyze_root_cause(
        {
            "type": anomaly,
            "table": table,
            "project_path": project,
            "message": message or "",
        }
    )
    print_root_cause_report(report)
    return report


def _load_metric_history(table: str) -> list:
    if not table:
        return []

    db_path = Path(metrics_store.METADATA_HISTORY_DB)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "table_metrics"):
            return []

        columns = _column_names(conn, "table_metrics")
        time_col = "timestamp" if "timestamp" in columns else "recorded_at"
        select_cols = [c for c in columns if c in {
            "timestamp",
            "recorded_at",
            "project_name",
            "table_name",
            "row_count",
            "duplicate_rows",
            "metrics_json",
            "null_rates",
            "distinct_counts",
        }]
        rows = conn.execute(
            f"""
            SELECT {", ".join(select_cols)}
            FROM table_metrics
            WHERE lower(table_name) = lower(?)
            ORDER BY {time_col} ASC, id ASC
            """,
            (table,),
        ).fetchall()
        return [_normalize_metric_row(dict(row)) for row in rows]
    finally:
        conn.close()


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_names(conn, table_name: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _normalize_metric_row(row: dict) -> dict:
    metrics = _loads(row.get("metrics_json"))
    null_rates = metrics.get("null_rates") or _loads(row.get("null_rates"))
    distinct_counts = metrics.get("distinct_counts") or _loads(row.get("distinct_counts"))
    return {
        "row_count": _number(row.get("row_count")),
        "duplicate_rows": _number(row.get("duplicate_rows")),
        "null_rates": null_rates or {},
        "distinct_counts": distinct_counts or {},
    }


def _loads(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_baseline_and_current(history: list) -> tuple:
    if not history:
        return {}, {}
    if len(history) == 1:
        return {}, history[-1]
    current = history[-1]
    baseline_rows = history[:-1] or history
    return _median_metric(baseline_rows), current


def _median_metric(rows: list) -> dict:
    row_counts = [r["row_count"] for r in rows if r.get("row_count") is not None]
    dupes = [r["duplicate_rows"] for r in rows if r.get("duplicate_rows") is not None]
    return {
        "row_count": median(row_counts) if row_counts else None,
        "duplicate_rows": median(dupes) if dupes else None,
        "null_rates": _median_nested(rows, "null_rates"),
        "distinct_counts": _median_nested(rows, "distinct_counts"),
    }


def _median_nested(rows: list, key: str) -> dict:
    values_by_name = {}
    for row in rows:
        for name, value in (row.get(key) or {}).items():
            numeric = _number(value)
            if numeric is not None:
                values_by_name.setdefault(name, []).append(numeric)
    return {name: median(values) for name, values in values_by_name.items()}


def _build_signal(anomaly_type: str, anomaly: dict, previous: dict, current: dict) -> dict:
    if anomaly_type == "row_count_anomaly":
        return _row_count_signal(anomaly, previous, current)
    if anomaly_type == "null_explosion":
        return _nested_increase_signal("null_rates", "null rate", anomaly, previous, current)
    if anomaly_type == "duplicate_explosion":
        return _simple_increase_signal("duplicate_rows", "duplicate rows", anomaly, previous, current)
    if anomaly_type == "cardinality_explosion":
        return _nested_increase_signal("distinct_counts", "distinct values", anomaly, previous, current)
    if anomaly_type == "freshness_anomaly":
        return _freshness_signal(anomaly)
    if anomaly_type == "schema_drift":
        return _schema_drift_signal(anomaly)
    return {"direction": "unknown", "magnitude": _pct_from_message(anomaly.get("message", ""))}


def _row_count_signal(anomaly: dict, previous: dict, current: dict) -> dict:
    anomaly_message = anomaly.get("message")
    if anomaly_message:
        message = _message_row_count_signal(anomaly_message)
        if message:
            message["reason"] = anomaly_message
            return message

    explicit = _explicit_row_count_signal(anomaly)
    if explicit:
        return explicit

    baseline = previous.get("row_count")
    current_rows = current.get("row_count")
    if baseline and current_rows is not None:
        change = (current_rows - baseline) / baseline
        change_pct = round(abs(change) * 100, 1)
        if change == 0:
            return _no_evidence_signal()
        direction = "dropped" if change < 0 else "spiked"
        return {
            "direction": direction,
            "change_pct": change_pct,
            "confidence": _row_count_confidence(change_pct),
            "reason": f"row count {direction} by {change_pct:g}% compared to historical baseline",
        }

    return _no_evidence_signal()


def _explicit_row_count_signal(anomaly: dict) -> dict:
    direction = _normalize_direction(anomaly.get("direction"))
    change_pct = anomaly.get("change_pct")
    current_rows = anomaly.get("current_rows")
    baseline_rows = anomaly.get("baseline_rows")

    if change_pct is None and baseline_rows:
        current_number = _number(current_rows)
        baseline_number = _number(baseline_rows)
        if current_number is not None and baseline_number:
            change_pct = abs(current_number - baseline_number) / baseline_number * 100
            if direction == "unknown":
                direction = "dropped" if current_number < baseline_number else "spiked"

    change_pct = _number(change_pct)
    if direction == "unknown" or change_pct is None:
        return {}

    change_pct = round(change_pct, 1)
    return {
        "direction": direction,
        "change_pct": change_pct,
        "confidence": _row_count_confidence(change_pct),
        "reason": f"row count {direction} by {change_pct:g}%",
    }


def _message_row_count_signal(message: str) -> dict:
    direction = _direction_from_message(message)
    if direction == "unknown":
        return {}

    change_pct = round(_pct_from_message(message), 1)
    return {
        "direction": direction,
        "change_pct": change_pct,
        "confidence": _row_count_confidence(change_pct),
        "reason": f"row count {direction} by {change_pct:g}%",
    }


def _no_evidence_signal() -> dict:
    return {
        "direction": "unknown",
        "change_pct": 0,
        "confidence": 0.2,
        "reason": "no strong row count RCA evidence in anomaly or historical metadata",
    }


def _simple_increase_signal(metric: str, label: str, anomaly: dict, previous: dict, current: dict) -> dict:
    anomaly_message = anomaly.get("message")
    if anomaly_message:
        explicit = _message_increase_signal(label, anomaly_message)
        if explicit:
            explicit["reason"] = anomaly_message
            return explicit

    baseline = previous.get(metric)
    current_value = current.get(metric)
    if baseline is not None and current_value is not None:
        increase = current_value - baseline
        ratio = increase / max(baseline, 1)
        return {
            "direction": "spike",
            "magnitude": max(0.0, ratio),
            "reason": f"{label} increased from {int(baseline)} to {int(current_value)}",
        }
    return {
        "direction": "spike",
        "magnitude": _pct_from_message(anomaly.get("message", "")) / 100,
        "reason": f"{label} increased according to anomaly message",
    }


def _message_increase_signal(label: str, message: str) -> dict:
    match = re.search(
        rf"{re.escape(label)}\s+increased\s+from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)",
        message or "",
        re.IGNORECASE,
    )
    if not match:
        return {}

    baseline = float(match.group(1))
    current_value = float(match.group(2))
    increase = current_value - baseline
    ratio = increase / max(baseline, 1)
    return {
        "direction": "spike",
        "magnitude": max(0.0, ratio),
        "reason": f"{label} increased from {int(baseline)} to {int(current_value)}",
    }


def _nested_increase_signal(metric: str, label: str, anomaly: dict, previous: dict, current: dict) -> dict:
    anomaly_message = anomaly.get("message")
    if anomaly_message:
        return {
            "direction": "spike",
            "magnitude": _pct_from_message(anomaly_message) / 50,
            "reason": anomaly_message,
        }

    baseline = previous.get(metric) or {}
    current_values = current.get(metric) or {}
    largest = None
    for name, current_value in current_values.items():
        current_number = _number(current_value)
        baseline_number = _number(baseline.get(name, 0))
        if current_number is None or baseline_number is None:
            continue
        increase = current_number - baseline_number
        if largest is None or increase > largest["increase"]:
            largest = {"name": name, "increase": increase}

    if largest and largest["increase"] > 0:
        return {
            "direction": "spike",
            "magnitude": largest["increase"] / 50,
            "reason": f"{largest['name']} {label} increased by {round(largest['increase'], 1)} points",
        }

    return {
        "direction": "spike",
        "magnitude": _pct_from_message(anomaly.get("message", "")) / 50,
        "reason": f"{label} increased according to anomaly message",
    }


def _freshness_signal(anomaly: dict) -> dict:
    message = anomaly.get("message") or anomaly.get("detail") or (
        "The table has not received fresh records within the expected freshness window."
    )
    return {
        "direction": "stale",
        "magnitude": _pct_from_message(message) / 100,
        "confidence": 0.85,
        "reason": message,
    }


def _schema_drift_signal(anomaly: dict) -> dict:
    message = anomaly.get("message") or anomaly.get("detail") or "Schema drift detected"
    return {
        "direction": "changed",
        "magnitude": 0.75,
        "confidence": 0.85,
        "reason": message,
    }


def _rank_causes(anomaly_type: str, signal: dict) -> list:
    direction = signal.get("direction")
    confidence = signal.get("confidence", _confidence(signal.get("magnitude", 0)))
    reason = signal.get("reason", "metadata signal exceeded historical baseline")

    if anomaly_type == "row_count_anomaly" and direction == "dropped":
        return _row_count_drop_causes(confidence, reason)
    if anomaly_type == "row_count_anomaly" and direction == "spiked":
        return _row_count_spike_causes(confidence, reason)
    if anomaly_type == "row_count_anomaly":
        return [{
            "cause": "no strong RCA evidence",
            "confidence": min(0.25, round(confidence, 2)),
            "reason": reason,
        }]
    if anomaly_type == "null_explosion":
        return _causes(
            ["source column missing", "failed join key", "schema evolution", "upstream pipeline issue"],
            confidence,
            reason,
        )
    if anomaly_type == "duplicate_explosion":
        return _causes(
            ["duplicate ingestion", "bad join", "missing deduplication", "retry/replay of source load"],
            confidence,
            reason,
        )
    if anomaly_type == "cardinality_explosion":
        return _causes(
            ["new dimension introduced", "malformed grouping", "incorrect join logic"],
            confidence,
            reason,
        )
    if anomaly_type == "freshness_anomaly":
        return _causes(
            [
                "upstream ingestion delay",
                "failed scheduled load",
                "source connector paused",
                "warehouse/job orchestration failure",
            ],
            confidence,
            reason,
        )
    if anomaly_type == "schema_drift":
        return _causes(
            [
                "upstream schema change",
                "source connector schema evolution",
                "dbt model contract mismatch",
                "renamed or removed source field",
            ],
            confidence,
            reason,
        )
    return []


def _causes(names: list, top_confidence: float, reason: str) -> list:
    decay = [0.0, 0.19, 0.28, 0.34]
    results = []
    for idx, name in enumerate(names):
        confidence = max(0.0, round(top_confidence - decay[min(idx, len(decay) - 1)], 2))
        results.append({"cause": name, "confidence": confidence, "reason": reason})
    return results


def _row_count_drop_causes(top_confidence: float, reason: str) -> list:
    return [
        {"cause": "upstream ingestion failure", "confidence": round(top_confidence, 2), "reason": reason},
        {
            "cause": "accidental filter introduction",
            "confidence": max(0.0, round(top_confidence - 0.1, 2)),
            "reason": "a restrictive filter can remove records before downstream models run",
        },
        {
            "cause": "source table truncation",
            "confidence": max(0.0, round(top_confidence - 0.15, 2)),
            "reason": "large row count drops can indicate partial loads or truncation",
        },
        {
            "cause": "join removing records",
            "confidence": max(0.0, round(top_confidence - 0.25, 2)),
            "reason": "downstream joins may remove unmatched rows",
        },
    ]


def _row_count_spike_causes(top_confidence: float, reason: str) -> list:
    return [
        {"cause": "duplicate ingestion", "confidence": round(top_confidence, 2), "reason": reason},
        {
            "cause": "join fan-out",
            "confidence": max(0.0, round(top_confidence - 0.1, 2)),
            "reason": "many-to-many joins can multiply records",
        },
        {
            "cause": "accidental cross join",
            "confidence": max(0.0, round(top_confidence - 0.15, 2)),
            "reason": "missing join predicates can create a Cartesian product",
        },
        {
            "cause": "missing deduplication",
            "confidence": max(0.0, round(top_confidence - 0.25, 2)),
            "reason": "deduplication changes can allow repeated records downstream",
        },
    ]


def _row_count_confidence(change_pct: float) -> float:
    if change_pct >= 90:
        return 0.95
    if change_pct >= 50:
        return 0.85
    if change_pct >= 20:
        return 0.65
    if change_pct > 0:
        return 0.4
    return 0.2


def _confidence(magnitude: float) -> float:
    magnitude = max(0.0, float(magnitude or 0.0))
    if magnitude >= 0.9:
        return min(0.99, round(magnitude, 2))
    return round(min(0.9, 0.35 + magnitude * 0.8), 2)


def _pct_from_message(message: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", message or "")
    return float(match.group(1)) if match else 0.0


def _direction_from_message(message: str) -> str:
    lowered = (message or "").lower()
    if any(word in lowered for word in ("dropped", "drop", "decreased", "lower")):
        return "dropped"
    if any(word in lowered for word in ("spiked", "spike", "increased", "higher")):
        return "spiked"
    return "unknown"


def _normalize_direction(direction: str) -> str:
    lowered = (direction or "").lower()
    if lowered in ("dropped", "drop", "decreased", "lower"):
        return "dropped"
    if lowered in ("spiked", "spike", "increased", "higher"):
        return "spiked"
    return "unknown"


def _recommended_actions(anomaly_type: str, signal: dict, table: str) -> list:
    if anomaly_type == "row_count_anomaly":
        if signal.get("direction") == "dropped":
            return [action.format(table=table) for action in ROW_DROP_ACTIONS]
        if signal.get("direction") == "spiked":
            return ROW_SPIKE_ACTIONS
        return [
            "Compare row counts with previous run",
            "Check upstream ingestion jobs",
            "Check for duplicate ingestion",
            "Inspect join conditions",
        ]

    return {
        "null_explosion": NULL_EXPLOSION_ACTIONS,
        "duplicate_explosion": DUPLICATE_EXPLOSION_ACTIONS,
        "cardinality_explosion": CARDINALITY_EXPLOSION_ACTIONS,
        "freshness_anomaly": [action.format(table=table) for action in FRESHNESS_ANOMALY_ACTIONS],
        "schema_drift": SCHEMA_DRIFT_ACTIONS,
    }.get(anomaly_type, ROW_SPIKE_ACTIONS)


def _changed_columns_from_anomaly(anomaly: dict) -> list:
    schema_change = anomaly.get("schema_change") or {}
    column = schema_change.get("column")
    return [column] if column else []


def _safe_blast_radius(project_path: str, table: str, changed_columns: list = None) -> dict:
    if not table:
        return {"directly_affected": [], "indirectly_affected": [], "total_affected": 0}
    try:
        if changed_columns:
            return calculate_blast_radius(project_path, table, changed_columns=changed_columns)
        return calculate_blast_radius(project_path, table)
    except Exception as exc:
        if changed_columns:
            try:
                return calculate_blast_radius(project_path, table)
            except Exception as fallback_exc:
                exc = fallback_exc
        return {
            "directly_affected": [],
            "indirectly_affected": [],
            "total_affected": 0,
            "error": str(exc),
        }


def _affected_model_names(blast: dict) -> list:
    models = []
    for section in ("directly_affected", "indirectly_affected"):
        for item in blast.get(section, []):
            model = item.get("model") if isinstance(item, dict) else item
            if model and model not in models:
                models.append(model)
    return models
