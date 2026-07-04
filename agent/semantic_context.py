import copy
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

from agent.column_lineage import build_column_lineage_graph
from agent.kpi_discovery import discover_kpis
from agent.semantic_contract_validation import validate_semantic_contracts
from agent.semantic_graph import build_semantic_graph
from agent.semantic_knowledge import build_semantic_knowledge
from agent.semantic_kpi_inference import infer_impacted_kpis


@dataclass
class SemanticContext:
    project_context: dict[str, Any] = field(default_factory=dict)
    discovered_kpis: list[Any] = field(default_factory=list)
    semantic_graph: Any = None
    column_lineage_graph: Any = None
    kpi_impact_report: Any = None
    knowledge_report: Any = None
    contract_validation_result: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)


def build_semantic_context(
    *,
    project_context,
    changed_models=None,
    metadata=None,
) -> SemanticContext:
    context = copy.deepcopy(project_context or {})
    changed = list(changed_models or [])
    validation_metadata = copy.deepcopy(metadata or {})

    discovered_kpis = discover_kpis(context)
    semantic_graph = build_semantic_graph(context)
    column_lineage_graph = build_column_lineage_graph(context)
    kpi_impact_report = None
    if changed:
        kpi_impact_report = infer_impacted_kpis(
            changed_models=changed,
            discovered_kpis=discovered_kpis,
            semantic_graph=semantic_graph,
            column_lineage_graph=column_lineage_graph,
            changed_columns_by_model=validation_metadata.get("changed_columns_by_model"),
        )

    knowledge_report = build_semantic_knowledge(
        discovered_kpis=discovered_kpis,
        semantic_graph=semantic_graph,
        project_context=context,
    )
    contract_validation_result = validate_semantic_contracts(
        contracts=knowledge_report.contracts,
        changed_models=changed,
        metadata=validation_metadata,
        kpi_impact_report=kpi_impact_report,
    )

    return SemanticContext(
        project_context=context,
        discovered_kpis=discovered_kpis,
        semantic_graph=semantic_graph,
        column_lineage_graph=column_lineage_graph,
        kpi_impact_report=kpi_impact_report,
        knowledge_report=knowledge_report,
        contract_validation_result=contract_validation_result,
        metadata={
            "changed_models": changed,
            "has_kpi_impact_report": kpi_impact_report is not None,
            "kpi_count": len(discovered_kpis),
            "contract_count": len(knowledge_report.contracts),
        },
    )


def to_dict(context: SemanticContext) -> dict:
    return context.to_dict()


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
