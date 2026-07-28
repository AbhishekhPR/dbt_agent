import copy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agent.dbt_context import load_project_context_from_manifest_path
from agent.deployment_history import DeploymentHistoryStore
from agent.deployment_lifecycle import DeploymentReviewResult, review_deployment
from agent.deployment_snapshot import create_deployment_snapshot
from agent.semantic_context import build_semantic_context


@dataclass
class BacktestResult:
    review: DeploymentReviewResult
    historical_deployment_id: str
    baseline_source: str

    @property
    def incident(self):
        return self.review.incident

    @property
    def would_have_decision(self) -> str:
        return _enum_value(getattr(self.incident, "decision", "UNKNOWN"))

    @property
    def would_have_health(self) -> int:
        return int(getattr(self.incident, "health", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "historical_deployment_id": self.historical_deployment_id,
            "baseline_source": self.baseline_source,
            "would_have_decision": self.would_have_decision,
            "would_have_health": self.would_have_health,
            "review": self.review.to_dict(),
        }


def backtest_deployment(
    *,
    dbt_manifest_path: str,
    changed_models,
    baseline_manifest_path: str | None = None,
    history_path: str = ".relium/deployment_history.json",
    deployment_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> BacktestResult:
    changed = [str(model) for model in list(changed_models or []) if str(model)]
    if not changed:
        raise ValueError("At least one changed model is required for backtest.")

    project_context = load_project_context_from_manifest_path(dbt_manifest_path)
    history_store, baseline_source = _backtest_history_store(
        baseline_manifest_path=baseline_manifest_path,
        history_path=history_path,
    )

    review = review_deployment(
        changed_models=_changed_model_specs(changed),
        project_context=project_context,
        history_store=history_store,
        deployment_id=deployment_id or "historical-backtest",
        metadata={
            "mode": "backtest",
            "historical_deployment_id": deployment_id or "historical-backtest",
            "baseline_source": baseline_source,
            **copy.deepcopy(metadata or {}),
        },
        auto_record=False,
        allow_blocked_recording=False,
    )

    return BacktestResult(
        review=review,
        historical_deployment_id=deployment_id or "historical-backtest",
        baseline_source=baseline_source,
    )


def _backtest_history_store(*, baseline_manifest_path, history_path):
    if baseline_manifest_path:
        snapshot = _baseline_snapshot_from_manifest(baseline_manifest_path)
        return _SingleSnapshotHistoryStore(snapshot), "baseline_manifest"

    store = DeploymentHistoryStore(history_path)
    if store.load_latest_snapshot() is None:
        raise ValueError(
            "Backtest requires a previous production snapshot. Provide "
            "--baseline-manifest or a --history-path containing at least one snapshot."
        )
    return store, "history_path"


def _baseline_snapshot_from_manifest(manifest_path: str) -> dict[str, Any]:
    project_context = load_project_context_from_manifest_path(manifest_path)
    model_names = list(project_context.get("model_names") or [])
    if not model_names:
        raise ValueError("No dbt models found in baseline manifest.")

    semantic_context = build_semantic_context(
        project_context=project_context,
        changed_models=model_names,
    )
    snapshot = create_deployment_snapshot(
        deployment_id="backtest-baseline",
        changed_models=model_names,
        semantic_context=semantic_context,
        metadata={
            "source": "backtest_baseline",
            "dbt_manifest_path": str(manifest_path),
            "model_count": len(model_names),
        },
    )
    return snapshot.to_dict()


def _changed_model_specs(changed_models: list[str]) -> list[dict[str, str]]:
    return [
        {
            "model_name": Path(model).stem,
            "sql": f"select * from {Path(model).stem}",
        }
        for model in changed_models
    ]


class _SingleSnapshotHistoryStore:
    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)

    def load_latest_snapshot(self):
        return copy.deepcopy(self.snapshot)

    def list_snapshots(self):
        return [copy.deepcopy(self.snapshot)]


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return str(value)
