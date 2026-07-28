from dataclasses import dataclass
from pathlib import Path

from agent.dbt_context import load_project_context_from_manifest_path
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_snapshot import create_deployment_snapshot
from agent.semantic_context import build_semantic_context


@dataclass
class BaselineResult:
    snapshot_id: str
    deployment_id: str
    model_count: int
    kpi_count: int
    history_path: str


def initialize_production_baseline(
    *,
    dbt_manifest_path: str,
    history_path: str = ".relium/deployment_history.json",
    deployment_id: str = "production-baseline",
) -> BaselineResult:
    project_context = load_project_context_from_manifest_path(dbt_manifest_path)
    model_names = list(project_context.get("model_names") or [])
    if not model_names:
        raise ValueError("No dbt models found in manifest.")

    semantic_context = build_semantic_context(
        project_context=project_context,
        changed_models=model_names,
    )
    snapshot = create_deployment_snapshot(
        deployment_id=deployment_id,
        changed_models=model_names,
        semantic_context=semantic_context,
        metadata={
            "source": "production_baseline",
            "dbt_manifest_path": str(dbt_manifest_path),
            "model_count": len(model_names),
        },
    )
    store = DeploymentHistoryStore(history_path)
    store.save_snapshot(snapshot)

    return BaselineResult(
        snapshot_id=snapshot.snapshot_id,
        deployment_id=str(deployment_id),
        model_count=len(model_names),
        kpi_count=len(semantic_context.discovered_kpis or []),
        history_path=str(Path(history_path)),
    )
