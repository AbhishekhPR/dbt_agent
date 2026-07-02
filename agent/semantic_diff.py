from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

from agent.signals import Signal


@dataclass
class SemanticDiff:
    previous_snapshot_id: str
    current_snapshot_id: str
    changed_kpis: list[str] = field(default_factory=list)
    added_kpis: list[str] = field(default_factory=list)
    removed_kpis: list[str] = field(default_factory=list)
    dependency_changes: dict[str, Any] = field(default_factory=dict)
    contract_changes: dict[str, Any] = field(default_factory=dict)
    severity: str = "LOW"
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)


def compare_semantic_snapshots(previous_snapshot, current_snapshot) -> SemanticDiff:
    previous = _snapshot_dict(previous_snapshot)
    current = _snapshot_dict(current_snapshot)
    previous_contracts = _contracts_by_kpi(previous)
    current_contracts = _contracts_by_kpi(current)

    previous_names = set(previous_contracts)
    current_names = set(current_contracts)
    added_kpis = sorted(current_names - previous_names)
    removed_kpis = sorted(previous_names - current_names)
    common_kpis = sorted(previous_names & current_names)

    dependency_changes = {}
    contract_changes = {}
    changed_kpis = []
    reasons = []
    severity_hits = []
    change_counts_by_kpi = {}

    for kpi in added_kpis:
        reasons.append(f"{kpi} KPI was added")
        severity_hits.append("MEDIUM")
        change_counts_by_kpi[kpi] = change_counts_by_kpi.get(kpi, 0) + 1

    for kpi in removed_kpis:
        reasons.append(f"{kpi} KPI was removed")
        severity_hits.append("HIGH")
        change_counts_by_kpi[kpi] = change_counts_by_kpi.get(kpi, 0) + 1

    for kpi in common_kpis:
        previous_contract = previous_contracts[kpi]
        current_contract = current_contracts[kpi]
        kpi_dependency_changes, dependency_reasons, dependency_severities = _dependency_changes(
            kpi,
            previous_contract,
            current_contract,
        )
        kpi_contract_changes, contract_reasons, contract_severities = _contract_changes(
            kpi,
            previous_contract,
            current_contract,
        )
        if kpi_dependency_changes or kpi_contract_changes:
            changed_kpis.append(kpi)
            change_counts_by_kpi[kpi] = (
                change_counts_by_kpi.get(kpi, 0)
                + _change_count(kpi_dependency_changes)
                + _change_count(kpi_contract_changes)
            )
        if kpi_dependency_changes:
            dependency_changes[kpi] = kpi_dependency_changes
        if kpi_contract_changes:
            contract_changes[kpi] = kpi_contract_changes
        reasons.extend(dependency_reasons)
        reasons.extend(contract_reasons)
        severity_hits.extend(dependency_severities)
        severity_hits.extend(contract_severities)

    severity = _severity(severity_hits)
    return SemanticDiff(
        previous_snapshot_id=str(previous.get("snapshot_id", "")),
        current_snapshot_id=str(current.get("snapshot_id", "")),
        changed_kpis=sorted(changed_kpis),
        added_kpis=added_kpis,
        removed_kpis=removed_kpis,
        dependency_changes=dependency_changes,
        contract_changes=contract_changes,
        severity=severity,
        confidence=_confidence(severity, change_counts_by_kpi),
        reasons=_ordered_unique(reasons),
        metadata={
            "previous_snapshot_id": str(previous.get("snapshot_id", "")),
            "current_snapshot_id": str(current.get("snapshot_id", "")),
            "changed_kpis": sorted(changed_kpis),
            "affected_dependencies": dependency_changes,
            "affected_contracts": contract_changes,
        },
    )


def to_signal(diff: SemanticDiff) -> Signal:
    return Signal(
        component="semantic_diff",
        severity=diff.severity,
        confidence=diff.confidence,
        score=_signal_score(diff.severity),
        reasons=list(diff.reasons or []),
        metadata=_signal_metadata(diff),
    )


def _dependency_changes(kpi: str, previous: dict, current: dict):
    changes = {}
    reasons = []
    severities = []
    dependency_fields = [
        ("related_models", "related model", "MEDIUM"),
        ("related_columns", "related columns", "MEDIUM"),
        ("upstream_sources", "upstream dependency", "HIGH"),
        ("downstream_consumers", "downstream consumer", "MEDIUM"),
    ]
    for field_name, label, severity in dependency_fields:
        added, removed = _list_delta(previous.get(field_name, []), current.get(field_name, []))
        if not added and not removed:
            continue
        changes[field_name] = {"added": added, "removed": removed}
        severities.append(severity)
        reasons.extend(_dependency_reasons(kpi, field_name, label, added, removed))
    return changes, reasons, severities


def _contract_changes(kpi: str, previous: dict, current: dict):
    changes = {}
    reasons = []
    severities = []

    previous_meaning = previous.get("business_meaning")
    current_meaning = current.get("business_meaning")
    if previous_meaning != current_meaning:
        changes["business_meaning"] = {
            "previous": previous_meaning,
            "current": current_meaning,
        }
        reasons.append(f"{kpi} contract meaning changed")
        severities.append("HIGH")

    for field_name, label in [("assumptions", "assumption"), ("invariants", "invariant")]:
        added, removed = _list_delta(previous.get(field_name, []), current.get(field_name, []))
        if not added and not removed:
            continue
        changes[field_name] = {"added": added, "removed": removed}
        reasons.extend(_contract_reasons(kpi, field_name, label, added, removed))
        if field_name == "invariants" and removed:
            severities.append("HIGH")
        else:
            severities.append("MEDIUM")

    return changes, reasons, severities


def _dependency_reasons(kpi: str, field_name: str, label: str, added: list[str], removed: list[str]) -> list[str]:
    if field_name == "related_columns" and added and removed:
        return [f"{kpi} related columns changed from {', '.join(removed)} to {', '.join(added)}"]
    reasons = []
    for value in added:
        reasons.append(f"{kpi} gained {label} {value}")
    for value in removed:
        reasons.append(f"{kpi} lost {label} {value}")
    return reasons


def _contract_reasons(kpi: str, field_name: str, label: str, added: list[str], removed: list[str]) -> list[str]:
    if field_name == "assumptions":
        return [f"{kpi} contract assumption changed"]
    reasons = []
    for value in added:
        reasons.append(f"{kpi} gained {label} {value}")
    for value in removed:
        reasons.append(f"{kpi} lost {label} {value}")
    return reasons


def _contracts_by_kpi(snapshot: dict) -> dict[str, dict]:
    semantic_context = snapshot.get("semantic_context") or {}
    knowledge_report = semantic_context.get("knowledge_report") or {}
    contracts = knowledge_report.get("contracts") or []
    return {
        str(contract.get("kpi_name")): dict(contract)
        for contract in contracts
        if contract.get("kpi_name")
    }


def _snapshot_dict(snapshot) -> dict:
    if hasattr(snapshot, "to_dict"):
        return snapshot.to_dict()
    if is_dataclass(snapshot):
        return _serializable(snapshot)
    return dict(snapshot or {})


def _list_delta(previous_values, current_values) -> tuple[list[str], list[str]]:
    previous = {str(value) for value in (previous_values or [])}
    current = {str(value) for value in (current_values or [])}
    return sorted(current - previous), sorted(previous - current)


def _change_count(changes: dict) -> int:
    count = 0
    for value in changes.values():
        if isinstance(value, dict):
            count += len(value.get("added", [])) + len(value.get("removed", []))
            if "previous" in value or "current" in value:
                count += 1
        else:
            count += 1
    return count


def _severity(severities: list[str]) -> str:
    if "HIGH" in severities:
        return "HIGH"
    if "MEDIUM" in severities:
        return "MEDIUM"
    return "LOW"


def _confidence(severity: str, change_counts_by_kpi: dict[str, int]) -> int:
    if severity == "LOW":
        return 75
    total_changes = sum(change_counts_by_kpi.values())
    max_same_kpi_changes = max(change_counts_by_kpi.values(), default=0)
    return min(100, 70 + (total_changes * 5) + (max_same_kpi_changes * 5))


def _signal_score(severity: str) -> int:
    return {
        "LOW": 0,
        "MEDIUM": -20,
        "HIGH": -35,
    }[str(severity)]


def _signal_metadata(diff: SemanticDiff) -> dict[str, Any]:
    return {
        **_serializable(dict(diff.metadata or {})),
        "previous_snapshot_id": diff.previous_snapshot_id,
        "current_snapshot_id": diff.current_snapshot_id,
        "changed_kpis": _serializable(diff.changed_kpis),
        "added_kpis": _serializable(diff.added_kpis),
        "removed_kpis": _serializable(diff.removed_kpis),
        "dependency_changes": _serializable(diff.dependency_changes),
        "contract_changes": _serializable(diff.contract_changes),
    }


def _ordered_unique(values) -> list[Any]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _serializable(value):
    if is_dataclass(value):
        return _serializable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _serializable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value
