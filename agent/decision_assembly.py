from typing import Any

from agent.decision_engine import evaluate
from agent.incident import Incident
from agent.incident_builder import build_incident, summarize_incident
from agent.signals import Signal


def assemble_decision_incident(
    signals: list[Signal],
    *,
    incident_id: str = "INC-0001",
    root_cause: str | None = None,
    recommendation: str | None = None,
    affected_models: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Incident:
    decision = evaluate(signals)
    return build_incident(
        decision,
        incident_id=incident_id,
        root_cause=root_cause,
        recommendation=recommendation,
        affected_models=affected_models,
        metadata=metadata,
    )


def summarize_decision_incident(incident: Incident) -> dict:
    return summarize_incident(incident)
