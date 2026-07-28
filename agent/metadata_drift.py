from datetime import datetime
from pathlib import Path

from agent.metadata_store import (
    DEFAULT_METADATA_DB_PATH,
    DriftRecord,
    fetch_latest_model_identity,
    fetch_recent_model_metrics,
    insert_metric_drift,
)
from agent.signals import Signal


DRIFT_SIGNAL_CONFIDENCE = {
    "HIGH": 95,
    "MEDIUM": 85,
    "LOW": 75,
}

DRIFT_SIGNAL_SCORES = {
    "HIGH": -35,
    "MEDIUM": -20,
    "LOW": -5,
}

DRIFT_SIGNAL_METADATA_FIELDS = [
    "row_count_change_pct",
    "null_count_change_pct",
    "duplicate_count_change_pct",
    "schema_column_count_change",
    "freshness_regressed",
]


def compare_last_run(
    db_path: str | Path | None = DEFAULT_METADATA_DB_PATH,
    project_name: str | None = None,
    model_name: str | None = None,
) -> dict:
    db_path = db_path or DEFAULT_METADATA_DB_PATH
    resolved_project, resolved_model = _resolve_model_identity(
        db_path, project_name, model_name
    )
    recent_metrics = fetch_recent_model_metrics(
        db_path,
        project_name=resolved_project,
        model_name=resolved_model,
        limit=2,
    )
    if len(recent_metrics) < 2:
        raise ValueError(
            f"Need at least two metric runs for {resolved_project}/{resolved_model}."
        )

    current = recent_metrics[0]
    previous = recent_metrics[1]

    row_count_change_pct = _pct_change(
        previous["row_count"],
        current["row_count"],
    )
    null_count_change_pct = _pct_change(
        previous["null_count"],
        current["null_count"],
    )
    duplicate_count_change_pct = _pct_change(
        previous["duplicate_count"],
        current["duplicate_count"],
    )
    schema_column_count_change = (
        current["schema_column_count"] - previous["schema_column_count"]
    )
    freshness_regressed = _freshness_regressed(
        previous.get("freshness_timestamp"),
        current.get("freshness_timestamp"),
    )
    drift_level = _drift_level(
        row_count_change_pct=row_count_change_pct,
        null_count_change_pct=null_count_change_pct,
        duplicate_count_change_pct=duplicate_count_change_pct,
        schema_column_count_change=schema_column_count_change,
        freshness_regressed=freshness_regressed,
    )
    report_text = _format_drift_report(
        row_count_change_pct=row_count_change_pct,
        null_count_change_pct=null_count_change_pct,
        duplicate_count_change_pct=duplicate_count_change_pct,
        schema_column_count_change=schema_column_count_change,
        freshness_regressed=freshness_regressed,
        drift_level=drift_level,
    )

    insert_metric_drift(
        db_path,
        DriftRecord(
            project_name=resolved_project,
            model_name=resolved_model,
            current_scan_id=current["scan_id"],
            previous_scan_id=previous["scan_id"],
            row_count_change_pct=row_count_change_pct,
            null_count_change_pct=null_count_change_pct,
            duplicate_count_change_pct=duplicate_count_change_pct,
            schema_column_count_change=schema_column_count_change,
            freshness_regressed=freshness_regressed,
            drift_level=drift_level,
            report_text=report_text,
        ),
    )

    return {
        "project_name": resolved_project,
        "model_name": resolved_model,
        "current_scan_id": current["scan_id"],
        "previous_scan_id": previous["scan_id"],
        "row_count_change_pct": row_count_change_pct,
        "null_count_change_pct": null_count_change_pct,
        "duplicate_count_change_pct": duplicate_count_change_pct,
        "schema_column_count_change": schema_column_count_change,
        "freshness_regressed": freshness_regressed,
        "drift_level": drift_level,
        "report_text": report_text,
    }


def format_compare_last_run_report(result: dict) -> str:
    return result["report_text"]


def to_signal(drift_result: dict) -> Signal:
    drift_level = str(drift_result.get("drift_level", "LOW")).upper()
    neutral = _drift_signal_is_neutral(drift_result, drift_level)
    report_text = drift_result.get("report_text", "")
    reasons = [
        line.strip()
        for line in report_text.splitlines()
        if line.strip()
    ]
    if not reasons and not neutral:
        reasons = [f"Metadata Drift: {drift_level}"]

    metadata = {
        field: drift_result.get(field)
        for field in DRIFT_SIGNAL_METADATA_FIELDS
    }
    if drift_result.get("comparison_status") is not None:
        metadata["comparison_status"] = drift_result.get("comparison_status")

    return Signal(
        component="metadata_drift",
        severity=drift_level,
        confidence=DRIFT_SIGNAL_CONFIDENCE.get(drift_level, 75),
        score=0 if neutral else DRIFT_SIGNAL_SCORES.get(drift_level, -5),
        reasons=[] if neutral else reasons,
        metadata=metadata,
    )


def _drift_signal_is_neutral(result: dict, drift_level: str) -> bool:
    status = str(result.get("comparison_status") or "evaluated").casefold()
    if status in {"unavailable", "skipped", "not_evaluated", "unevaluated"}:
        return True
    if drift_level != "LOW":
        return False
    return not any(
        [
            float(result.get("row_count_change_pct") or 0),
            float(result.get("null_count_change_pct") or 0),
            float(result.get("duplicate_count_change_pct") or 0),
            int(result.get("schema_column_count_change") or 0),
            bool(result.get("freshness_regressed")),
        ]
    )


def _resolve_model_identity(
    db_path: str | Path | None,
    project_name: str | None,
    model_name: str | None,
) -> tuple[str, str]:
    db_path = db_path or DEFAULT_METADATA_DB_PATH
    if project_name and model_name:
        return project_name, model_name
    latest = fetch_latest_model_identity(db_path)
    if not latest:
        raise ValueError("No stored model metrics found.")
    latest_project, latest_model = latest
    return project_name or latest_project, model_name or latest_model


def _pct_change(previous: int | None, current: int | None) -> float:
    previous_value = previous or 0
    current_value = current or 0
    if previous_value == 0:
        if current_value == 0:
            return 0.0
        return 100.0
    return round(((current_value - previous_value) / previous_value) * 100, 1)


def _freshness_regressed(previous: str | None, current: str | None) -> bool:
    if not previous or not current:
        return False
    return _parse_timestamp(current) < _parse_timestamp(previous)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _drift_level(
    row_count_change_pct: float,
    null_count_change_pct: float,
    duplicate_count_change_pct: float,
    schema_column_count_change: int,
    freshness_regressed: bool,
) -> str:
    if (
        abs(row_count_change_pct) >= 50
        or abs(null_count_change_pct) >= 50
        or abs(duplicate_count_change_pct) >= 50
        or schema_column_count_change != 0
        or freshness_regressed
    ):
        return "HIGH"
    if (
        abs(row_count_change_pct) >= 20
        or abs(null_count_change_pct) >= 20
        or abs(duplicate_count_change_pct) >= 20
    ):
        return "MEDIUM"
    return "LOW"


def _format_drift_report(
    row_count_change_pct: float,
    null_count_change_pct: float,
    duplicate_count_change_pct: float,
    schema_column_count_change: int,
    freshness_regressed: bool,
    drift_level: str,
) -> str:
    lines = [
        f"Row count change: {_fmt_pct(row_count_change_pct)}",
        f"Null count change: {_fmt_pct(null_count_change_pct)}",
        f"Duplicate count change: {_fmt_pct(duplicate_count_change_pct)}",
        f"Schema column count change: {schema_column_count_change:+d}",
        f"Freshness regression: {'YES' if freshness_regressed else 'NO'}",
        "",
        f"Metadata Drift: {drift_level}",
    ]
    return "\n".join(lines)


def _fmt_pct(value: float) -> str:
    if value == int(value):
        return f"{int(value):+d}%"
    return f"{value:+.1f}%"
