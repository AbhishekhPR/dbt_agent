from typing import Any

from agent.semantic_knowledge import SemanticContract
from agent.semantic_kpi_inference import KPIImpactReport
from agent.signals import Signal


def validate_semantic_contracts(
    *,
    contracts,
    changed_models,
    metadata=None,
    kpi_impact_report=None,
) -> dict:
    contract_list = list(contracts or [])
    changed = list(changed_models or [])
    observed_metadata = _copy_metadata(metadata)
    impact_report = kpi_impact_report

    contract_names = sorted(contract.kpi_name for contract in contract_list)
    impacted_models = []
    impacted_kpis = []
    impact_paths = _impact_paths(impact_report)
    violated_invariants = {}
    reasons = []
    severities = []
    confidences = []

    impact_by_kpi = _impact_by_kpi(impact_report)

    for contract in sorted(contract_list, key=lambda item: item.kpi_name):
        touched_models = sorted(set(changed).intersection(contract.related_models or []))
        if touched_models:
            impacted_models.extend(touched_models)
            severities.append("MEDIUM")
            confidences.append(80)
            for model in touched_models:
                reasons.append(f"{contract.kpi_name} may be impacted by changed model {model}")

        if contract.kpi_name in impact_by_kpi:
            impact = impact_by_kpi[contract.kpi_name]
            impacted_kpis.append(contract.kpi_name)
            impacted_models.extend(list(impact.impacted_by_models or []))
            severities.append("HIGH")
            confidences.append(max(90, int(impact.confidence or 0)))
            reasons.append(f"{contract.kpi_name} is semantically impacted by changed models")
            for reason in impact.reasons or []:
                if reason not in reasons:
                    reasons.append(reason)

        contract_violations = _violated_invariants(contract, observed_metadata)
        if contract_violations:
            violated_invariants[contract.kpi_name] = contract_violations
            severities.append("HIGH")
            confidences.append(95)
            for invariant in contract_violations:
                reasons.append(f"{contract.kpi_name} violates invariant: {invariant}")

    severity = _severity(severities)
    confidence = _confidence(severity, confidences)

    return {
        "severity": severity,
        "confidence": confidence,
        "score": _score(severity),
        "reasons": _ordered_unique(reasons),
        "metadata": {
            "contract_names": contract_names,
            "violated_invariants": violated_invariants,
            "impacted_models": sorted(_ordered_unique(impacted_models)),
            "impacted_kpis": sorted(_ordered_unique(impacted_kpis)),
            "impact_paths": impact_paths,
            "input_metadata": observed_metadata,
        },
    }


def to_signal(result) -> Signal:
    return Signal(
        component="semantic_contract",
        severity=result["severity"],
        confidence=result["confidence"],
        score=result["score"],
        reasons=list(result.get("reasons", [])),
        metadata=dict(result.get("metadata", {})),
    )


def _violated_invariants(contract: SemanticContract, metadata: dict[str, Any]) -> list[str]:
    violations = []
    values = _values_for_contract(contract, metadata)
    for invariant in contract.invariants or []:
        if invariant == "never negative" and any(value < 0 for value in values):
            violations.append(invariant)
        elif invariant == "between 0 and 100%" and any(value < 0 or value > 100 for value in values):
            violations.append(invariant)
    return violations


def _values_for_contract(contract: SemanticContract, metadata: dict[str, Any]) -> list[float]:
    metric_values = metadata.get("metric_values") if isinstance(metadata.get("metric_values"), dict) else {}
    candidates = [contract.kpi_name, *list(contract.related_columns or [])]
    values = []
    for key in candidates:
        if key in metric_values and isinstance(metric_values[key], (int, float)):
            values.append(float(metric_values[key]))
        elif key in metadata and isinstance(metadata[key], (int, float)):
            values.append(float(metadata[key]))
    return values


def _impact_by_kpi(report: KPIImpactReport | None) -> dict[str, Any]:
    if report is None:
        return {}
    return {
        impact.name: impact
        for impact in list(report.impacted_kpis or [])
    }


def _impact_paths(report: KPIImpactReport | None) -> list[list[str]]:
    paths = []
    if report is None:
        return paths
    for impact in list(report.impacted_kpis or []):
        for path in (impact.metadata or {}).get("impact_paths", []):
            copied = list(path)
            if copied not in paths:
                paths.append(copied)
    return sorted(paths, key=lambda path: tuple(path))


def _severity(severities: list[str]) -> str:
    if "HIGH" in severities:
        return "HIGH"
    if "MEDIUM" in severities:
        return "MEDIUM"
    return "LOW"


def _confidence(severity: str, confidences: list[int]) -> int:
    if confidences:
        return round(sum(confidences) / len(confidences))
    if severity == "LOW":
        return 75
    return 80


def _score(severity: str) -> int:
    return {
        "HIGH": -30,
        "MEDIUM": -15,
        "LOW": 0,
    }[severity]


def _copy_metadata(metadata) -> dict[str, Any]:
    copied = {}
    for key, value in dict(metadata or {}).items():
        if isinstance(value, dict):
            copied[key] = dict(value)
        elif isinstance(value, list):
            copied[key] = list(value)
        else:
            copied[key] = value
    return copied


def _ordered_unique(values) -> list[Any]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique
