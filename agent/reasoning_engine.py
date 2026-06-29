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
    context = (
        f"health {_health_text(incident.health)}, "
        f"severity {_enum_value(incident.severity)}, "
        f"and confidence {_confidence_text(incident.confidence)}"
    )
    if decision == DeploymentDecision.BLOCK.value:
        return (
            "Deployment is blocked because the reliability signals indicate "
            f"deployment risk (decision BLOCK DEPLOYMENT; {context})."
        )
    if decision == DeploymentDecision.WARN.value:
        return (
            "Deployment should proceed with caution because the reliability "
            f"signals need review (decision WARN; {context})."
        )
    if decision == DeploymentDecision.ALLOW.value:
        return (
            "Deployment is allowed because the reliability signals are within "
            f"acceptable limits (decision ALLOW; {context})."
        )
    return (
        "Deployment was evaluated from the available reliability signals "
        f"({context})."
    )


def _conclusion(incident: Incident, evidence: list[Evidence]) -> str:
    decision = _enum_value(incident.decision)
    evidence_count = len(evidence)
    plural = "item" if evidence_count == 1 else "items"
    return (
        "The recommendation reflects that multiple reliability signals "
        f"contributed to the {_decision_label(decision)} outcome: "
        f"{evidence_count} evidence {plural} were considered with health "
        f"{_health_text(incident.health)}, severity "
        f"{_enum_value(incident.severity)}, and confidence "
        f"{_confidence_text(incident.confidence)}."
    )


def _component_label(component: str) -> str:
    if component.lower() == "ast":
        return "AST"
    return " ".join(part.capitalize() for part in component.split("_"))


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _health_text(health: int) -> str:
    return f"{health} / 100"


def _confidence_text(confidence: int) -> str:
    return f"{confidence}%"


def _decision_label(decision: Any) -> str:
    value = _enum_value(decision)
    if value == DeploymentDecision.BLOCK.value:
        return "BLOCK DEPLOYMENT"
    return str(value)
