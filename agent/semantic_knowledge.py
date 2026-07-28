from dataclasses import dataclass, field
from typing import Any

from agent.kpi_discovery import DiscoveredKPI
from agent.semantic_graph import SemanticGraph


@dataclass
class SemanticContract:
    kpi_name: str
    description: str
    business_meaning: str
    related_models: list[str] = field(default_factory=list)
    related_columns: list[str] = field(default_factory=list)
    upstream_sources: list[str] = field(default_factory=list)
    downstream_consumers: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    confidence: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeReport:
    contracts: list[SemanticContract] = field(default_factory=list)
    confidence: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def build_semantic_knowledge(
    *,
    discovered_kpis,
    semantic_graph,
    project_context,
) -> KnowledgeReport:
    kpis = list(discovered_kpis or [])
    context = dict(project_context or {})
    contracts = [
        _contract_for_kpi(kpi, semantic_graph, context)
        for kpi in sorted(kpis, key=lambda item: item.name)
    ]
    return KnowledgeReport(
        contracts=contracts,
        confidence=_report_confidence(contracts),
        metadata={
            "kpi_count": len(contracts),
            "context_sources": sorted(context.keys()),
        },
    )


def _contract_for_kpi(
    kpi: DiscoveredKPI,
    semantic_graph: SemanticGraph,
    project_context: dict[str, Any],
) -> SemanticContract:
    upstream = _upstream_nodes(semantic_graph, kpi.name)
    downstream = _downstream_nodes(semantic_graph, kpi.name)
    upstream_models = _nodes_of_type(semantic_graph, upstream, "model")
    upstream_sources = _nodes_of_type(semantic_graph, upstream, "source")
    related_models = _ordered_unique([*list(kpi.related_models or []), *upstream_models])
    related_columns = _ordered_unique(list(kpi.related_columns or []))
    assumptions = _assumptions(kpi, project_context)
    invariants = _invariants(kpi, project_context)

    return SemanticContract(
        kpi_name=kpi.name,
        description=kpi.description,
        business_meaning=_business_meaning(kpi, project_context),
        related_models=related_models,
        related_columns=related_columns,
        upstream_sources=upstream_sources,
        downstream_consumers=downstream,
        assumptions=assumptions,
        invariants=invariants,
        confidence=_contract_confidence(
            kpi,
            related_models,
            related_columns,
            upstream_sources,
            downstream,
            assumptions,
            invariants,
            project_context,
        ),
        metadata={
            "industry_hint": kpi.industry_hint,
            "kpi_confidence": kpi.confidence,
            "evidence_reasons": list(kpi.reasons or []),
            "matched_sources": list((kpi.metadata or {}).get("matched_sources", [])),
        },
    )


def _business_meaning(kpi: DiscoveredKPI, project_context: dict[str, Any]) -> str:
    text = _evidence_text(kpi, project_context)
    name = _normalise(kpi.name)
    if "mrr" in name or "recurring_revenue" in text or "subscription_revenue" in text:
        return "Represents recurring subscription revenue."
    if ("revenue" in name and "gmv" not in name) or "payment" in text:
        return "Represents completed customer payments."
    if "gmv" in name or "gross_merchandise" in text:
        return "Represents gross merchandise value."
    if "playback" in name or "stream" in text:
        return "Represents successful playback sessions."
    if "retention" in name:
        return "Represents retained active users over time."
    if "conversion" in name:
        return "Represents conversion to a target action."
    return kpi.description


def _assumptions(kpi: DiscoveredKPI, project_context: dict[str, Any]) -> list[str]:
    text = _evidence_text(kpi, project_context)
    name = _normalise(kpi.name)
    assumptions = []
    if "revenue" in name or "gmv" in name or "payment" in text:
        assumptions.extend(["completed payments only", "non-negative", "currency consistent"])
    if "retention" in name or "churn" in name:
        assumptions.extend(["active users exist", "cohort definitions unchanged"])
    if "mrr" in name or "recurring" in name or "subscription" in text:
        assumptions.append("subscription lifecycle preserved")
    if "playback" in name or "stream" in text:
        assumptions.append("successful session events required")
    return _ordered_unique(assumptions)


def _invariants(kpi: DiscoveredKPI, project_context: dict[str, Any]) -> list[str]:
    text = _evidence_text(kpi, project_context)
    name = _normalise(kpi.name)
    if any(term in name for term in ["retention", "conversion", "rate", "success"]):
        return ["between 0 and 100%"]
    if "playback" in name and "rate" in text:
        return ["between 0 and 100%"]
    if any(term in name for term in ["revenue", "gmv", "mrr"]) or "payment" in text:
        return ["never negative"]
    return []


def _contract_confidence(
    kpi: DiscoveredKPI,
    related_models: list[str],
    related_columns: list[str],
    upstream_sources: list[str],
    downstream_consumers: list[str],
    assumptions: list[str],
    invariants: list[str],
    project_context: dict[str, Any],
) -> int:
    evidence_sources = len([key for key, value in project_context.items() if value])
    score = int(kpi.confidence or 0)
    score += min(len(related_models) * 5, 20)
    score += min(len(related_columns) * 5, 20)
    score += min(len(upstream_sources) * 5, 15)
    score += min(len(downstream_consumers) * 3, 9)
    score += min((len(assumptions) + len(invariants)) * 3, 18)
    score += min(evidence_sources * 3, 15)
    return min(100, score)


def _report_confidence(contracts: list[SemanticContract]) -> int:
    if not contracts:
        return 0
    return round(sum(contract.confidence for contract in contracts) / len(contracts))


def _upstream_nodes(semantic_graph: SemanticGraph, kpi_name: str) -> list[str]:
    if semantic_graph is None:
        return []
    return list(semantic_graph.upstream(kpi_name))


def _downstream_nodes(semantic_graph: SemanticGraph, kpi_name: str) -> list[str]:
    if semantic_graph is None:
        return []
    return list(semantic_graph.downstream(kpi_name))


def _nodes_of_type(semantic_graph: SemanticGraph, node_ids: list[str], node_type: str) -> list[str]:
    if semantic_graph is None:
        return []
    return [
        node_id for node_id in node_ids
        if node_id in semantic_graph.nodes and semantic_graph.nodes[node_id].type == node_type
    ]


def _evidence_text(kpi: DiscoveredKPI, project_context: dict[str, Any]) -> str:
    values = [
        kpi.name,
        kpi.description,
        *list(kpi.related_models or []),
        *list(kpi.related_columns or []),
        *_flatten(project_context),
    ]
    return " ".join(_normalise(value) for value in values)


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        flattened = []
        for key, item in value.items():
            flattened.extend(_flatten(key))
            flattened.extend(_flatten(item))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(_flatten(item))
        return flattened
    return [str(value)]


def _normalise(value: Any) -> str:
    text = str(value).lower()
    chars = []
    previous_was_separator = False
    for char in text:
        if char.isalnum():
            chars.append(char)
            previous_was_separator = False
        elif not previous_was_separator:
            chars.append("_")
            previous_was_separator = True
    return "".join(chars).strip("_")


def _ordered_unique(values) -> list[str]:
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique
