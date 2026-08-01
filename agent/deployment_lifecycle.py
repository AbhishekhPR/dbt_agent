import copy
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

from agent.deployment_history import record_deployment_snapshot
from agent.pr_analysis import analyze_pr_with_history


@dataclass
class DeploymentReviewResult:
    incident: Any
    current_snapshot: dict[str, Any] | None
    previous_snapshot: dict[str, Any] | None
    previous_snapshot_loaded: bool
    saved_snapshot_id: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _serializable(self)


def review_deployment(
    *,
    changed_models,
    project_context,
    history_store=None,
    deployment_id=None,
    metadata=None,
    events=None,
    auto_record=False,
    allow_blocked_recording=False,
    metadata_db_path=None,
    previous_snapshot=None,
    **options,
) -> DeploymentReviewResult:
    previous_snapshot = copy.deepcopy(previous_snapshot) if previous_snapshot is not None else _load_previous_snapshot(history_store)
    incident = analyze_pr_with_history(
        changed_models=copy.deepcopy(list(changed_models or [])),
        project_context=copy.deepcopy(project_context or {}),
        history_store=history_store,
        deployment_id=deployment_id,
        metadata=copy.deepcopy(metadata or {}),
        events=copy.deepcopy(events) if events is not None else None,
        metadata_db_path=metadata_db_path,
        previous_snapshot=previous_snapshot,
        **copy.deepcopy(options),
    )

    current_snapshot = _current_snapshot(incident)
    previous_snapshot_loaded = bool(
        (incident.metadata or {}).get(
            "previous_snapshot_loaded",
            previous_snapshot is not None,
        )
    )
    if previous_snapshot is None and previous_snapshot_loaded:
        previous_snapshot = _previous_snapshot_from_metadata(incident)

    saved_snapshot_id = None
    if auto_record and history_store is not None:
        saved_snapshot_id = record_deployment_snapshot(
            history_store=history_store,
            incident=incident,
            allow_blocked=allow_blocked_recording,
        )

    return DeploymentReviewResult(
        incident=incident,
        current_snapshot=current_snapshot,
        previous_snapshot=previous_snapshot,
        previous_snapshot_loaded=previous_snapshot_loaded,
        saved_snapshot_id=saved_snapshot_id,
        metadata={
            "history_enabled": history_store is not None,
            "previous_snapshot_loaded": previous_snapshot_loaded,
            "previous_snapshot_id": _snapshot_id(previous_snapshot),
            "current_snapshot_id": _snapshot_id(current_snapshot),
            "auto_record": auto_record,
            "allow_blocked_recording": allow_blocked_recording,
            "metadata_db_enabled": metadata_db_path is not None,
            "request_metadata": copy.deepcopy(metadata or {}),
            "options": copy.deepcopy(options),
        },
    )


def _load_previous_snapshot(history_store):
    if history_store is None:
        return None
    previous_snapshot = history_store.load_latest_snapshot()
    if previous_snapshot is None:
        return None
    return copy.deepcopy(previous_snapshot)


def _current_snapshot(incident) -> dict[str, Any] | None:
    current_snapshot = (getattr(incident, "metadata", {}) or {}).get("current_snapshot")
    if current_snapshot is None:
        return None
    return copy.deepcopy(current_snapshot)


def _previous_snapshot_from_metadata(incident) -> dict[str, Any] | None:
    snapshot_id = (getattr(incident, "metadata", {}) or {}).get("previous_snapshot_id")
    if not snapshot_id:
        return None
    return {"snapshot_id": str(snapshot_id)}


def _snapshot_id(snapshot) -> str | None:
    if not snapshot:
        return None
    if isinstance(snapshot, dict):
        value = snapshot.get("snapshot_id")
    else:
        value = getattr(snapshot, "snapshot_id", None)
    return str(value) if value else None


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
