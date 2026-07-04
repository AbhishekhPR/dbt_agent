import re
from enum import Enum
from typing import Any

from agent.decision_engine import DeploymentDecision
from agent.evidence_curation import (
    curate_reasons,
    is_column_level_reason,
    is_supporting_column_reason,
    order_semantic_diff_reasons,
)
from agent.incident import Incident
from agent.reasoning_engine import build_reasoning_report


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    return value


def _top_reasons(incident: Incident) -> list[str]:
    return curate_reasons(incident.signals, max_reasons=5)


def _bullet_list(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {_display_text(item)}" for item in items]


def _business_metric_lines(incident: Incident) -> list[str]:
    for signal in incident.signals:
        if signal.component == "business_metrics":
            return _business_metric_lines_from_metadata(signal.metadata)
    return []


def _semantic_diff_signal(incident: Incident):
    for signal in incident.signals:
        if signal.component == "semantic_diff":
            return signal
    return None


def _column_lineage_lines(incident: Incident) -> list[str]:
    lines = []
    for signal in incident.signals:
        metadata = dict(signal.metadata or {})
        signal_column_lines = []
        for item in metadata.get("column_dependency_changes") or []:
            text = str(item)
            if is_column_level_reason(text) and not is_supporting_column_reason(text):
                signal_column_lines.append(text)
        signal_evidence_lines = [
            str(item)
            for item in metadata.get("column_level_evidence") or []
        ]
        lines.extend(signal_column_lines)
        if signal_column_lines or signal_evidence_lines:
            lines.extend(_lineage_summary_lines(signal))
        lines.extend(signal_evidence_lines)
    return _ordered_unique(line for line in lines if line)


def _semantic_diff_lines(signal) -> list[str]:
    metadata = dict(signal.metadata or {})
    lines = order_semantic_diff_reasons(list(signal.reasons or []))
    lines.extend([
        f"Changed KPIs: {_list_text(metadata.get('changed_kpis'))}",
        f"Added KPIs: {_list_text(metadata.get('added_kpis'))}",
        f"Removed KPIs: {_list_text(metadata.get('removed_kpis'))}",
    ])
    lines.extend(_change_lines("Dependency Changes", metadata.get("dependency_changes") or {}))
    lines.extend(_change_lines("Contract Changes", metadata.get("contract_changes") or {}))
    return lines


def _lineage_summary_lines(signal) -> list[str]:
    if signal.component != "semantic_diff":
        return []
    summaries = []
    for reason in signal.reasons or []:
        match = re.match(
            r"^(?P<kpi>.+?) gained upstream dependency (?P<dependency>.+)$",
            str(reason).strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        kpi = match.group("kpi").strip()
        dependency = match.group("dependency").strip()
        if "refund" in dependency.casefold():
            summaries.append(f"{kpi} lineage now includes refund-related data")
        else:
            summaries.append(f"{kpi} lineage now includes {dependency} data")
    return summaries


def _semantic_diff_snapshot_lines(signal) -> list[str]:
    metadata = dict(signal.metadata or {})
    return [
        f"Previous Snapshot: {_display_text(metadata.get('previous_snapshot_id') or 'None')}",
        f"Current Snapshot: {_display_text(metadata.get('current_snapshot_id') or 'None')}",
    ]


def _business_metric_lines_from_metadata(metadata: dict) -> list[str]:
    spike_percentages = metadata.get("spike_percentages") or {}
    if not spike_percentages:
        return ["Healthy"]
    return [
        f"{_business_metric_label(name)} +{_format_percentage(value)}"
        for name, value in spike_percentages.items()
    ]


def _business_metric_label(name: str) -> str:
    labels = {
        "mis_sorts": "Mis-sorts",
    }
    if name in labels:
        return labels[name]
    return " ".join(part.capitalize() for part in str(name).split("_"))


def _format_percentage(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _list_text(values: Any) -> str:
    items = [str(value) for value in list(values or [])]
    if not items:
        return "None"
    return ", ".join(items)


def _ordered_unique(values) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _change_lines(label: str, changes: dict) -> list[str]:
    lines = []
    for kpi in sorted(changes):
        fields = changes.get(kpi) or {}
        for field_name in sorted(fields):
            detail = fields.get(field_name) or {}
            if not isinstance(detail, dict):
                lines.append(f"{label}: {kpi} {field_name} {_display_text(detail)}")
                continue
            for value in detail.get("added", []) or []:
                lines.append(f"{label}: {kpi} {field_name} added {value}")
            for value in detail.get("removed", []) or []:
                lines.append(f"{label}: {kpi} {field_name} removed {value}")
            if "previous" in detail or "current" in detail:
                lines.append(
                    f"{label}: {kpi} {field_name} changed from "
                    f"{_display_text(detail.get('previous'))} to "
                    f"{_display_text(detail.get('current'))}"
                )
    if not lines:
        return [f"{label}: None"]
    return lines


def _snapshot_value(line: str) -> str:
    return _display_text(line.split(": ", 1)[1] if ": " in line else line)


def render_cli(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
    business_metric_lines = _business_metric_lines(incident)
    semantic_diff_signal = _semantic_diff_signal(incident)
    column_lineage_lines = _column_lineage_lines(incident)
    lines = [
        "Relium Deployment Decision",
        "",
        f"Pipeline Health: {_health_text(incident.health)}",
        f"Deployment Decision: {_decision_label(incident.decision)}",
        f"Severity: {_enum_value(incident.severity)}",
        f"Confidence: {_confidence_text(incident.confidence)}",
        "",
        f"Primary Root Cause: {_display_text(incident.root_cause or 'None')}",
        "",
        "Top Reasons:",
        *_bullet_list(_top_reasons(incident)),
        "",
        f"Recommendation: {_display_text(incident.recommendation or 'None')}",
        "",
        "Signals Considered:",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if business_metric_lines:
        lines.extend([
            "",
            "Business Metrics",
            *_bullet_list(business_metric_lines),
        ])

    if semantic_diff_signal is not None:
        lines.extend([
            "",
            "Historical Semantic Change",
            *_bullet_list([
                *_semantic_diff_lines(semantic_diff_signal),
                *_semantic_diff_snapshot_lines(semantic_diff_signal),
            ]),
        ])

    if column_lineage_lines:
        lines.extend([
            "",
            "Column-Level Lineage",
            *_bullet_list(column_lineage_lines),
        ])

    if incident.affected_models:
        lines.extend([
            "",
            "Affected Models:",
            *_bullet_list(list(incident.affected_models)),
        ])

    lines.extend([
        "",
        "Reasoning:",
        "",
        f"Executive Summary: {_display_text(reasoning.executive_summary)}",
        "",
        "Evidence:",
        *_format_cli_evidence(reasoning.evidence),
        "",
        f"Conclusion: {_display_text(reasoning.conclusion)}",
        "",
        f"Recommendation: {_display_text(reasoning.recommendation)}",
    ])

    return "\n".join(lines)


def render_markdown(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
    business_metric_lines = _business_metric_lines(incident)
    semantic_diff_signal = _semantic_diff_signal(incident)
    column_lineage_lines = _column_lineage_lines(incident)
    lines = [
        "# Relium Deployment Decision",
        "",
        "## Pipeline Health",
        _health_text(incident.health),
        "",
        "## Deployment Decision",
        _decision_label(incident.decision),
        "",
        "## Severity",
        str(_enum_value(incident.severity)),
        "",
        "## Confidence",
        _confidence_text(incident.confidence),
        "",
        "## Primary Root Cause",
        _display_text(incident.root_cause or "None"),
        "",
        "## Top Reasons",
        *_bullet_list(_top_reasons(incident)),
        "",
        "## Recommendation",
        _display_text(incident.recommendation or "None"),
        "",
        "## Signals Considered",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if business_metric_lines:
        lines.extend([
            "",
            "## Business Metrics",
            *_bullet_list(business_metric_lines),
        ])

    if semantic_diff_signal is not None:
        previous_snapshot, current_snapshot = _semantic_diff_snapshot_lines(semantic_diff_signal)
        lines.extend([
            "",
            "### Historical Semantic Change",
            *_bullet_list(_semantic_diff_lines(semantic_diff_signal)),
            "",
            f"**Previous Snapshot:** {_snapshot_value(previous_snapshot)}  ",
            f"**Current Snapshot:** {_snapshot_value(current_snapshot)}",
        ])

    if column_lineage_lines:
        lines.extend([
            "",
            "## Column-Level Lineage",
            *_bullet_list(column_lineage_lines),
        ])

    if incident.affected_models:
        lines.extend([
            "",
            "## Affected Models",
            *_bullet_list(list(incident.affected_models)),
        ])

    lines.extend([
        "",
        "## Reasoning",
        "",
        "### Executive Summary",
        _display_text(reasoning.executive_summary),
        "",
        "### Evidence",
        *_format_markdown_evidence(reasoning.evidence),
        "",
        "### Conclusion",
        _display_text(reasoning.conclusion),
        "",
        "### Recommendation",
        _display_text(reasoning.recommendation),
    ])

    return "\n".join(lines)


def render_json(incident: Incident) -> dict:
    return {
        "incident_id": incident.incident_id,
        "health": incident.health,
        "decision": _enum_value(incident.decision),
        "severity": _enum_value(incident.severity),
        "confidence": incident.confidence,
        "root_cause": incident.root_cause,
        "recommendation": incident.recommendation,
        "signal_count": len(incident.signals),
        "signal_components": [
            signal.component for signal in incident.signals
        ],
        "top_reasons": _top_reasons(incident),
        "affected_models": list(incident.affected_models),
        "metadata": _serialize(dict(incident.metadata)),
    }


def _format_cli_evidence(evidence) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- {_display_text(item.title)} "
            f"(severity: {item.severity}, confidence: {item.confidence})"
        )
        for item in evidence
    ]


def _format_markdown_evidence(evidence) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- **{_display_text(item.title)}** "
            f"(severity: {item.severity}, confidence: {item.confidence})"
        )
        for item in evidence
    ]


def _health_text(health: int) -> str:
    return f"{health} / 100"


def _confidence_text(confidence: int) -> str:
    return f"{confidence}%"


def _decision_label(decision: Any) -> str:
    value = _enum_value(decision)
    if value == DeploymentDecision.BLOCK.value:
        return "BLOCK DEPLOYMENT"
    return str(value)


def _display_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=with\b)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()
