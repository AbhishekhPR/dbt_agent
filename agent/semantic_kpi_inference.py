from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agent.kpi_discovery import DiscoveredKPI
from agent.semantic_graph import SemanticGraph, explain_path


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
) -> KPIImpactReport:
    changed = list(changed_models or [])
    kpis = list(discovered_kpis or [])
    lineage_map = _copy_lineage(lineage)
    downstream_by_model = {
        model: _downstream_models(model, lineage_map)
        for model in changed
    }

    impacted = []
    unaffected = []

    for kpi in kpis:
        impact = _impact_for_kpi(kpi, changed, downstream_by_model, semantic_graph)
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
            "downstream_models_by_changed_model": downstream_by_model,
            "impacted_count": len(impacted),
            "unaffected_count": len(unaffected),
        },
    )


def _impact_for_kpi(
    kpi: DiscoveredKPI,
    changed_models: list[str],
    downstream_by_model: dict[str, list[str]],
    semantic_graph: SemanticGraph | None,
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
