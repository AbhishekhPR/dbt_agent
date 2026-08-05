"""Versioned evidence coverage and decision policy.

Health is intentionally independent from evidence coverage.  Missing evidence
cannot manufacture a finding or subtract health; policy only determines the
decision escalation for material required sources.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class EvidenceState(str, Enum):
    EVALUATED = "EVALUATED"
    MISSING = "MISSING"
    FAILED = "FAILED"
    NOT_EVALUATED = "NOT EVALUATED"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED_BY_CREDENTIALS = "BLOCKED BY CREDENTIALS"
    # Evidence that was collected but is older than policy allows. It is
    # deliberately NOT EVALUATED-equivalent and deliberately not MISSING:
    # the distinction matters when explaining a decision, and treating stale
    # production state as current is the specific failure this release exists
    # to prevent. Because it is not EVALUATED, a required stale source still
    # yields coverage INCOMPLETE and WARN/BLOCK by mode.
    STALE = "STALE"
    # Required evidence that has been requested and is still outstanding. A
    # review in this state has not failed; it has not finished.
    PENDING = "PENDING"


class EvidenceRequirement(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"


@dataclass(frozen=True)
class EvidencePolicyVersion:
    version: str
    requirements: Mapping[str, EvidenceRequirement]
    content_hash: str

    @classmethod
    def create(
        cls,
        version: str,
        requirements: Mapping[str, EvidenceRequirement | str],
    ) -> "EvidencePolicyVersion":
        normalized = {
            str(name): _requirement(value)
            for name, value in sorted(requirements.items())
        }
        payload = {name: value.value for name, value in normalized.items()}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            version=str(version),
            requirements=normalized,
            content_hash=digest,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EvidencePolicyVersion":
        if not isinstance(payload, Mapping):
            raise ValueError("evidence_policy must be an object")
        version = payload.get("version")
        sources = payload.get("sources")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("evidence_policy.version must be a non-empty string")
        if not isinstance(sources, Mapping) or not sources:
            raise ValueError("evidence_policy.sources must be a non-empty object")
        return cls.create(version, sources)


@dataclass(frozen=True)
class EvidenceCoverageResult:
    health: int
    coverage: str
    decision: str
    evidence: dict[str, EvidenceState] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    unevaluated: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    required_missing: list[str] = field(default_factory=list)
    policy_version: str = ""
    policy_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "health": self.health,
            "coverage": self.coverage,
            "decision": self.decision,
            "evidence": {
                name: state.value for name, state in self.evidence.items()
            },
            "reasons": list(self.reasons),
            "unevaluated": list(self.unevaluated),
            "unsupported": list(self.unsupported),
            "required_missing": list(self.required_missing),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }


def default_policy() -> EvidencePolicyVersion:
    return EvidencePolicyVersion.create(
        "default-v1",
        {
            "base_manifest": EvidenceRequirement.REQUIRED,
            "head_manifest": EvidenceRequirement.REQUIRED,
            "changed_files": EvidenceRequirement.REQUIRED,
            "changed_models": EvidenceRequirement.REQUIRED,
            "history": EvidenceRequirement.OPTIONAL,
            "warehouse_metadata": EvidenceRequirement.OPTIONAL,
            "slack": EvidenceRequirement.OPTIONAL,
            "dashboard": EvidenceRequirement.OPTIONAL,
        },
    )


def evaluate_evidence_policy(
    *,
    mode: str,
    policy: EvidencePolicyVersion,
    evidence: Mapping[str, EvidenceState | str],
    health: int,
) -> EvidenceCoverageResult:
    normalized_mode = str(mode).lower()
    if normalized_mode not in {"shadow", "enforce"}:
        raise ValueError("mode must be shadow or enforce")
    if not 0 <= int(health) <= 100:
        raise ValueError("health must be between 0 and 100")

    states = {
        str(name): _state(value)
        for name, value in sorted(evidence.items())
    }
    required_missing = []
    unevaluated = []
    unsupported = []
    reasons = []
    for name, state in states.items():
        requirement = policy.requirements.get(name, EvidenceRequirement.OPTIONAL)
        if requirement == EvidenceRequirement.DISABLED:
            continue
        if state == EvidenceState.UNSUPPORTED:
            unsupported.append(name)
        if state == EvidenceState.NOT_EVALUATED:
            unevaluated.append(name)
        if requirement == EvidenceRequirement.REQUIRED and state != EvidenceState.EVALUATED:
            required_missing.append(name)
            reasons.append(f"Required evidence unavailable: {name} ({state.value})")

    coverage = "INCOMPLETE" if required_missing else "COMPLETE"
    decision = _health_decision(int(health))
    if required_missing:
        decision = "WARN" if normalized_mode == "shadow" else "BLOCK"
    return EvidenceCoverageResult(
        health=int(health),
        coverage=coverage,
        decision=decision,
        evidence=states,
        reasons=reasons,
        unevaluated=unevaluated,
        unsupported=unsupported,
        required_missing=required_missing,
        policy_version=policy.version,
        policy_hash=policy.content_hash,
    )


def _state(value: EvidenceState | str) -> EvidenceState:
    if isinstance(value, EvidenceState):
        return value
    text = str(value).upper().replace("_", " ")
    for state in EvidenceState:
        if state.value == text:
            return state
    raise ValueError(f"Unknown evidence state: {value}")


def _requirement(value: EvidenceRequirement | str) -> EvidenceRequirement:
    if isinstance(value, EvidenceRequirement):
        return value
    return EvidenceRequirement(str(value).lower())


def _health_decision(health: int) -> str:
    if health < 70:
        return "BLOCK"
    if health < 90:
        return "WARN"
    return "ALLOW"
