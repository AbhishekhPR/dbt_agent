from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.signals import Signal


DEFAULT_STALE_AFTER_HOURS = 6
DEFAULT_CRITICAL_AFTER_HOURS = 24

FRESHNESS_COLUMN_PRIORITY = (
    "source_max_updated_at",
    "source_max_ingested_at",
    "source_max_event_time",
    "source_updated_at",
    "source_ingested_at",
    "source_event_time",
    "model_built_at",
    "updated_at",
    "ingested_at",
    "loaded_at",
    "created_at",
    "event_time",
    "_loaded_at",
)


METADATA_CHECK_SIGNAL_CONFIDENCE = {
    "HIGH": 95,
    "MEDIUM": 85,
    "LOW": 75,
}

METADATA_CHECK_SIGNAL_SCORES = {
    "HIGH": -30,
    "MEDIUM": -15,
    "LOW": -5,
}


@dataclass
class MetadataCheckResult:
    model_name: str
    row_count: int
    null_count: int
    duplicate_count: int
    freshness_timestamp: str | None
    schema_column_count: int
    anomalies: list[str] = field(default_factory=list)
    freshness_column: str | None = None
    freshness_status: str | None = None
    freshness_age_hours: float | None = None


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


def select_freshness_column(columns: list[str]) -> str | None:
    lower_to_original = {column.casefold(): column for column in columns}
    for candidate in FRESHNESS_COLUMN_PRIORITY:
        if candidate in lower_to_original:
            return lower_to_original[candidate]
    for column in columns:
        normalized = column.casefold()
        if (
            normalized.endswith("_at")
            or "timestamp" in normalized
            or normalized.endswith("_date")
        ):
            return column
    return None


def get_freshness_observation(
    conn,
    table_name: str,
) -> tuple[str | None, str | None]:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")]
    freshness_column = select_freshness_column(columns)
    if not freshness_column:
        return None, None
    quoted_column = '"' + freshness_column.replace('"', '""') + '"'
    value = conn.execute(
        f"SELECT MAX({quoted_column}) FROM {table_name}"
    ).fetchone()[0]
    return freshness_column, str(value) if value is not None else None


def get_freshness_timestamp(conn, table_name: str) -> str | None:
    _, timestamp = get_freshness_observation(conn, table_name)
    return timestamp


def evaluate_freshness_sla(
    freshness_timestamp: str | datetime | None,
    *,
    now: datetime | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    critical_after_hours: float = DEFAULT_CRITICAL_AFTER_HOURS,
) -> dict:
    if stale_after_hours < 0:
        raise ValueError("stale_after_hours must be non-negative")
    if critical_after_hours < stale_after_hours:
        raise ValueError(
            "critical_after_hours must be greater than or equal to stale_after_hours"
        )

    parsed = _parse_freshness_timestamp(freshness_timestamp)
    if parsed is None:
        return {
            "status": "unknown",
            "age_hours": None,
            "last_updated": (
                str(freshness_timestamp)
                if freshness_timestamp is not None
                else None
            ),
            "stale_after_hours": stale_after_hours,
            "critical_after_hours": critical_after_hours,
        }

    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    else:
        evaluated_at = evaluated_at.astimezone(timezone.utc)

    age_hours = round((evaluated_at - parsed).total_seconds() / 3600, 2)
    if age_hours >= critical_after_hours:
        status = "critical"
    elif age_hours >= stale_after_hours:
        status = "stale"
    else:
        status = "ok"
    return {
        "status": status,
        "age_hours": age_hours,
        "last_updated": parsed.isoformat(),
        "stale_after_hours": stale_after_hours,
        "critical_after_hours": critical_after_hours,
    }


def _parse_freshness_timestamp(
    value: str | datetime | None,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_schema_column_count(conn, table_name: str) -> int:
    return len(list(conn.execute(f"PRAGMA table_info({table_name})")))


def run_metadata_checks(
    conn,
    table_name: str,
    key_columns: list[str],
    *,
    now: datetime | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    critical_after_hours: float = DEFAULT_CRITICAL_AFTER_HOURS,
) -> MetadataCheckResult:
    row_count = get_row_count(conn, table_name)
    null_count = get_null_count(conn, table_name, key_columns)
    duplicate_count = get_duplicate_count(conn, table_name, key_columns)
    freshness_column, freshness_timestamp = get_freshness_observation(
        conn,
        table_name,
    )
    freshness = evaluate_freshness_sla(
        freshness_timestamp,
        now=now,
        stale_after_hours=stale_after_hours,
        critical_after_hours=critical_after_hours,
    )
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
    elif freshness["status"] in {"stale", "critical"}:
        anomalies.append(f"Freshness SLA breached: {freshness['status']}")

    return MetadataCheckResult(
        model_name=table_name,
        row_count=row_count,
        null_count=null_count,
        duplicate_count=duplicate_count,
        freshness_timestamp=freshness_timestamp,
        schema_column_count=schema_column_count,
        anomalies=anomalies,
        freshness_column=freshness_column,
        freshness_status=freshness["status"],
        freshness_age_hours=freshness["age_hours"],
    )


def to_signal(
    metadata_result: MetadataCheckResult | dict,
    *,
    safe_to_continue: bool | None = None,
) -> Signal:
    result = _metadata_result_as_dict(metadata_result)
    severity = _metadata_signal_severity(result, safe_to_continue)
    neutral = _metadata_signal_is_neutral(result, severity)
    metadata = {
        "row_count": result.get("row_count"),
        "null_count": result.get("null_count"),
        "duplicate_count": result.get("duplicate_count"),
        "freshness_timestamp": result.get("freshness_timestamp"),
        "schema_columns": result.get("schema_column_count"),
        "safe_to_continue": safe_to_continue,
    }
    if result.get("evaluation_status") is not None:
        metadata["evaluation_status"] = result.get("evaluation_status")
    if result.get("freshness_column") is not None:
        metadata["freshness_column"] = result.get("freshness_column")
    if result.get("freshness_status") is not None:
        metadata["freshness_status"] = result.get("freshness_status")
    if result.get("freshness_age_hours") is not None:
        metadata["freshness_age_hours"] = result.get("freshness_age_hours")

    return Signal(
        component="metadata_checks",
        severity=severity,
        confidence=METADATA_CHECK_SIGNAL_CONFIDENCE[severity],
        score=0 if neutral else METADATA_CHECK_SIGNAL_SCORES[severity],
        reasons=[] if neutral else _metadata_signal_reasons(result),
        metadata=metadata,
    )


def _metadata_result_as_dict(metadata_result: MetadataCheckResult | dict) -> dict:
    if isinstance(metadata_result, dict):
        return dict(metadata_result)
    return {
        "model_name": metadata_result.model_name,
        "row_count": metadata_result.row_count,
        "null_count": metadata_result.null_count,
        "duplicate_count": metadata_result.duplicate_count,
        "freshness_timestamp": metadata_result.freshness_timestamp,
        "schema_column_count": metadata_result.schema_column_count,
        "anomalies": list(metadata_result.anomalies),
        "freshness_column": metadata_result.freshness_column,
        "freshness_status": metadata_result.freshness_status,
        "freshness_age_hours": metadata_result.freshness_age_hours,
    }


def _metadata_signal_severity(
    result: dict,
    safe_to_continue: bool | None,
) -> str:
    if result.get("severity"):
        return str(result["severity"]).upper()
    if safe_to_continue is False or result.get("anomalies"):
        return "HIGH"
    return "LOW"


def _metadata_signal_reasons(result: dict) -> list[str]:
    reasons = []
    if result.get("row_count") == 0:
        reasons.append("Row count changed unexpectedly")
    if result.get("null_count", 0) > 0:
        reasons.append("Null rate increased")
    if result.get("duplicate_count", 0) > 0:
        reasons.append("Duplicate count increased")
    if result.get("freshness_timestamp") is None:
        reasons.append("Freshness regression detected")
    elif result.get("freshness_status") in {"stale", "critical"}:
        reasons.append(
            f"Freshness SLA breached: {result['freshness_status']}"
        )
    if result.get("schema_column_count_change"):
        reasons.append("Schema column count changed")
    return reasons or list(result.get("anomalies", []))


def _metadata_signal_is_neutral(result: dict, severity: str) -> bool:
    status = str(result.get("evaluation_status") or "evaluated").casefold()
    if status in {"unavailable", "skipped", "not_evaluated", "unevaluated"}:
        return True
    return severity == "LOW" and not list(result.get("anomalies") or [])
