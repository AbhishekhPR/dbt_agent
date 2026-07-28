import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from agent.incident import Incident
from agent.incident_builder import summarize_incident
from agent.semantic_context import SemanticContext


@dataclass
class DeploymentSnapshot:
    snapshot_id: str
    deployment_id: str
    created_at: str
    changed_models: list[str] = field(default_factory=list)
    semantic_context: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] | None = None
    incident_summary: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serializable(self)


def create_deployment_snapshot(
    *,
    deployment_id,
    changed_models,
    semantic_context,
    decision=None,
    incident=None,
    metadata=None,
) -> DeploymentSnapshot:
    changed = [str(model) for model in list(changed_models or [])]
    return DeploymentSnapshot(
        snapshot_id=_snapshot_id(deployment_id, changed),
        deployment_id=str(deployment_id),
        created_at=datetime.now(timezone.utc).isoformat(),
        changed_models=changed,
        semantic_context=_semantic_context_dict(semantic_context),
        decision=_decision_dict(decision),
        incident_summary=_incident_summary(incident),
        metadata=copy.deepcopy(metadata or {}),
    )


def compare_snapshot_identity(first: DeploymentSnapshot, second: DeploymentSnapshot) -> bool:
    return first.snapshot_id == second.snapshot_id


def _snapshot_id(deployment_id, changed_models: list[str]) -> str:
    payload = {
        "deployment_id": str(deployment_id),
        "changed_models": list(changed_models),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"snap_{digest[:16]}"


def _semantic_context_dict(semantic_context) -> dict[str, Any]:
    if semantic_context is None:
        return {}
    if hasattr(semantic_context, "to_dict"):
        return copy.deepcopy(semantic_context.to_dict())
    if isinstance(semantic_context, dict):
        return copy.deepcopy(semantic_context)
    return _serializable(semantic_context)


def _decision_dict(decision) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "health": decision.health,
        "decision": _enum_value(decision.decision),
        "severity": _enum_value(decision.severity),
        "confidence": decision.confidence,
        "reasons": list(decision.reasons or []),
        "signal_count": len(decision.signals or []),
    }


def _incident_summary(incident: Incident | None) -> dict[str, Any] | None:
    if incident is None:
        return None
    return _serializable(summarize_incident(incident))


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


def _enum_value(value):
    if isinstance(value, Enum):
        return value.value
    return value
