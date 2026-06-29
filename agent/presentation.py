from enum import Enum
from typing import Any

from agent.incident import Incident


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
    reasons = []
    for signal in incident.signals:
        reasons.extend(signal.reasons)
    return list(reasons)


def _bullet_list(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def render_cli(incident: Incident) -> str:
    lines = [
        "Relium Deployment Decision",
        "",
        f"Pipeline Health: {incident.health}",
        f"Deployment Decision: {_enum_value(incident.decision)}",
        f"Severity: {_enum_value(incident.severity)}",
        f"Confidence: {incident.confidence}",
        "",
        f"Primary Root Cause: {incident.root_cause or 'None'}",
        "",
        "Top Reasons:",
        *_bullet_list(_top_reasons(incident)),
        "",
        f"Recommendation: {incident.recommendation or 'None'}",
        "",
        "Signals Considered:",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if incident.affected_models:
        lines.extend([
            "",
            "Affected Models:",
            *_bullet_list(list(incident.affected_models)),
        ])

    return "\n".join(lines)


def render_markdown(incident: Incident) -> str:
    lines = [
        "# Relium Deployment Decision",
        "",
        "## Pipeline Health",
        str(incident.health),
        "",
        "## Deployment Decision",
        str(_enum_value(incident.decision)),
        "",
        "## Severity",
        str(_enum_value(incident.severity)),
        "",
        "## Confidence",
        str(incident.confidence),
        "",
        "## Primary Root Cause",
        incident.root_cause or "None",
        "",
        "## Top Reasons",
        *_bullet_list(_top_reasons(incident)),
        "",
        "## Recommendation",
        incident.recommendation or "None",
        "",
        "## Signals Considered",
        *_bullet_list([signal.component for signal in incident.signals]),
    ]

    if incident.affected_models:
        lines.extend([
            "",
            "## Affected Models",
            *_bullet_list(list(incident.affected_models)),
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
