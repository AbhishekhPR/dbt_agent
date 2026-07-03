from typing import Any

from agent.decision_engine import SEVERITY_RANKS, Decision
from agent.evidence_curation import (
    clean_reason,
    is_low_level_reason,
    semantic_diff_reason_priority,
)
from agent.incident import Incident, create_incident
from agent.signals import Severity, Signal


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
    signals_with_reasons = _signals_with_reportable_reasons(decision.signals)
    if not signals_with_reasons:
        return ""

    for component, allowed_severities in [
        ("semantic_diff", None),
        ("semantic_contract", None),
        ("kpi_impact", None),
        ("ast", {Severity.HIGH, Severity.CRITICAL}),
    ]:
        reason = _prioritized_component_reason(
            signals_with_reasons,
            component,
            allowed_severities,
        )
        if reason:
            return reason

    highest_severity_signal = max(
        signals_with_reasons,
        key=lambda signal: SEVERITY_RANKS.get(signal.severity, 0),
    )
    return _first_reportable_reason(highest_severity_signal) or ""


def _prioritized_component_reason(
    signals: list[Signal],
    component: str,
    allowed_severities: set[Severity] | None,
) -> str:
    component_signals = [
        signal
        for signal in signals
        if signal.component == component
        and (
            allowed_severities is None
            or signal.severity in allowed_severities
        )
    ]
    if not component_signals:
        return ""

    if component == "semantic_diff":
        semantic_reasons = []
        for signal_index, signal in enumerate(component_signals):
            for reason_index, reason in enumerate(signal.reasons or []):
                if is_low_level_reason(reason):
                    continue
                cleaned = clean_reason(reason)
                if cleaned:
                    semantic_reasons.append(
                        (
                            semantic_diff_reason_priority(cleaned),
                            signal_index,
                            reason_index,
                            cleaned,
                        )
                    )

        if semantic_reasons:
            return min(semantic_reasons)[3]

    highest_signal = max(
        component_signals,
        key=lambda signal: SEVERITY_RANKS.get(signal.severity, 0),
    )
    return _first_reportable_reason(highest_signal) or ""


def _signals_with_reportable_reasons(signals: list[Signal]) -> list[Signal]:
    return [
        signal
        for signal in signals
        if _first_reportable_reason(signal)
    ]


def _first_reportable_reason(signal: Signal) -> str:
    for reason in signal.reasons or []:
        if is_low_level_reason(reason):
            continue
        cleaned = clean_reason(reason)
        if cleaned:
            return cleaned
    return ""
