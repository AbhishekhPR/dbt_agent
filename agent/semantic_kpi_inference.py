from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agent.kpi_discovery import DiscoveredKPI
from agent.semantic_graph import SemanticGraph, explain_path
from agent.signals import Signal


@dataclass
class ImpactedKPI:
    name: str
    confidence: int
    impacted_by_models: list[str] = field(default_factory=list)
    related_columns: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class KPIImpactReport:
    changed_models: list[str] = field(default_factory=list)
    impacted_kpis: list[ImpactedKPI] = field(default_factory=list)
    unaffected_kpis: list[DiscoveredKPI] = field(default_factory=list)
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def infer_impacted_kpis(
    *,
    changed_models: list[str],
    discovered_kpis: list[DiscoveredKPI],
    lineage: dict[str, list[str]] | None = None,
    semantic_graph: SemanticGraph | None = None,
    column_lineage_graph: Any = None,
    changed_columns_by_model: dict[str, list[str]] | None = None,
) -> KPIImpactReport:
    changed = list(changed_models or [])
    kpis = list(discovered_kpis or [])
    lineage_map = _copy_lineage(lineage)
    changed_columns = _copy_changed_columns(changed_columns_by_model)
    downstream_by_model = {
        model: _downstream_models(model, lineage_map)
        for model in changed
    }
    column_lineage = _column_lineage_dict(column_lineage_graph)

    impacted = []
    unaffected = []

    for kpi in kpis:
        impact = _impact_for_kpi(
            kpi,
            changed,
            downstream_by_model,
            semantic_graph,
            column_lineage,
            changed_columns,
        )
        if impact:
            impacted.append(impact)
        else:
            unaffected.append(kpi)

    impacted = sorted(impacted, key=lambda impact: impact.name)
    unaffected = sorted(unaffected, key=lambda kpi: kpi.name)
    confidence = _report_confidence(impacted)

    return KPIImpactReport(
        changed_models=changed,
        impacted_kpis=impacted,
        unaffected_kpis=unaffected,
        confidence=confidence,
        reasons=_report_reasons(impacted),
        metadata={
            "lineage_provided": lineage is not None,
            "semantic_graph_provided": semantic_graph is not None,
            "column_lineage_available": bool(column_lineage),
            "changed_columns_by_model": changed_columns,
            "fallback_reason": _report_fallback_reason(impacted, changed_columns, column_lineage),
            "column_level_evidence": _report_column_evidence(impacted),
            "downstream_models_by_changed_model": downstream_by_model,
            "impacted_count": len(impacted),
            "unaffected_count": len(unaffected),
        },
    )


def to_signal(report: KPIImpactReport) -> Signal:
    impacted_kpis = list(report.impacted_kpis or [])
    severity = _signal_severity(impacted_kpis, report.confidence)
    return Signal(
        component="kpi_impact",
        severity=severity,
        confidence=report.confidence,
        score=_signal_score(severity),
        reasons=_signal_reasons(report),
        metadata=_signal_metadata(report),
    )


def _impact_for_kpi(
    kpi: DiscoveredKPI,
    changed_models: list[str],
    downstream_by_model: dict[str, list[str]],
    semantic_graph: SemanticGraph | None,
    column_lineage: dict[str, Any],
    changed_columns_by_model: dict[str, list[str]],
) -> ImpactedKPI | None:
    related_models = list(kpi.related_models or [])
    related_model_set = set(related_models)
    direct_matches = _ordered_unique(
        model for model in changed_models
        if model in related_model_set
    )
    downstream_matches = []
    supporting_matches = []
    impact_paths = _impact_paths(kpi, changed_models, semantic_graph)

    for changed_model in sorted(changed_models):
        for downstream_model in downstream_by_model.get(changed_model, []):
            if downstream_model in related_model_set and downstream_model not in direct_matches:
                downstream_matches.append(downstream_model)
                supporting_matches.append(f"{changed_model} -> {downstream_model}")

    downstream_matches = _ordered_unique(downstream_matches)
    supporting_matches.extend(f"{model} -> {model}" for model in direct_matches)
    supporting_matches = _sort_supporting_matches(supporting_matches)

    path_models = [
        node
        for path in impact_paths
        for node in path[:-1]
    ]
    impacted_by_models = _ordered_unique([*direct_matches, *downstream_matches, *path_models])
    if not impacted_by_models:
        return None

    confidence = _impact_confidence(direct_matches, downstream_matches, impact_paths)
    impact_reasons = _impact_reasons(direct_matches, supporting_matches, impact_paths, kpi.name)
    column_assessment = _column_assessment_for_kpi(
        kpi,
        changed_models,
        impacted_by_models,
        column_lineage,
        changed_columns_by_model,
    )
    confidence = _column_adjusted_confidence(confidence, column_assessment)
    impact_reasons.extend(column_assessment.get("evidence", []))

    return ImpactedKPI(
        name=kpi.name,
        confidence=confidence,
        impacted_by_models=sorted(impacted_by_models),
        related_columns=list(kpi.related_columns or []),
        reasons=[*list(kpi.reasons or []), *impact_reasons],
        metadata={
            **dict(kpi.metadata or {}),
            "source_kpi_confidence": kpi.confidence,
            "direct_matches": sorted(direct_matches),
            "downstream_matches": sorted(downstream_matches),
            "supporting_matches": supporting_matches,
            "impact_paths": impact_paths,
            "column_lineage_available": bool(column_lineage),
            "changed_columns_by_model": changed_columns_by_model,
            "fallback_reason": column_assessment.get("fallback_reason"),
            "column_level_evidence": list(column_assessment.get("evidence", [])),
            "column_impact": column_assessment.get("impact"),
        },
    )


def _downstream_models(model: str, lineage: dict[str, list[str]]) -> list[str]:
    visited = set()
    downstream = []
    queue = deque(lineage.get(model, []))

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        downstream.append(current)
        queue.extend(lineage.get(current, []))

    return downstream


def _impact_paths(
    kpi: DiscoveredKPI,
    changed_models: list[str],
    semantic_graph: SemanticGraph | None,
) -> list[list[str]]:
    if semantic_graph is None:
        return []

    paths = []
    for changed_model in sorted(changed_models):
        path = explain_path(semantic_graph, changed_model, kpi.name)
        if path:
            paths.append(path)
    return sorted(paths, key=lambda path: tuple(path))


def _impact_confidence(
    direct_matches: list[str],
    downstream_matches: list[str],
    impact_paths: list[list[str]],
) -> int:
    support_count = len(direct_matches) + len(downstream_matches) + len(impact_paths)
    if direct_matches or impact_paths:
        base = 90
    else:
        base = 80
    return min(100, base + max(0, support_count - 1) * 5)


def _impact_reasons(
    direct_matches: list[str],
    supporting_matches: list[str],
    impact_paths: list[list[str]],
    kpi_name: str,
) -> list[str]:
    reasons = [f"Direct model match: {model}" for model in sorted(direct_matches)]
    for match in supporting_matches:
        upstream, downstream = match.split(" -> ", 1)
        if upstream != downstream:
            reasons.append(f"Downstream lineage match: {upstream} -> {downstream}")
    for path in impact_paths:
        reasons.append(f"{kpi_name} is impacted through {' → '.join(path)}")
    return reasons


def _report_confidence(impacted: list[ImpactedKPI]) -> int:
    if not impacted:
        return 0
    return round(sum(impact.confidence for impact in impacted) / len(impacted))


def _report_reasons(impacted: list[ImpactedKPI]) -> list[str]:
    reasons = []
    for impact in impacted:
        reasons.append(
            f"{impact.name} impacted by {', '.join(impact.impacted_by_models)}"
        )
    return reasons


def _copy_lineage(lineage: dict[str, list[str]] | None) -> dict[str, list[str]]:
    return {
        str(model): list(downstream or [])
        for model, downstream in (lineage or {}).items()
    }


def _copy_changed_columns(changed_columns_by_model: dict[str, list[str]] | None) -> dict[str, list[str]]:
    copied = {}
    for model_name, columns in (changed_columns_by_model or {}).items():
        values = _ordered_unique(str(column) for column in columns or [] if column)
        if values:
            copied[str(model_name)] = values
    return copied


def _column_lineage_dict(column_lineage_graph: Any) -> dict[str, Any]:
    if column_lineage_graph is None:
        return {}
    if hasattr(column_lineage_graph, "to_dict"):
        payload = column_lineage_graph.to_dict()
    elif isinstance(column_lineage_graph, dict):
        payload = column_lineage_graph
    else:
        return {}
    models = payload.get("models") if isinstance(payload, dict) else {}
    return dict(models or {}) if isinstance(models, dict) else {}


def _column_assessment_for_kpi(
    kpi: DiscoveredKPI,
    changed_models: list[str],
    impacted_by_models: list[str],
    column_lineage: dict[str, Any],
    changed_columns_by_model: dict[str, list[str]],
) -> dict[str, Any]:
    if not column_lineage:
        return {"impact": "fallback", "fallback_reason": "column lineage unavailable", "evidence": []}
    if not changed_columns_by_model:
        return {"impact": "fallback", "fallback_reason": "changed columns unavailable", "evidence": []}

    relevant_columns = {str(column).casefold() for column in list(kpi.related_columns or []) if column}
    if not relevant_columns:
        return {"impact": "fallback", "fallback_reason": "kpi columns unavailable", "evidence": []}

    evidence = []
    ambiguous = False
    related = False
    evaluated = False
    impacted_model_set = set(impacted_by_models)
    for model_name in changed_models:
        if model_name not in impacted_model_set and model_name not in list(kpi.related_models or []):
            continue
        lineage = column_lineage.get(model_name)
        changed_columns = changed_columns_by_model.get(model_name) or []
        if not changed_columns:
            continue
        if not isinstance(lineage, dict):
            ambiguous = True
            continue
        unknown_columns = {str(column).casefold() for column in lineage.get("unknown_columns") or []}
        output_columns = {str(column).casefold() for column in lineage.get("output_columns") or []}
        for column in changed_columns:
            evaluated = True
            normalized = str(column).casefold()
            display = f"{model_name}.{column}"
            if normalized in unknown_columns or normalized not in output_columns:
                ambiguous = True
                continue
            if normalized in relevant_columns:
                related = True
                evidence.append(f"{kpi.name} reads {display}")
            else:
                evidence.append(f"{kpi.name} does not read {display}")

    if related:
        return {"impact": "related", "fallback_reason": None, "evidence": _ordered_unique(evidence)}
    if ambiguous or not evaluated:
        return {"impact": "fallback", "fallback_reason": "column lineage ambiguous", "evidence": []}
    return {"impact": "unrelated", "fallback_reason": None, "evidence": _ordered_unique(evidence)}


def _column_adjusted_confidence(confidence: int, assessment: dict[str, Any]) -> int:
    impact = assessment.get("impact")
    if impact == "related":
        return max(confidence, 95)
    if impact == "unrelated":
        return min(confidence, 55)
    return confidence


def _report_column_evidence(impacted: list[ImpactedKPI]) -> list[str]:
    evidence = []
    for impact in impacted:
        evidence.extend((impact.metadata or {}).get("column_level_evidence") or [])
    return _ordered_unique(evidence)


def _report_fallback_reason(
    impacted: list[ImpactedKPI],
    changed_columns_by_model: dict[str, list[str]],
    column_lineage: dict[str, Any],
) -> str | None:
    for impact in impacted:
        reason = (impact.metadata or {}).get("fallback_reason")
        if reason:
            return reason
    if not column_lineage:
        return "column lineage unavailable"
    if not changed_columns_by_model:
        return "changed columns unavailable"
    return None


def _ordered_unique(values) -> list[str]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _sort_supporting_matches(matches: list[str]) -> list[str]:
    return sorted(
        matches,
        key=lambda match: tuple(match.split(" -> ", 1)),
    )


def _signal_severity(impacted_kpis: list[ImpactedKPI], confidence: int) -> str:
    if impacted_kpis and confidence >= 90:
        return "HIGH"
    if impacted_kpis and confidence < 70:
        return "LOW"
    if impacted_kpis:
        return "MEDIUM"
    return "LOW"


def _signal_score(severity: str) -> int:
    return {
        "HIGH": -30,
        "MEDIUM": -15,
        "LOW": 0,
    }[severity]


def _signal_reasons(report: KPIImpactReport) -> list[str]:
    reasons = list(report.reasons or [])
    for impact in sorted(report.impacted_kpis or [], key=lambda kpi: kpi.name):
        for reason in impact.reasons or []:
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _signal_metadata(report: KPIImpactReport) -> dict[str, Any]:
    impacted = sorted(report.impacted_kpis or [], key=lambda kpi: kpi.name)
    unaffected = sorted(report.unaffected_kpis or [], key=lambda kpi: kpi.name)
    return {
        **dict(report.metadata or {}),
        "changed_models": list(report.changed_models or []),
        "impacted_kpis": [impact.name for impact in impacted],
        "unaffected_kpis": [kpi.name for kpi in unaffected],
        "impact_paths": _signal_impact_paths(impacted),
        "column_level_evidence": _report_column_evidence(impacted),
    }


def _signal_impact_paths(impacted_kpis: list[ImpactedKPI]) -> list[list[str]]:
    paths = []
    for impact in impacted_kpis:
        for path in (impact.metadata or {}).get("impact_paths", []):
            copied = list(path)
            if copied not in paths:
                paths.append(copied)
    return sorted(paths, key=lambda path: tuple(path))
