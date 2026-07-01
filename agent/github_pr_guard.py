from enum import Enum
from typing import Any

from agent.incident import Incident
from agent.reasoning_engine import build_reasoning_report


REVIEW_TITLE = "Relium AI Deployment Review"


def build_pr_review(incident: Incident) -> dict[str, Any]:
    reasoning = build_reasoning_report(incident)
    model_names = _model_names(incident)

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


def _model_names(incident: Incident) -> list[str]:
    names = []
    for model_name in incident.affected_models:
        _append_unique(names, model_name)
    for signal in incident.signals:
        _append_unique(names, signal.metadata.get("model_name"))
    return names


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
