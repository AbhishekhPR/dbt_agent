from enum import Enum
from typing import Any

from agent.evidence_curation import curate_reasons, order_semantic_diff_reasons
from agent.incident import Incident
from agent.reasoning_engine import build_reasoning_report
from agent.redaction import redact_sensitive_data, redact_text


REVIEW_TITLE = "Relium AI Deployment Review"


def build_pr_review(incident: Incident) -> dict[str, Any]:
    reasoning = build_reasoning_report(incident)
    material_reasons = curate_reasons(incident.signals)
    model_names = _model_names(incident)
    business_metrics = _business_metrics(incident)
    historical_semantic_change = _historical_semantic_change(incident)

    review = {
        "title": REVIEW_TITLE,
        "incident_id": incident.incident_id,
        "deployment_decision": _enum_value(incident.decision),
        "pipeline_health": f"{incident.health} / 100",
        "health": incident.health,
        "confidence": f"{incident.confidence}%",
        "confidence_percent": incident.confidence,
        "models_reviewed": len(model_names),
        "model_names": model_names,
        "highest_severity": _enum_value(incident.severity),
        "primary_root_cause": redact_text(
            _material_root_cause(incident.root_cause, material_reasons)
        ),
        "executive_summary": redact_text(reasoning.executive_summary),
        "evidence": [
            {
                "title": redact_text(item.title),
                "explanation": redact_text(item.explanation),
                "severity": item.severity,
                "confidence": item.confidence,
                "supporting_metadata": redact_sensitive_data(
                    _serialize(dict(item.supporting_metadata))
                ),
            }
            for item in reasoning.evidence
        ],
        "recommendation": redact_text(reasoning.recommendation),
        "business_metrics": business_metrics,
        "business_metric_keys": _business_metric_keys(incident),
        "signals_considered": [
            {
                "component": signal.component,
                "severity": _enum_value(signal.severity),
                "confidence": signal.confidence,
                "score": signal.score,
                "reasons": redact_sensitive_data(list(signal.reasons)),
                "metadata": redact_sensitive_data(_serialize(dict(signal.metadata))),
            }
            for signal in incident.signals
        ],
    }
    if historical_semantic_change is not None:
        review["historical_semantic_change"] = historical_semantic_change
    return review


def render_pr_review_markdown(review: dict) -> str:
    lines = [
        "## Relium AI Deployment Review",
        "",
        f"**Deployment Decision:** {_review_text(review, 'deployment_decision')}",
        f"**Pipeline Health:** {_review_text(review, 'pipeline_health')}",
        f"**Confidence:** {_review_text(review, 'confidence')}",
        f"**Models Reviewed:** {_review_text(review, 'models_reviewed')}",
        f"**Highest Severity:** {_review_text(review, 'highest_severity')}",
    ]
    if (
        _review_text(review, "deployment_decision") == "ALLOW"
        and not review.get("evidence")
    ):
        lines.extend([
            "",
            "No material deployment risks detected.",
        ])
    else:
        lines.extend([
            "",
            "### Primary Root Cause",
            _review_text(review, "primary_root_cause", "None"),
        ])
    lines.extend([
        "",
        "### Executive Summary",
        _review_text(review, "executive_summary", "None"),
        "",
        "### Evidence",
        *_evidence_lines(review.get("evidence", [])),
        "",
        "### Recommendation",
        _review_text(review, "recommendation", "None"),
        "",
        *_business_metric_section(
            review.get("business_metrics", []),
            review.get("business_metric_keys", []),
        ),
        *_historical_semantic_change_section(
            review.get("historical_semantic_change"),
        ),
        "### Signals Considered",
        *_signal_lines(review.get("signals_considered", [])),
    ])
    return "\n".join(lines)


def _business_metric_section(lines: list[str], metric_keys: list[str]) -> list[str]:
    if not lines:
        return []
    section = [
        "### Business Metrics",
        *_bullet_lines(lines),
    ]
    if metric_keys:
        section.extend([
            "",
            "Metric keys: " + ", ".join(f"`{key}`" for key in metric_keys),
        ])
    section.append("")
    return section


def _historical_semantic_change_section(change: dict | None) -> list[str]:
    if not change:
        return []
    section = [
        "### Historical Semantic Change",
        *_bullet_lines(_historical_semantic_change_lines(change)),
        "",
        f"**Previous Snapshot:** {_text(change.get('previous_snapshot_id'), 'None')}  ",
        f"**Current Snapshot:** {_text(change.get('current_snapshot_id'), 'None')}",
        "",
    ]
    return section


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def _business_metrics(incident: Incident) -> list[str]:
    for signal in incident.signals:
        if signal.component == "business_metrics":
            return _business_metric_lines_from_metadata(signal.metadata)
    return []


def _historical_semantic_change(incident: Incident) -> dict[str, Any] | None:
    for signal in incident.signals:
        if signal.component != "semantic_diff":
            continue
        metadata = dict(signal.metadata or {})
        return {
            "changed_kpis": _serialize(list(metadata.get("changed_kpis") or [])),
            "added_kpis": _serialize(list(metadata.get("added_kpis") or [])),
            "removed_kpis": _serialize(list(metadata.get("removed_kpis") or [])),
            "dependency_changes": _serialize(dict(metadata.get("dependency_changes") or {})),
            "contract_changes": _serialize(dict(metadata.get("contract_changes") or {})),
            "previous_snapshot_id": _serialize(metadata.get("previous_snapshot_id")),
            "current_snapshot_id": _serialize(metadata.get("current_snapshot_id")),
            "reasons": order_semantic_diff_reasons(list(signal.reasons or [])),
        }
    return None


def _historical_semantic_change_lines(change: dict) -> list[str]:
    lines = list(change.get("reasons") or [])
    lines.extend([
        f"Changed KPIs: {_list_text(change.get('changed_kpis'))}",
        f"Added KPIs: {_list_text(change.get('added_kpis'))}",
        f"Removed KPIs: {_list_text(change.get('removed_kpis'))}",
    ])
    lines.extend(_change_lines("Dependency Changes", change.get("dependency_changes") or {}))
    lines.extend(_change_lines("Contract Changes", change.get("contract_changes") or {}))
    return lines


def _list_text(values: Any) -> str:
    items = [str(value) for value in list(values or [])]
    if not items:
        return "None"
    return ", ".join(items)


def _change_lines(label: str, changes: dict) -> list[str]:
    lines = []
    for kpi in sorted(changes):
        fields = changes.get(kpi) or {}
        for field_name in sorted(fields):
            detail = fields.get(field_name) or {}
            if not isinstance(detail, dict):
                lines.append(f"{label}: {kpi} {field_name} {detail}")
                continue
            for value in detail.get("added", []) or []:
                lines.append(f"{label}: {kpi} {field_name} added {value}")
            for value in detail.get("removed", []) or []:
                lines.append(f"{label}: {kpi} {field_name} removed {value}")
            if "previous" in detail or "current" in detail:
                lines.append(
                    f"{label}: {kpi} {field_name} changed from "
                    f"{_text(detail.get('previous'))} to {_text(detail.get('current'))}"
                )
    if not lines:
        return [f"{label}: None"]
    return lines


def _business_metric_keys(incident: Incident) -> list[str]:
    for signal in incident.signals:
        if signal.component == "business_metrics":
            spike_percentages = signal.metadata.get("spike_percentages") or {}
            if spike_percentages:
                return list(spike_percentages)
            metrics = signal.metadata.get("metrics") or {}
            return [key for key in metrics if key != "total_events"]
    return []


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


def _model_names(incident: Incident) -> list[str]:
    names = []
    for model_name in incident.affected_models:
        _append_unique(names, model_name)
    for signal in incident.signals:
        _append_unique(names, signal.metadata.get("model_name"))
    return names


def _material_root_cause(root_cause: Any, material_reasons: list[str]) -> str:
    root = str(root_cause or "").strip()
    if root and root in material_reasons:
        return root
    return material_reasons[0] if material_reasons else ""


def _evidence_lines(evidence: list[dict]) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- **{_text(item.get('title'), 'Evidence')}** "
            f"(severity: {_text(item.get('severity'))}, "
            f"confidence: {_text(item.get('confidence'))}%)"
        )
        for item in evidence
    ]


def _signal_lines(signals: list[dict]) -> list[str]:
    if not signals:
        return ["- None"]
    return [
        (
            f"- **{_text(signal.get('component'), 'signal')}** "
            f"(severity: {_text(signal.get('severity'))}, "
            f"confidence: {_text(signal.get('confidence'))}%)"
        )
        for signal in signals
    ]


def _review_text(review: dict, key: str, default: str = "") -> str:
    return _text(review.get(key), default)


def _text(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _append_unique(values: list[str], value: Any) -> None:
    if value is None:
        return
    text = str(value)
    if text and text not in values:
        values.append(text)


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


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value
