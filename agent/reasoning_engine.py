from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.decision_engine import DeploymentDecision
from agent.incident import Incident


@dataclass
class Evidence:
    title: str
    explanation: str
    severity: str
    confidence: int
    supporting_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReasoningReport:
    executive_summary: str
    evidence: list[Evidence]
    conclusion: str
    recommendation: str


def build_reasoning_report(incident: Incident) -> ReasoningReport:
    evidence = _evidence_from_signals(incident)
    return ReasoningReport(
        executive_summary=_executive_summary(incident),
        evidence=evidence,
        conclusion=_conclusion(incident, evidence),
        recommendation=(
            incident.recommendation
            or "Review the deployment evidence before proceeding."
        ),
    )


def _evidence_from_signals(incident: Incident) -> list[Evidence]:
    evidence = []
    for signal in incident.signals:
        reasons = signal.reasons or ["Signal detected"]
        for reason in reasons:
            evidence.append(
                Evidence(
                    title=f"{_component_label(signal.component)}: {reason}",
                    explanation=(
                        f"{_component_label(signal.component)} reported: "
                        f"{reason}"
                    ),
                    severity=_enum_value(signal.severity),
                    confidence=signal.confidence,
                    supporting_metadata=dict(signal.metadata),
                )
            )
    return evidence


def _executive_summary(incident: Incident) -> str:
    decision = _enum_value(incident.decision)
    disposition = {
        DeploymentDecision.ALLOW.value: "allowed",
        DeploymentDecision.WARN.value: "returned a warning",
        DeploymentDecision.BLOCK.value: "blocked",
    }.get(decision, "evaluated")
    return (
        f"Deployment {decision} was {disposition} with health "
        f"{incident.health}, severity {_enum_value(incident.severity)}, "
        f"and confidence {incident.confidence}."
    )


def _conclusion(incident: Incident, evidence: list[Evidence]) -> str:
    decision = _enum_value(incident.decision)
    evidence_count = len(evidence)
    plural = "item" if evidence_count == 1 else "items"
    return (
        f"The combined evidence ({evidence_count} {plural}) led to "
        f"deployment decision {decision} at health {incident.health} with "
        f"severity {_enum_value(incident.severity)} and confidence "
        f"{incident.confidence}."
    )


def _component_label(component: str) -> str:
    if component.lower() == "ast":
        return "AST"
    return " ".join(part.capitalize() for part in component.split("_"))


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value
