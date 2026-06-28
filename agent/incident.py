from dataclasses import dataclass, field
from typing import Any

from agent.decision_engine import Decision, DeploymentDecision
from agent.signals import Severity, Signal


@dataclass
class Incident:
    incident_id: str
    health: int
    decision: DeploymentDecision
    severity: Severity
    confidence: int
    root_cause: str
    recommendation: str
    affected_models: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def create_incident(
    decision: Decision,
    *,
    incident_id: str = "INC-0001",
    root_cause: str = "",
    recommendation: str = "",
    affected_models: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        health=decision.health,
        decision=decision.decision,
        severity=decision.severity,
        confidence=decision.confidence,
        root_cause=root_cause,
        recommendation=recommendation,
        affected_models=list(affected_models or []),
        signals=list(decision.signals),
        metadata=dict(metadata or {}),
    )
