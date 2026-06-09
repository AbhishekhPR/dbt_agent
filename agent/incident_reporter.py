from datetime import datetime
from pathlib import Path
import re


INCIDENTS_DIR = Path("incidents")


def create_incident_report(project_name: str, anomaly: dict, rca_report: dict, table_name: str | None = None) -> str:
    """
    Write a metadata-only RCA incident report and return its path.
    """
    INCIDENTS_DIR.mkdir(exist_ok=True)

    table = _resolve_table_name(anomaly, rca_report, table_name)
    anomaly_type = anomaly.get("type", anomaly.get("anomaly", "unknown"))
    generated_at = datetime.utcnow()
    timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
    filename = (
        f"{_slug(project_name)}_{_slug(table)}_"
        f"{_slug(anomaly_type)}_{timestamp}.md"
    )
    path = INCIDENTS_DIR / filename

    path.write_text(
        _render_report(project_name, anomaly, rca_report, generated_at, table),
        encoding="utf-8",
    )
    return path.as_posix()


def _resolve_table_name(anomaly: dict, rca_report: dict, table_name: str | None = None) -> str:
    return anomaly.get("table") or rca_report.get("table") or table_name or "unknown"


def _render_report(project_name: str, anomaly: dict, rca_report: dict, generated_at: datetime, table_name: str | None = None) -> str:
    causes = rca_report.get("likely_causes") or []
    primary = causes[0] if causes else {}
    alternatives = causes[1:]
    affected_models = rca_report.get("affected_models") or []
    actions = rca_report.get("recommended_actions") or []
    impact_count = rca_report.get("impact_count", len(affected_models))
    table = _resolve_table_name(anomaly, rca_report, table_name)
    anomaly_type = anomaly.get("type", anomaly.get("anomaly", "unknown"))
    severity = anomaly.get("severity", "unknown")
    primary_cause = _sentence_case(primary.get("cause", "No strong RCA evidence"))
    metric_evidence = _extract_metric_evidence(anomaly)
    investigation_steps = _investigation_steps(table, actions, anomaly_type)

    return "\n".join([
        "# Relium Incident Report",
        "",
        "## Incident Summary",
        "",
        f"Project: {project_name}  ",
        f"Table: {table}  ",
        f"Anomaly Type: {anomaly_type}  ",
        f"Severity: {severity}  ",
        f"Data Loss Risk: {'yes' if severity == 'critical' else 'no'}  ",
        f"Generated At: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}  ",
        "",
        "## Executive Summary",
        "",
        f"Relium detected a {severity} {_human_anomaly(anomaly_type)} in {table}.",
        "",
        _executive_impact_sentence(anomaly_type, table, metric_evidence),
        "",
        f"Primary hypothesis: {primary.get('cause', 'No strong RCA evidence')}.",
        "",
        f"{impact_count} downstream model(s) may be affected.",
        "",
        "## Metric Evidence",
        "",
        f"Expected rows: {metric_evidence['expected']}  ",
        f"Observed rows: {metric_evidence['observed']}  ",
        f"Change: {metric_evidence['change']}  ",
        "",
        "Anomaly message:",
        anomaly.get("message", "N/A"),
        "",
        "Detail:",
        anomaly.get("detail", "N/A"),
        "",
        "Impact:",
        anomaly.get("impact", "N/A"),
        "",
        "## Root Cause Analysis",
        "",
        "Primary hypothesis:",
        primary_cause,
        "",
        "Confidence:",
        _format_confidence(primary.get("confidence")),
        "",
        "Status:",
        "Primary hypothesis based on metadata evidence. Not yet confirmed by source system logs.",
        "",
        "Reason:",
        _primary_reason(table, anomaly_type, primary),
        "",
        "## Alternative Hypotheses",
        "",
        _format_alternatives(alternatives),
        "",
        "## Blast Radius",
        "",
        f"Total affected models: {impact_count}",
        "",
        "Affected models:",
        "",
        _format_bullets(affected_models) if affected_models else "- None found",
        "",
        "Interpretation:",
        _blast_radius_interpretation(table, anomaly_type),
        "",
        "## Recommended Investigation Steps",
        "",
        _format_numbered(investigation_steps),
        "",
        "## Suggested Owner Action",
        "",
        _owner_first_action(table, anomaly_type),
        "",
        _owner_investigation_priority(table, anomaly_type),
        "",
        "## Compliance Note",
        "",
        "This report was generated using metadata only.",
        "",
        "Relium did not access customer records, raw table data, query results, emails, names, or PII.",
        "",
        "Only the following metadata was used:",
        "",
        "- row counts",
        "- anomaly details",
        "- dependency graph",
        "- SQL structure metadata",
        "- blast radius metadata",
        "",
    ])


def _format_alternatives(causes: list) -> str:
    if not causes:
        return "No alternative hypotheses identified."

    lines = []
    for idx, cause in enumerate(causes, 1):
        lines.extend([
            f"{idx}. {_sentence_case(cause.get('cause', 'unknown'))}  ",
            f"   Confidence: {_format_confidence(cause.get('confidence'))}  ",
            f"   Reason: {_sentence_case(cause.get('reason', 'N/A'))}",
            "",
        ])
    return "\n".join(lines).rstrip()


def _format_bullets(items: list) -> str:
    return "\n".join(f"- {item}" for item in items)


def _format_numbered(items: list) -> str:
    return "\n".join(f"{idx}. {item}" for idx, item in enumerate(items, 1))


def _format_confidence(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f}"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "unknown"))
    return cleaned.strip("_").lower() or "unknown"


def _sentence_case(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _extract_metric_evidence(anomaly: dict) -> dict:
    detail = anomaly.get("detail", "")
    message = anomaly.get("message", "")
    expected = "N/A"
    observed = "N/A"
    change = "N/A"

    detail_match = re.search(r"Expected\s+(~?\d+(?:\.\d+)?)\s+rows,\s+got\s+(\d+(?:\.\d+)?)", detail)
    if detail_match:
        expected = detail_match.group(1)
        observed = detail_match.group(2)

    message_match = re.search(r"dropped by\s+(\d+(?:\.\d+)?)%", message, re.IGNORECASE)
    if message_match:
        change = f"-{message_match.group(1)}%"
    else:
        spike_match = re.search(r"(?:spiked|increased) by\s+(\d+(?:\.\d+)?)%", message, re.IGNORECASE)
        if spike_match:
            change = f"+{spike_match.group(1)}%"

    return {"expected": expected, "observed": observed, "change": change}


def _human_anomaly(anomaly_type: str) -> str:
    if anomaly_type == "row_count_anomaly":
        return "row-count anomaly"
    return anomaly_type.replace("_", " ")


def _executive_impact_sentence(anomaly_type: str, table: str, metric_evidence: dict) -> str:
    if anomaly_type == "row_count_anomaly":
        expected = metric_evidence.get("expected", "the expected baseline")
        observed = metric_evidence.get("observed", "the observed value")
        change = metric_evidence.get("change", "an unknown change").lstrip("-")
        return (
            f"{table} dropped from {expected} expected rows to {observed} observed rows, "
            f"a {change} decrease."
        )
    return (
        f"The anomaly in {table} may affect downstream analytics models that depend on this table."
    )


def _primary_reason(table: str, anomaly_type: str, primary: dict) -> str:
    if anomaly_type == "row_count_anomaly":
        return (
            f"The {table} table experienced a sharp row-count drop compared to the baseline. "
            "Since this table is a raw/source-level dependency, downstream models are likely "
            "inheriting the issue rather than causing it."
        )
    return _sentence_case(primary.get("reason", "No deterministic reason available."))


def _blast_radius_interpretation(table: str, anomaly_type: str) -> str:
    if anomaly_type == "freshness_anomaly":
        return (
            f"These models either directly or indirectly depend on {table}. "
            f"If {table} is stale, downstream models may be using outdated data."
        )
    return (
        f"These models either directly or indirectly depend on {table}. "
        f"If {table} is incomplete, these downstream models may produce incorrect metrics."
    )


def _owner_first_action(table: str, anomaly_type: str) -> str:
    if anomaly_type == "freshness_anomaly":
        return (
            f"First action: Verify whether the scheduled ingestion job for {table} "
            "completed successfully and updated the table within the expected freshness window."
        )
    return (
        f"First action: Verify whether the upstream ingestion job for {table} "
        "completed successfully and loaded the expected number of rows."
    )


def _owner_investigation_priority(table: str, anomaly_type: str) -> str:
    if anomaly_type == "freshness_anomaly":
        return (
            "Investigation priority: Start with ingestion schedule, source connector status, "
            "and orchestration logs before debugging downstream models."
        )
    return (
        f"Investigation priority: Start at {table} before debugging downstream models, "
        "because the affected models appear to inherit the anomaly from the raw table layer."
    )


def _investigation_steps(table: str, actions: list, anomaly_type: str = "unknown") -> list:
    if actions:
        normalized = [_sentence_case(action) for action in actions]
    else:
        normalized = [f"Investigate upstream pipeline health for {table}."]

    if anomaly_type == "freshness_anomaly":
        return normalized[:6]

    desired = [
        f"Check the upstream ingestion job for {table}.",
        f"Compare the latest {table} row count with the previous successful run.",
        "Verify whether the source table was partially loaded or truncated.",
        "Review recent WHERE clause or filter changes.",
        f"Inspect downstream joins only if {table} appears healthy.",
    ]

    merged = []
    for item in desired + normalized:
        if item not in merged:
            merged.append(item)
    return merged[:5]
