from enum import Enum
from typing import Any

from agent.incident import Incident
from agent.reasoning_engine import build_reasoning_report


REVIEW_TITLE = "Relium AI Deployment Review"


def build_pr_review(incident: Incident) -> dict[str, Any]:
    reasoning = build_reasoning_report(incident)
    model_names = _model_names(incident)
    business_metrics = _business_metrics(incident)

    return {
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
        "primary_root_cause": incident.root_cause,
        "executive_summary": reasoning.executive_summary,
        "evidence": [
            {
                "title": item.title,
                "explanation": item.explanation,
                "severity": item.severity,
                "confidence": item.confidence,
                "supporting_metadata": _serialize(dict(item.supporting_metadata)),
            }
            for item in reasoning.evidence
        ],
        "recommendation": reasoning.recommendation,
        "business_metrics": business_metrics,
        "signals_considered": [
            {
                "component": signal.component,
                "severity": _enum_value(signal.severity),
                "confidence": signal.confidence,
                "score": signal.score,
                "reasons": list(signal.reasons),
                "metadata": _serialize(dict(signal.metadata)),
            }
            for signal in incident.signals
        ],
    }


def render_pr_review_markdown(review: dict) -> str:
    return "\n".join(
        [
            "## Relium AI Deployment Review",
            "",
            f"**Deployment Decision:** {_review_text(review, 'deployment_decision')}",
            f"**Pipeline Health:** {_review_text(review, 'pipeline_health')}",
            f"**Confidence:** {_review_text(review, 'confidence')}",
            f"**Models Reviewed:** {_review_text(review, 'models_reviewed')}",
            f"**Highest Severity:** {_review_text(review, 'highest_severity')}",
            "",
            "### Primary Root Cause",
            _review_text(review, "primary_root_cause", "None"),
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
            *_business_metric_section(review.get("business_metrics", [])),
            "### Signals Considered",
            *_signal_lines(review.get("signals_considered", [])),
        ]
    )


def _business_metric_section(lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [
        "### Business Metrics",
        *_bullet_lines(lines),
        "",
    ]


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def _business_metrics(incident: Incident) -> list[str]:
    for signal in incident.signals:
        if signal.component == "business_metrics":
            return _business_metric_lines_from_metadata(signal.metadata)
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
