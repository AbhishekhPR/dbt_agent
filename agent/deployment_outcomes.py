import copy
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agent.decision_engine import DeploymentDecision
from agent.signals import Severity, Signal


@dataclass
class DeploymentOutcome:
    outcome_id: str
    deployment_id: str
    snapshot_id: str | None = None
    decision: str | DeploymentDecision = ""
    outcome: str = ""
    created_at: str = ""
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.outcome_id = str(self.outcome_id)
        self.deployment_id = str(self.deployment_id)
        self.snapshot_id = str(self.snapshot_id) if self.snapshot_id else None
        self.decision = _enum_value(self.decision)
        self.outcome = str(self.outcome)
        self.created_at = str(self.created_at)
        self.notes = str(self.notes) if self.notes is not None else None
        self.metadata = copy.deepcopy(dict(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "deployment_id": self.deployment_id,
            "snapshot_id": self.snapshot_id,
            "decision": _enum_value(self.decision),
            "outcome": self.outcome,
            "created_at": self.created_at,
            "notes": self.notes,
            "metadata": _serializable(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        if not isinstance(payload, dict):
            raise ValueError("Outcome payload must be an object.")

        missing = [
            field_name
            for field_name in (
                "outcome_id",
                "deployment_id",
                "decision",
                "outcome",
                "created_at",
            )
            if not payload.get(field_name)
        ]
        if missing:
            raise ValueError(f"Outcome payload is missing: {', '.join(missing)}")

        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("Outcome metadata must be an object.")

        return cls(
            outcome_id=payload["outcome_id"],
            deployment_id=payload["deployment_id"],
            snapshot_id=payload.get("snapshot_id"),
            decision=payload["decision"],
            outcome=payload["outcome"],
            created_at=payload["created_at"],
            notes=payload.get("notes"),
            metadata=metadata,
        )


@dataclass
class OutcomeHistoryAnalysis:
    severity: Severity
    confidence: int
    score: int
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serializable(self)


class DeploymentOutcomeStore:
    def __init__(self, path):
        self.path = Path(path)

    def save_outcome(self, outcome) -> None:
        outcome_dict = _outcome_dict(outcome)
        outcome_id = outcome_dict.get("outcome_id")
        if not outcome_id:
            raise ValueError("Outcome must include an outcome_id.")

        outcomes = [
            existing
            for existing in self._read_outcome_dicts()
            if existing.get("outcome_id") != outcome_id
        ]
        outcomes.append(outcome_dict)
        self._write_outcomes(outcomes)

    def list_outcomes(self) -> list[DeploymentOutcome]:
        return [
            copy.deepcopy(outcome)
            for outcome in self._read_outcomes()
        ]

    def list_by_deployment(self, deployment_id) -> list[DeploymentOutcome]:
        deployment_id = str(deployment_id)
        return [
            outcome
            for outcome in self.list_outcomes()
            if outcome.deployment_id == deployment_id
        ]

    def latest_for_deployment(self, deployment_id) -> DeploymentOutcome | None:
        outcomes = self.list_by_deployment(deployment_id)
        if not outcomes:
            return None
        return copy.deepcopy(outcomes[-1])

    def summarize_outcomes(self) -> dict[str, int]:
        summary = {
            "total_outcomes": 0,
            "false_positives": 0,
            "incidents_after_allow": 0,
            "incidents_after_warn": 0,
            "incidents_after_block": 0,
            "accepted_risks": 0,
            "reverted_deployments": 0,
            "blocked_deployments": 0,
            "manually_approved_deployments": 0,
        }

        for outcome in self.list_outcomes():
            decision = str(outcome.decision).upper()
            outcome_name = str(outcome.outcome).lower()
            summary["total_outcomes"] += 1

            if outcome_name == "false_positive":
                summary["false_positives"] += 1
            if outcome_name == "incident_occurred":
                if decision == DeploymentDecision.ALLOW.value:
                    summary["incidents_after_allow"] += 1
                elif decision == DeploymentDecision.WARN.value:
                    summary["incidents_after_warn"] += 1
                elif decision == DeploymentDecision.BLOCK.value:
                    summary["incidents_after_block"] += 1
            if outcome_name == "accepted_risk":
                summary["accepted_risks"] += 1
            if outcome_name == "reverted":
                summary["reverted_deployments"] += 1
            if outcome_name == "blocked":
                summary["blocked_deployments"] += 1
            if outcome_name == "manually_approved":
                summary["manually_approved_deployments"] += 1

        return summary

    def _read_outcomes(self) -> list[DeploymentOutcome]:
        outcomes = []
        for payload in self._read_outcome_dicts():
            try:
                outcomes.append(DeploymentOutcome.from_dict(payload))
            except (TypeError, ValueError):
                continue
        return outcomes

    def _read_outcome_dicts(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return []

        outcomes = payload.get("outcomes") if isinstance(payload, dict) else payload
        if not isinstance(outcomes, list):
            return []
        return [
            copy.deepcopy(outcome)
            for outcome in outcomes
            if isinstance(outcome, dict)
        ]

    def _write_outcomes(self, outcomes: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"outcomes": [_serializable(outcome) for outcome in outcomes]}
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


def analyze_outcome_history(
    outcomes,
    deployment_id=None,
    changed_models=None,
    root_cause=None,
) -> OutcomeHistoryAnalysis:
    outcome_list = [_coerce_outcome(outcome) for outcome in list(outcomes or [])]
    outcome_dicts = [outcome.to_dict() for outcome in outcome_list]

    counts = {
        "false_positive_after_block_count": 0,
        "incident_after_allow_or_warn_count": 0,
        "reverted_after_allow_or_warn_count": 0,
        "accepted_risk_count": 0,
        "fixed_before_merge_after_block_count": 0,
    }
    supporting_ids: dict[str, list[str]] = {
        key: []
        for key in counts
    }

    for outcome in outcome_list:
        decision = str(outcome.decision).upper()
        outcome_name = str(outcome.outcome).lower()

        if decision == DeploymentDecision.BLOCK.value and outcome_name == "false_positive":
            _increment_outcome_count(
                counts,
                supporting_ids,
                "false_positive_after_block_count",
                outcome.outcome_id,
            )
        elif decision in _ALLOW_WARN and outcome_name == "incident_occurred":
            _increment_outcome_count(
                counts,
                supporting_ids,
                "incident_after_allow_or_warn_count",
                outcome.outcome_id,
            )
        elif decision in _ALLOW_WARN and outcome_name == "reverted":
            _increment_outcome_count(
                counts,
                supporting_ids,
                "reverted_after_allow_or_warn_count",
                outcome.outcome_id,
            )
        elif outcome_name == "accepted_risk":
            _increment_outcome_count(
                counts,
                supporting_ids,
                "accepted_risk_count",
                outcome.outcome_id,
            )
        elif decision == DeploymentDecision.BLOCK.value and outcome_name == "fixed_before_merge":
            _increment_outcome_count(
                counts,
                supporting_ids,
                "fixed_before_merge_after_block_count",
                outcome.outcome_id,
            )

    reasons = []
    if counts["false_positive_after_block_count"]:
        reasons.append("Previous BLOCK decisions were marked false positive")
    if counts["incident_after_allow_or_warn_count"]:
        reasons.append("Previous allowed or warned deployments were followed by incidents")
    if counts["reverted_after_allow_or_warn_count"]:
        reasons.append("Previous allowed or warned deployments were reverted")
    if counts["accepted_risk_count"]:
        reasons.append("Similar risk was previously accepted")
    if counts["fixed_before_merge_after_block_count"]:
        reasons.append("Previous block led to fix before merge")

    risk_count = (
        counts["incident_after_allow_or_warn_count"]
        + counts["reverted_after_allow_or_warn_count"]
        + counts["fixed_before_merge_after_block_count"]
    )
    if risk_count >= 2:
        severity = Severity.HIGH
        score = -20
    elif risk_count == 1:
        severity = Severity.MEDIUM
        score = -10
    else:
        severity = Severity.LOW
        score = 0

    supporting_count = sum(counts.values())
    confidence = 0 if supporting_count == 0 else min(95, 65 + (supporting_count * 8))
    metadata = {
        "total_outcomes": len(outcome_list),
        "deployment_id": str(deployment_id) if deployment_id else None,
        "changed_models": [str(model) for model in list(changed_models or [])],
        "root_cause": str(root_cause) if root_cause else None,
        "supporting_outcome_ids": {
            key: list(ids)
            for key, ids in supporting_ids.items()
            if ids
        },
        "outcomes": outcome_dicts,
        **counts,
    }
    return OutcomeHistoryAnalysis(
        severity=severity,
        confidence=confidence,
        score=score,
        reasons=reasons,
        metadata=metadata,
    )


def outcome_history_to_signal(analysis: OutcomeHistoryAnalysis) -> Signal | None:
    if analysis is None or not analysis.reasons:
        return None
    return Signal(
        component="deployment_outcomes",
        severity=analysis.severity,
        confidence=analysis.confidence,
        score=analysis.score,
        reasons=list(analysis.reasons),
        metadata=copy.deepcopy(dict(analysis.metadata or {})),
    )


def _outcome_dict(outcome) -> dict[str, Any]:
    if hasattr(outcome, "to_dict"):
        return copy.deepcopy(outcome.to_dict())
    return DeploymentOutcome.from_dict(copy.deepcopy(dict(outcome or {}))).to_dict()


def _coerce_outcome(outcome) -> DeploymentOutcome:
    return DeploymentOutcome.from_dict(_outcome_dict(outcome))


def _increment_outcome_count(counts, supporting_ids, key, outcome_id) -> None:
    counts[key] += 1
    supporting_ids[key].append(str(outcome_id))


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


def _enum_value(value) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


_ALLOW_WARN = {
    DeploymentDecision.ALLOW.value,
    DeploymentDecision.WARN.value,
}
