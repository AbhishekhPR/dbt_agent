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


def assemble_pipeline_incident(
    *,
    metadata_signal: Signal | None = None,
    drift_signal: Signal | None = None,
    blast_radius_signal: Signal | None = None,
    historical_reliability_signal: Signal | None = None,
    ast_signal: Signal | None = None,
    kpi_impact_signal: Signal | None = None,
    incident_id: str = "INC-0001",
    affected_models: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Incident:
    signals = [
        signal
        for signal in [
            ast_signal,
            metadata_signal,
            drift_signal,
            blast_radius_signal,
            historical_reliability_signal,
            kpi_impact_signal,
        ]
        if signal is not None
    ]
    decision = evaluate(signals)
    return build_incident(
        decision,
        incident_id=incident_id,
        affected_models=affected_models,
        metadata=metadata,
    )


def summarize_pipeline_incident(incident: Incident) -> dict:
    summary = summarize_incident(incident)
    return {
        **summary,
        "signal_components": [
            signal.component
            for signal in incident.signals
        ],
    }
