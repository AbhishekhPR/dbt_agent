import json
from datetime import datetime, timezone
from pathlib import Path

from agent.presentation import render_markdown


DEFAULT_REPORT_PATH = Path("pipeline_validation_report.md")
DEFAULT_JSON_REPORT_PATH = Path("pipeline_validation_report.json")


def format_pipeline_validation_report(result: dict) -> str:
    drift = result.get("drift_result")
    safe_to_merge = bool(result.get("safe_to_continue"))
    drift_level = _drift_level_text(drift)
    primary_reason = _primary_reason(result, drift)
    generated = result.get("generated_timestamp") or _generated_timestamp()
    affected_models = result.get("affected_models") or []

    lines = [
        "# Relium Pipeline Validation Report",
        "",
        "## Executive Summary",
        "",
        f"Decision: {'✅ SAFE TO MERGE' if safe_to_merge else '❌ BLOCK DEPLOYMENT'}",
        "",
        f"Risk Level: {result.get('severity', 'UNKNOWN')}",
        "",
        "Changed Model:",
        result.get("changed_model") or result.get("model_name", "Unknown"),
        "",
        "Downstream Impact:",
        _format_list_inline(affected_models, empty="None identified"),
        "",
        "Metadata Drift:",
        drift_level,
        "",
        "Primary Reason:",
        primary_reason,
        "",
        "Generated:",
        generated,
        "",
        "------------------------------------------------",
        "",
        f"Generated timestamp: {generated}",
        "",
        f"Project: {result.get('project_name', 'Unknown')}",
        "",
        f"Model: {result.get('model_name', 'Unknown')}",
        "",
        f"Run ID: {result.get('scan_id') or 'Not available'}",
        "",
        "------------------------------------------------",
        "",
        "## Static Analysis",
        "",
        f"Risk Level: {result.get('severity', 'UNKNOWN')}",
        "",
        f"Safe To Merge: {'YES' if safe_to_merge else 'NO'}",
        "",
        "Detected SQL risks:",
        _format_sql_risks(result),
        "",
        f"Confidence: {_confidence(result)}",
        "",
        f"Changed model: {result.get('changed_model') or result.get('model_name', 'Unknown')}",
        "",
        "Affected downstream models:",
        _format_bullets(affected_models, empty="- None identified"),
        "",
        f"Recommendation: {result.get('recommendation') or 'Review the SQL transformation before deployment.'}",
        "",
        "------------------------------------------------",
        "",
        "## Metadata Validation",
        "",
        f"Row count: {result.get('row_count', 'Unknown')}",
        "",
        f"Null count: {result.get('null_count', 'Unknown')}",
        "",
        f"Duplicate count: {result.get('duplicate_count', 'Unknown')}",
        "",
        f"Freshness timestamp: {result.get('freshness_timestamp') or 'Not available'}",
        "",
        f"Schema column count: {result.get('schema_column_count', 'Unknown')}",
        "",
        "------------------------------------------------",
        "",
        "## Historical Drift",
        "",
        _format_historical_drift(drift),
        "",
        "------------------------------------------------",
        "",
        _format_deployment_decision_section(result, safe_to_merge, primary_reason),
        "",
        "------------------------------------------------",
        "",
        "## Recommended Actions",
        "",
        _recommended_actions(result, drift),
        "",
    ]
    return "\n".join(lines)


def write_pipeline_validation_report(
    result: dict,
    path: str | Path = DEFAULT_REPORT_PATH,
) -> Path:
    if not result.get("generated_timestamp"):
        result = {**result, "generated_timestamp": _generated_timestamp()}
    report_path = Path(path)
    report_path.write_text(format_pipeline_validation_report(result), encoding="utf-8")
    write_pipeline_validation_json(result, DEFAULT_JSON_REPORT_PATH)
    return report_path


def format_pipeline_validation_json(result: dict) -> dict:
    drift = result.get("drift_result")
    safe_to_continue = bool(result.get("safe_to_continue"))
    generated_at = result.get("generated_timestamp") or _generated_timestamp()
    return {
        "generated_at": generated_at,
        "project": result.get("project_name"),
        "model": result.get("model_name"),
        "scenario": result.get("scenario"),
        "run_id": result.get("scan_id"),
        "risk_level": result.get("severity"),
        "safe_to_continue": safe_to_continue,
        "static_analysis": {
            "risk_level": result.get("severity"),
            "safe_to_merge": safe_to_continue,
            "detected_sql_risks": _sql_risk_items(result),
            "confidence": _confidence(result),
            "changed_model": result.get("changed_model") or result.get("model_name"),
            "affected_downstream_models": result.get("affected_models") or [],
            "recommendation": result.get("recommendation")
            or "Review the SQL transformation before deployment.",
        },
        "metadata_checks": {
            "row_count": result.get("row_count"),
            "null_count": result.get("null_count"),
            "duplicate_count": result.get("duplicate_count"),
            "freshness_timestamp": result.get("freshness_timestamp"),
            "schema_column_count": result.get("schema_column_count"),
        },
        "drift_detection": _json_drift_detection(drift),
        "final_decision": {
            "safe_to_merge": safe_to_continue,
            "decision": "SAFE_TO_MERGE" if safe_to_continue else "BLOCK_DEPLOYMENT",
            "reason": _primary_reason(result, drift),
        },
        "recommended_actions": _recommended_action_items(result, drift),
    }


def write_pipeline_validation_json(
    result: dict,
    path: str | Path = DEFAULT_JSON_REPORT_PATH,
) -> Path:
    report_path = Path(path)
    report_path.write_text(
        json.dumps(format_pipeline_validation_json(result), indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def _generated_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M UTC")


def _format_deployment_decision_section(
    result: dict,
    safe_to_merge: bool,
    primary_reason: str,
) -> str:
    incident = result.get("incident")
    if incident:
        return _format_incident_markdown_for_report(incident)
    return "\n".join(
        [
            "## Final Decision",
            "",
            "SAFE TO MERGE:",
            "YES" if safe_to_merge else "NO",
            "",
            "Reason",
            primary_reason,
        ]
    )


def _format_incident_markdown_for_report(incident) -> str:
    rendered = render_markdown(incident)
    rendered = rendered.replace(
        f"## Pipeline Health\n{incident.health}",
        f"## Pipeline Health\n{incident.health} / 100",
        1,
    )
    rendered = rendered.replace(
        f"## Confidence\n{incident.confidence}",
        f"## Confidence\n{incident.confidence}%",
        1,
    )
    return rendered


def _format_sql_risks(result: dict) -> str:
    return "\n".join(f"- {item}" for item in _sql_risk_items(result))


def _sql_risk_items(result: dict) -> list[str]:
    bugs = result.get("sql_risks") or []
    items = []
    for bug in bugs:
        items.append(
            bug.get("description") or bug.get("category") or "SQL risk detected"
        )
    if items:
        return items
    text = result.get("static_analysis_text")
    if text:
        return [text]
    return ["None detected"]


def _confidence(result: dict) -> str:
    bugs = result.get("sql_risks") or []
    values = [bug.get("confidence") for bug in bugs if bug.get("confidence")]
    if values:
        return ", ".join(str(value).upper() for value in values)
    return "Not available"


def _format_historical_drift(drift: dict | None) -> str:
    if not drift:
        return "Not enough historical runs yet."
    return "\n".join(
        [
            f"Previous run timestamp: {drift.get('previous_run_timestamp') or 'Not available'}",
            "",
            f"Current run timestamp: {drift.get('current_run_timestamp') or 'Not available'}",
            "",
            f"Row count change: {_fmt_pct(drift.get('row_count_change_pct', 0.0))}",
            "",
            f"Null count change: {_fmt_pct(drift.get('null_count_change_pct', 0.0))}",
            "",
            f"Duplicate count change: {_fmt_pct(drift.get('duplicate_count_change_pct', 0.0))}",
            "",
            f"Schema column count change: {int(drift.get('schema_column_count_change', 0)):+d}",
            "",
            f"Freshness regression: {'YES' if drift.get('freshness_regressed') else 'NO'}",
            "",
            f"Overall Metadata Drift: {drift.get('drift_level', 'LOW')}",
        ]
    )


def _primary_reason(result: dict, drift: dict | None) -> str:
    if drift and abs(drift.get("duplicate_count_change_pct", 0.0)) >= 50:
        return (
            "Duplicate customer_id count increased by "
            f"{_fmt_pct(drift.get('duplicate_count_change_pct', 0.0))}."
        )
    if drift and drift.get("freshness_regressed"):
        return "Freshness timestamp regressed compared with the previous run."
    if result.get("severity") in {"HIGH", "CRITICAL"}:
        return "Static analysis risk level is HIGH."
    if result.get("duplicate_count", 0):
        return f"Duplicate customer_id count is {result.get('duplicate_count')}."
    return "All validation signals are within the configured acceptance criteria."


def _recommended_actions(result: dict, drift: dict | None) -> str:
    return "\n".join(f"- {action}" for action in _recommended_action_items(result, drift))


def _recommended_action_items(result: dict, drift: dict | None) -> list[str]:
    actions = []
    if result.get("severity") in {"HIGH", "CRITICAL"}:
        actions.append("Review the SQL transformation before deployment.")
    if result.get("duplicate_count", 0):
        actions.append("Investigate duplicate customer_id records in the model output.")
    if drift and drift.get("drift_level") == "HIGH":
        actions.append("Compare the current run against the previous baseline before merging.")
    if not actions:
        actions.append("Proceed with standard deployment review.")
    return actions


def _json_drift_detection(drift: dict | None) -> dict:
    if not drift:
        return {
            "status": "not_enough_history",
            "previous_run_timestamp": None,
            "current_run_timestamp": None,
            "row_count_change_pct": None,
            "null_count_change_pct": None,
            "duplicate_count_change_pct": None,
            "schema_column_count_change": None,
            "freshness_regression": None,
            "overall_metadata_drift": None,
        }
    return {
        "status": "available",
        "previous_run_timestamp": drift.get("previous_run_timestamp"),
        "current_run_timestamp": drift.get("current_run_timestamp"),
        "row_count_change_pct": drift.get("row_count_change_pct"),
        "null_count_change_pct": drift.get("null_count_change_pct"),
        "duplicate_count_change_pct": drift.get("duplicate_count_change_pct"),
        "schema_column_count_change": drift.get("schema_column_count_change"),
        "freshness_regression": bool(drift.get("freshness_regressed")),
        "overall_metadata_drift": drift.get("drift_level"),
    }


def _drift_level_text(drift: dict | None) -> str:
    if not drift:
        return "Not enough historical runs yet."
    return drift.get("drift_level", "LOW")


def _format_list_inline(values: list[str], empty: str) -> str:
    if not values:
        return empty
    return ", ".join(values)


def _format_bullets(values: list[str], empty: str) -> str:
    if not values:
        return empty
    return "\n".join(f"- {value}" for value in values)


def _fmt_pct(value: float) -> str:
    if value == int(value):
        return f"{int(value):+d}%"
    return f"{value:+.1f}%"
