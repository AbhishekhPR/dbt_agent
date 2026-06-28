from typing import Any

from agent.decision_engine import SEVERITY_RANKS, Decision
from agent.incident import Incident, create_incident


DEFAULT_RECOMMENDATION = "Review the flagged pipeline signals before deployment."


def build_incident(
    decision: Decision,
    *,
    incident_id: str = "INC-0001",
    root_cause: str | None = None,
    recommendation: str | None = None,
    affected_models: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Incident:
    return create_incident(
        decision,
        incident_id=incident_id,
        root_cause=(
            root_cause
            if root_cause is not None
            else _derive_root_cause(decision)
        ),
        recommendation=(
            recommendation
            if recommendation is not None
            else DEFAULT_RECOMMENDATION
        ),
        affected_models=list(affected_models or []),
        metadata=dict(metadata or {}),
    )


def summarize_incident(incident: Incident) -> dict:
    top_reasons = []
    for signal in incident.signals:
        top_reasons.extend(signal.reasons)

    return {
        "incident_id": incident.incident_id,
        "decision": incident.decision.value,
        "health": incident.health,
        "severity": incident.severity.value,
        "confidence": incident.confidence,
        "root_cause": incident.root_cause,
        "recommendation": incident.recommendation,
        "affected_models": list(incident.affected_models),
        "signal_count": len(incident.signals),
        "top_reasons": top_reasons,
    }


def _derive_root_cause(decision: Decision) -> str:
    signals_with_reasons = [
        signal for signal in decision.signals if signal.reasons
    ]
    if not signals_with_reasons:
        return ""

    highest_severity_signal = max(
        signals_with_reasons,
        key=lambda signal: SEVERITY_RANKS.get(signal.severity, 0),
    )
    return highest_severity_signal.reasons[0]
