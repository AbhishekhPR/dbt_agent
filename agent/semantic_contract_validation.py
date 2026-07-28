import copy
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
    material_reasons = []
    severities = []
    confidences = []

    impact_by_kpi = _impact_by_kpi(impact_report)
    contract_changes = _change_mapping(
        observed_metadata,
        "contract_changes",
        "affected_contracts",
    )
    dependency_changes = _change_mapping(
        observed_metadata,
        "dependency_changes",
        "affected_dependencies",
    )
    failed_contract_checks = _failed_contract_checks(observed_metadata)

    for contract in sorted(contract_list, key=lambda item: item.kpi_name):
        touched_models = sorted(set(changed).intersection(contract.related_models or []))
        if touched_models:
            impacted_models.extend(touched_models)

        if contract.kpi_name in impact_by_kpi:
            impact = impact_by_kpi[contract.kpi_name]
            impacted_kpis.append(contract.kpi_name)
            impacted_models.extend(list(impact.impacted_by_models or []))

        contract_violations = _violated_invariants(contract, observed_metadata)
        if contract_violations:
            violated_invariants[contract.kpi_name] = contract_violations
            severities.append("HIGH")
            confidences.append(95)
            for invariant in contract_violations:
                material_reasons.append(
                    f"{contract.kpi_name} violates invariant: {invariant}"
                )

        change_reasons, change_severities = _contract_change_evidence(
            contract.kpi_name,
            contract_changes.get(contract.kpi_name, {}),
        )
        if change_reasons:
            material_reasons.extend(change_reasons)
            severities.extend(change_severities)
            confidences.extend([95] * len(change_severities))

        dependency_reasons, dependency_severities = _dependency_change_evidence(
            contract.kpi_name,
            dependency_changes.get(contract.kpi_name, {}),
        )
        if dependency_reasons:
            material_reasons.extend(dependency_reasons)
            severities.extend(dependency_severities)
            confidences.extend([90] * len(dependency_severities))

        check_reasons = failed_contract_checks.get(contract.kpi_name, [])
        if check_reasons:
            material_reasons.extend(check_reasons)
            severities.extend(["HIGH"] * len(check_reasons))
            confidences.extend([95] * len(check_reasons))

    severity = _severity(severities)
    confidence = _confidence(severity, confidences)

    return {
        "severity": severity,
        "confidence": confidence,
        "score": _score(severity),
        "reasons": _ordered_unique(material_reasons),
        "metadata": {
            "contract_names": contract_names,
            "violated_invariants": violated_invariants,
            "contract_changes": contract_changes,
            "dependency_changes": dependency_changes,
            "failed_contract_checks": failed_contract_checks,
            "impacted_models": sorted(_ordered_unique(impacted_models)),
            "impacted_kpis": sorted(_ordered_unique(impacted_kpis)),
            "impact_paths": impact_paths,
            # Business-impact context belongs to the KPI-impact signal. Keeping
            # it out of the contract signal prevents one model-to-KPI
            # association from surfacing as two semantic causes.
            "context_reasons": [],
            "contextual_only": not bool(material_reasons),
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


def _change_mapping(metadata: dict[str, Any], *keys: str) -> dict[str, dict]:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, dict):
            return {
                str(kpi): copy.deepcopy(changes)
                for kpi, changes in value.items()
                if isinstance(changes, dict)
            }
    return {}


def _contract_change_evidence(
    kpi_name: str,
    changes: dict,
) -> tuple[list[str], list[str]]:
    if not isinstance(changes, dict):
        return [], []

    reasons = []
    severities = []
    meaning_change = changes.get("business_meaning")
    if _has_change(meaning_change):
        reasons.append(f"{kpi_name} contract meaning changed")
        severities.append("HIGH")

    assumption_change = changes.get("assumptions")
    if _has_change(assumption_change):
        reasons.append(f"{kpi_name} contract assumption changed")
        severities.append("MEDIUM")

    invariant_change = changes.get("invariants")
    if _has_change(invariant_change):
        added, removed = _added_removed(invariant_change)
        for invariant in removed:
            reasons.append(f"{kpi_name} lost invariant {invariant}")
        for invariant in added:
            reasons.append(f"{kpi_name} gained invariant {invariant}")
        if not added and not removed:
            reasons.append(f"{kpi_name} contract invariant changed")
        severities.append("HIGH")

    return reasons, severities


def _dependency_change_evidence(
    kpi_name: str,
    changes: dict,
) -> tuple[list[str], list[str]]:
    if not isinstance(changes, dict):
        return [], []

    reasons = []
    severities = []
    for field_name, change in sorted(changes.items()):
        if not _has_change(change):
            continue
        added, removed = _added_removed(change)
        label = field_name.replace("_", " ")
        for value in removed:
            reasons.append(f"{kpi_name} lost required {label} {value}")
        for value in added:
            reasons.append(f"{kpi_name} gained required {label} {value}")
        if not added and not removed:
            reasons.append(f"{kpi_name} required {label} changed")
        severities.append("HIGH" if removed else "MEDIUM")
    return reasons, severities


def _has_change(change: Any) -> bool:
    if not isinstance(change, dict):
        return bool(change)
    if "previous" in change or "current" in change:
        return change.get("previous") != change.get("current")
    added, removed = _added_removed(change)
    return bool(added or removed)


def _added_removed(change: Any) -> tuple[list[str], list[str]]:
    if not isinstance(change, dict):
        return [], []
    return (
        [str(value) for value in change.get("added", []) or []],
        [str(value) for value in change.get("removed", []) or []],
    )


def _failed_contract_checks(metadata: dict[str, Any]) -> dict[str, list[str]]:
    raw = (
        metadata.get("failed_contract_checks")
        or metadata.get("contract_check_failures")
        or {}
    )
    failures: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [
            (
                item.get("kpi_name") or item.get("contract_name"),
                item,
            )
            for item in raw
            if isinstance(item, dict)
        ]
    else:
        return failures

    for kpi_name, detail in items:
        if not kpi_name:
            continue
        if isinstance(detail, list):
            messages = [str(value) for value in detail if value]
        elif isinstance(detail, dict):
            if detail.get("passed") is True or str(
                detail.get("status", "")
            ).casefold() in {"passed", "ok"}:
                continue
            message = (
                detail.get("reason")
                or detail.get("message")
                or detail.get("check_name")
                or "evaluated contract check failed"
            )
            messages = [str(message)]
        elif detail:
            messages = [str(detail)]
        else:
            messages = []
        if messages:
            failures[str(kpi_name)] = [
                f"{kpi_name} contract check failed: {message}"
                for message in messages
            ]
    return failures


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
    return copy.deepcopy(dict(metadata or {}))


def _ordered_unique(values) -> list[Any]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique
