"""Authoritative PostgreSQL review lifecycle for the served GitHub path.

Before this module the GitHub review path wrote only to a filesystem store and
the PostgreSQL lifecycle store was a separate world; a review from a genuine
webhook never reached the `reviews` table, so it could not be bound to
production evidence, recomputed, or shown on the dashboard.

This is the single place where a live review becomes durable state. The
filesystem store keeps only its delivery de-duplication role.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.evidence_policy import EvidenceRequirement, default_policy
from agent.metadata_evidence.collection_plan import (
    build_collection_plan,
    manifest_hash,
    ttl_minutes_for,
)
from agent.metadata_evidence.decision import (
    PRODUCTION_METADATA,
    evaluate_metadata_decision,
)

# How long a collection request stays actionable. After this the review stops
# waiting and the policy applies to the absent evidence.
REQUEST_TTL_MINUTES = 30

# Which of the three product states each evidence source describes.
EVIDENCE_GROUPS = {
    "base_manifest": "base_code",
    "head_manifest": "head_code",
    "changed_files": "head_code",
    "changed_models": "head_code",
    PRODUCTION_METADATA: "production",
}


def review_id_for(repository_id, pull_number, head_sha) -> str:
    """Stable per-(repository, PR, head SHA) review identity.

    A redelivery of the same head SHA resolves to the SAME review, which is
    what makes duplicate webhook handling idempotent. A new head SHA resolves
    to a different review, which is what makes it a new attempt.
    """
    digest = hashlib.sha256(
        f"{repository_id}:{pull_number}:{head_sha}".encode()).hexdigest()
    return f"gh-{digest[:32]}"


@dataclass
class LifecycleOutcome:
    review_id: str
    attempt: int
    lifecycle_state: str
    decision: str | None
    coverage: str
    health: int
    metadata_required: bool
    request_id: str | None
    plan: dict
    findings: list
    evidence: dict
    waiting: bool
    policy_version: str
    policy_hash: str

    def as_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "attempt": self.attempt,
            "lifecycle_state": self.lifecycle_state,
            "decision": self.decision,
            "coverage": self.coverage,
            "health": self.health,
            "metadata_required": self.metadata_required,
            "request_id": self.request_id,
            "plan": dict(self.plan),
            "findings": list(self.findings),
            "evidence": dict(self.evidence),
            "waiting": self.waiting,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }


def _evidence_rows(decision):
    rows = {}
    policy = default_policy()
    for source, state in decision.evidence.items():
        requirement = policy.requirements.get(source, EvidenceRequirement.OPTIONAL)
        requirement = getattr(requirement, "value", str(requirement))
        if source == PRODUCTION_METADATA:
            requirement = "required" if decision.evidence_states_available.get(
                "production") or state in ("PENDING", "MISSING", "STALE") else "optional"
        detail = None
        for finding in decision.findings:
            if finding.category == "evidence":
                detail = finding.message
                break
        rows[source] = (requirement, state, EVIDENCE_GROUPS.get(source), detail)
    return rows


def begin_review(store, *, organization_id, repository_id, environment,
                 pull_number, base_sha, head_sha, base_manifest, head_manifest,
                 changed_models, enforcement_mode, delivery_id=None,
                 code_health=100, code_findings=(), critical_models=(),
                 evidence_level="profile", now=None):
    """Persist a genuine GitHub review and decide what to publish.

    Returns a LifecycleOutcome. When production evidence is required and not
    yet available the outcome carries ``waiting=True`` and ``decision=None`` -
    the review has not failed, it has not finished.
    """
    now = now or datetime.now(timezone.utc)
    review_id = review_id_for(repository_id, pull_number, head_sha)
    base_hash = manifest_hash(base_manifest)
    head_hash = manifest_hash(head_manifest)
    policy = default_policy()

    plan = build_collection_plan(
        base_manifest=base_manifest,
        head_manifest=head_manifest,
        changed_models=changed_models,
        evidence_level=evidence_level,
        critical_models=critical_models,
    )
    plan_dict = plan.as_dict()

    store.ensure_tenant(organization_id, repository_id, environment)
    review = store.upsert_pr_review(
        organization_id, repository_id, environment,
        review_id=review_id, pull_number=pull_number,
        base_sha=base_sha, head_sha=head_sha,
        base_manifest_hash=base_hash, head_manifest_hash=head_hash,
        enforcement_mode=enforcement_mode,
        policy_version=policy.version, policy_hash=policy.content_hash,
        github_delivery_id=delivery_id,
        metadata_required=plan_dict["metadata_required"],
        payload={"plan": plan_dict},
    )
    attempt = int(review.get("attempt") or 1)

    store.transition_review(organization_id, repository_id, review_id,
                            "CODE_ANALYSIS_COMPLETE",
                            reason="base and head manifests analysed")

    # An accepted snapshot may already exist for this exact review - for
    # example on a webhook redelivery after the collector already responded.
    snapshot = store.latest_accepted_snapshot(organization_id, repository_id, review_id)

    request_id = None
    if plan_dict["metadata_required"] and snapshot is None:
        # The request is persisted BEFORE the waiting state is published, so a
        # crash between the two can never leave a review waiting on a request
        # that does not exist.
        request_id = f"req-{review_id}-{attempt}"
        criticality = "critical" if any(
            t["criticality"] == "critical" for t in plan_dict["targets"]) else "standard"
        existing = store.get_collection_request(organization_id, repository_id, request_id)
        if existing is None:
            store.create_collection_request(
                organization_id, repository_id, environment,
                request_id=request_id, review_id=review_id, reason="pr_review",
                expires_at=now + timedelta(minutes=REQUEST_TTL_MINUTES),
                targets=[t for t in plan_dict["targets"]
                         if t["dependency_kind"] == "external"],
                base_sha=base_sha, head_sha=head_sha,
                base_manifest_hash=base_hash, head_manifest_hash=head_hash,
                priority=criticality,
                required_evidence_level=plan_dict["required_evidence_level"],
                plan={"attempt": attempt,
                      "policy_version": policy.version,
                      "policy_hash": policy.content_hash,
                      "ttl_minutes": ttl_minutes_for(criticality),
                      "downstream_models": plan_dict["downstream_models"],
                      "added_dependencies": plan_dict["added_dependencies"],
                      "removed_dependencies": plan_dict["removed_dependencies"]},
            )
        store.transition_review(organization_id, repository_id, review_id,
                                "METADATA_REQUESTED",
                                reason=f"targeted collection request {request_id}")

    decision = evaluate_metadata_decision(
        plan=plan_dict, snapshot=snapshot, enforcement_mode=enforcement_mode,
        code_health=code_health, code_findings=code_findings, policy=policy, now=now,
    )

    store.transition_review(organization_id, repository_id, review_id,
                            decision.lifecycle_state,
                            reason="metadata evidence evaluated")

    waiting = decision.decision is None
    store.record_review_decision(
        organization_id, repository_id, review_id,
        decision=decision.decision, evidence_coverage=decision.coverage,
        health=decision.health, attempt=attempt,
        trigger="initial", enforcement_mode=enforcement_mode,
        policy_version=decision.policy_version, policy_hash=decision.policy_hash,
        payload={"findings": [f.as_dict() for f in decision.findings],
                 "plan": plan_dict},
    )
    store.record_evidence_states(organization_id, repository_id, review_id, attempt,
                                 _evidence_rows(decision))
    store.append_audit(
        organization_id, repository_id, actor="github-app",
        event_type="review.analysed", reference_type="review", reference_id=review_id,
        payload={"attempt": attempt, "lifecycle_state": decision.lifecycle_state,
                 "decision": decision.decision, "coverage": decision.coverage,
                 "waiting": waiting},
    )

    return LifecycleOutcome(
        review_id=review_id, attempt=attempt,
        lifecycle_state=decision.lifecycle_state, decision=decision.decision,
        coverage=decision.coverage, health=decision.health,
        metadata_required=plan_dict["metadata_required"], request_id=request_id,
        plan=plan_dict, findings=[f.as_dict() for f in decision.findings],
        evidence=decision.evidence, waiting=waiting,
        policy_version=decision.policy_version, policy_hash=decision.policy_hash,
    )


# ------------------------------------------------------- snapshot binding

class SnapshotRejected(ValueError):
    """A snapshot did not match the review it claims to describe."""

    def __init__(self, reason, checks=None):
        super().__init__(reason)
        self.reason = reason
        self.checks = checks or {}


def validate_and_bind_snapshot(store, *, organization_id, repository_id, environment,
                               review_id, snapshot, request_id=None, attempt=None,
                               now=None):
    """Validate every binding a snapshot claims, then accept or reject it.

    Validation happens before any binding row is written, so a mismatch leaves
    no partial state behind. A rejection is still recorded - as a REJECTED
    binding with its reason - because silently dropping evidence would make
    the failure invisible.
    """
    now = now or datetime.now(timezone.utc)
    review = store.get_review(organization_id, repository_id, review_id)
    if review is None:
        raise SnapshotRejected("unknown review for this tenant")

    checks = {
        "organization": snapshot.get("organization_id") == organization_id,
        "repository": snapshot.get("repository_id") == repository_id,
        "environment": snapshot.get("environment") == review["environment"],
        "base_sha": (snapshot.get("base_sha") or review["base_sha"]) == review["base_sha"],
        "head_sha": (snapshot.get("head_sha") or review["head_sha"]) == review["head_sha"],
        "base_manifest_hash": (
            snapshot.get("base_manifest_hash") or review["base_manifest_hash"]
        ) == review["base_manifest_hash"],
        "head_manifest_hash": (
            snapshot.get("head_manifest_hash") or review["head_manifest_hash"]
        ) == review["head_manifest_hash"],
    }
    if attempt is not None:
        checks["attempt"] = int(attempt) == int(review.get("attempt") or 1)

    if request_id:
        request = store.get_collection_request(organization_id, repository_id, request_id)
        checks["request_exists"] = request is not None
        if request is not None:
            checks["request_review"] = request["review_id"] == review_id
            expires = request["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            checks["request_not_expired"] = expires > now

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        reason = f"snapshot binding mismatch: {', '.join(sorted(failed))}"
        store.bind_snapshot_to_review(
            organization_id, repository_id, review_id=review_id,
            snapshot_id=snapshot["snapshot_id"], binding_state="REJECTED",
            request_id=request_id, rejection_reason=reason,
            base_sha_match=checks["base_sha"], head_sha_match=checks["head_sha"],
            manifest_hash_match=checks["base_manifest_hash"] and checks["head_manifest_hash"],
            freshness_state=snapshot.get("freshness_state"),
            completeness=snapshot.get("completeness"),
        )
        raise SnapshotRejected(reason, checks)

    binding = store.bind_snapshot_to_review(
        organization_id, repository_id, review_id=review_id,
        snapshot_id=snapshot["snapshot_id"], binding_state="ACCEPTED",
        request_id=request_id,
        base_sha_match=True, head_sha_match=True, manifest_hash_match=True,
        freshness_state=snapshot.get("freshness_state"),
        completeness=snapshot.get("completeness"),
    )
    if request_id:
        state = "PARTIAL" if snapshot.get("completeness") == "PARTIAL" else "COMPLETED"
        store.close_collection_request(organization_id, repository_id, request_id,
                                       state=state)
    # Dedup on the snapshot: this snapshot's evidence is recomputed exactly
    # once, and the NEXT snapshot for the same review still enqueues its own
    # job. Keying on the review alone froze the decision at the first one.
    store.enqueue_review_recomputation(
        organization_id, repository_id, environment, review_id=review_id,
        event_type="metadata.review_recompute_requested",
        payload={"review_id": review_id, "snapshot_id": snapshot["snapshot_id"]},
        dedup_key=snapshot["snapshot_id"],
    )
    store.append_audit(
        organization_id, repository_id, actor="collector",
        event_type="snapshot.accepted", reference_type="review", reference_id=review_id,
        payload={"snapshot_id": snapshot["snapshot_id"], "request_id": request_id},
    )
    return binding


def new_snapshot_id() -> str:
    return f"snap-{uuid.uuid4().hex[:24]}"
