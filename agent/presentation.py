from enum import Enum
from typing import Any

from agent.decision_engine import DeploymentDecision
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
    reasons = []
    for signal in incident.signals:
        reasons.extend(signal.reasons)
    return list(reasons)


def _bullet_list(items: list[str]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def render_cli(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
    lines = [
        "Relium Deployment Decision",
        "",
        f"Pipeline Health: {_health_text(incident.health)}",
        f"Deployment Decision: {_decision_label(incident.decision)}",
        f"Severity: {_enum_value(incident.severity)}",
        f"Confidence: {_confidence_text(incident.confidence)}",
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

    lines.extend([
        "",
        "Reasoning:",
        "",
        f"Executive Summary: {reasoning.executive_summary}",
        "",
        "Evidence:",
        *_format_cli_evidence(reasoning.evidence),
        "",
        f"Conclusion: {reasoning.conclusion}",
        "",
        f"Recommendation: {reasoning.recommendation}",
    ])

    return "\n".join(lines)


def render_markdown(incident: Incident) -> str:
    reasoning = build_reasoning_report(incident)
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

    lines.extend([
        "",
        "## Reasoning",
        "",
        "### Executive Summary",
        reasoning.executive_summary,
        "",
        "### Evidence",
        *_format_markdown_evidence(reasoning.evidence),
        "",
        "### Conclusion",
        reasoning.conclusion,
        "",
        "### Recommendation",
        reasoning.recommendation,
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
            f"- {item.title} "
            f"(severity: {item.severity}, confidence: {item.confidence})"
        )
        for item in evidence
    ]


def _format_markdown_evidence(evidence) -> list[str]:
    if not evidence:
        return ["- None"]
    return [
        (
            f"- **{item.title}** "
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
