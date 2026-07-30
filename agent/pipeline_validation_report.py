import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agent.presentation import render_markdown


class PipelineReportError(ValueError):
    """Raised when demo reports cannot be serialized or replaced safely."""


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
    markdown_path: str | Path,
    json_path: str | Path,
) -> tuple[Path, Path]:
    if not result.get("generated_timestamp"):
        result = {**result, "generated_timestamp": _generated_timestamp()}
    try:
        markdown = format_pipeline_validation_report(result)
        json_text = (
            json.dumps(
                format_pipeline_validation_json(result),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as error:
        raise PipelineReportError(f"Could not serialize demo reports: {error}") from error

    markdown_target = Path(markdown_path)
    json_target = Path(json_path)
    try:
        previous_markdown = (
            markdown_target.read_bytes() if markdown_target.exists() else None
        )
        if json_target.exists():
            json_target.read_bytes()
    except OSError as error:
        raise PipelineReportError(f"Could not read existing demo reports: {error}") from error

    staged_markdown = None
    staged_json = None
    markdown_replaced = False
    replacement_error = None
    rollback_error = None
    try:
        staged_markdown = _stage_report(markdown_target, markdown)
        staged_json = _stage_report(json_target, json_text)
        os.replace(staged_markdown, markdown_target)
        staged_markdown = None
        markdown_replaced = True
        os.replace(staged_json, json_target)
        staged_json = None
    except OSError as error:
        replacement_error = error
        if markdown_replaced:
            try:
                _restore_report(markdown_target, previous_markdown)
            except OSError as restore_error:
                rollback_error = restore_error
    cleanup_errors = [
        error
        for error in (
            _remove_staged_report(staged_markdown),
            _remove_staged_report(staged_json),
        )
        if error is not None
    ]
    if replacement_error is not None:
        message = f"Could not replace demo reports safely: {replacement_error}"
        if rollback_error is not None:
            message += f"; Markdown rollback also failed: {rollback_error}"
        if cleanup_errors:
            message += "; temporary report cleanup also failed: " + "; ".join(
                str(error) for error in cleanup_errors
            )
        raise PipelineReportError(message) from replacement_error
    if cleanup_errors:
        raise PipelineReportError(
            "Demo reports were replaced, but temporary report cleanup failed: "
            + "; ".join(str(error) for error in cleanup_errors)
        )

    return markdown_target, json_target


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


def _stage_report(target: Path, content: str) -> Path:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path
    except OSError as error:
        cleanup_error = _remove_staged_report(temporary_path)
        if cleanup_error is not None:
            raise OSError(
                f"{error}; temporary report cleanup also failed: {cleanup_error}"
            ) from error
        raise


def _restore_report(target: Path, previous: bytes | None) -> None:
    if previous is None:
        target.unlink(missing_ok=True)
        return

    temporary_path = None
    restore_error = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".rollback.tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError as error:
        restore_error = error
    cleanup_error = _remove_staged_report(temporary_path)
    if restore_error is not None:
        message = str(restore_error)
        if cleanup_error is not None:
            message += f"; rollback cleanup also failed: {cleanup_error}"
        raise OSError(message) from restore_error
    if cleanup_error is not None:
        raise cleanup_error


def _remove_staged_report(path: Path | None) -> OSError | None:
    if path is None:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return error
    return None


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
