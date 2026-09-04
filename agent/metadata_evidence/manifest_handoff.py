"""Durable bridge from a CI-uploaded manifest to a waiting PR review."""
from __future__ import annotations

import hashlib
import json

from agent.deployment_review_service import (
    lifecycle_code_findings,
    review_manifest_change,
    semantic_evidence_from_incident,
)
from agent.evidence_policy import default_policy
from agent.metadata_evidence.publication_reconcile import (
    EVENT_TYPE as PUBLICATION_EVENT_TYPE,
)
from agent.metadata_evidence.review_lifecycle import (
    LifecycleOutcome,
    begin_review,
    review_id_for,
)
from agent.metadata_evidence.collection_plan import manifest_hash

EVENT_TYPE = "review.manifest_resume_requested"


class ManifestResumeError(RuntimeError):
    """A waiting review cannot yet be resumed and should be retried."""


def begin_manifest_wait(store, *, organization_id, repository_id, environment,
                        pull_number, base_sha, head_sha, base_manifest,
                        head_manifest=None, changed_files=(), enforcement_mode,
                        delivery_id=None):
    """Persist a webhook until both exact manifest revisions are available."""
    review_id = review_id_for(repository_id, pull_number, head_sha)
    policy = default_policy()
    store.ensure_tenant(organization_id, repository_id, environment)
    if base_manifest is not None:
        canonical = {"commit_sha": base_sha, "manifest": base_manifest}
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        store.submit_manifest_evidence(
            organization_id, repository_id,
            commit_sha=base_sha, manifest=base_manifest,
            manifest_hash=manifest_hash(base_manifest),
            idempotency_key=f"github-base:{review_id}:{base_sha}",
            payload_hash=payload_hash,
        )
    if head_manifest is not None:
        canonical = {"commit_sha": head_sha, "manifest": head_manifest}
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        store.submit_manifest_evidence(
            organization_id, repository_id,
            commit_sha=head_sha, manifest=head_manifest,
            manifest_hash=manifest_hash(head_manifest),
            idempotency_key=f"github-head:{review_id}:{head_sha}",
            payload_hash=payload_hash,
        )
    review = store.upsert_pr_review(
        organization_id, repository_id, environment,
        review_id=review_id, pull_number=pull_number,
        base_sha=base_sha, head_sha=head_sha,
        enforcement_mode=enforcement_mode,
        policy_version=policy.version, policy_hash=policy.content_hash,
        github_delivery_id=delivery_id,
        lifecycle_state="WAITING_FOR_MANIFEST",
        payload={"manifest_wait": {
            "changed_files": list(changed_files or []),
        }},
    )
    if review["lifecycle_state"] != "WAITING_FOR_MANIFEST":
        store.transition_review(
            organization_id, repository_id, review_id,
            "WAITING_FOR_MANIFEST", reason="exact head manifest not available")
    store.append_audit(
        organization_id, repository_id, actor="github-app",
        event_type="review.waiting_for_manifest", reference_type="review",
        reference_id=review_id,
        payload={"head_sha": head_sha, "delivery_id": delivery_id},
    )
    return LifecycleOutcome(
        review_id=review_id, attempt=int(review.get("attempt") or 1),
        lifecycle_state="WAITING_FOR_MANIFEST", decision=None,
        coverage="INCOMPLETE", health=100, metadata_required=False,
        request_id=None, plan={"changed_models": [], "targets": []},
        findings=[], evidence={
            "base_manifest": "AVAILABLE" if base_manifest is not None else "PENDING",
            "head_manifest": "AVAILABLE" if head_manifest is not None else "PENDING",
        }, waiting=True,
        policy_version=policy.version, policy_hash=policy.content_hash,
    )


def resume_manifest_review(store, *, organization_id, repository_id,
                           environment, review_id, commit_sha):
    """Run code analysis once the exact manifest has arrived, then republish."""
    review = store.get_review(organization_id, repository_id, review_id)
    if review is None:
        return {"review_id": review_id, "status": "unknown_review", "applied": False}
    if review.get("lifecycle_state") != "WAITING_FOR_MANIFEST":
        return {"review_id": review_id, "status": "already_resumed", "applied": False}
    if review.get("head_sha") != commit_sha:
        raise ManifestResumeError("resume job SHA does not match its review")
    head_evidence = store.get_manifest_evidence(
        organization_id, repository_id, review.get("head_sha"))
    base_evidence = store.get_manifest_evidence(
        organization_id, repository_id, review.get("base_sha"))
    if head_evidence is None or base_evidence is None:
        missing = []
        if base_evidence is None:
            missing.append("base")
        if head_evidence is None:
            missing.append("head")
        raise ManifestResumeError(
            f"exact {' and '.join(missing)} manifest evidence is not available")

    context = (review.get("payload") or {}).get("manifest_wait") or {}
    changed_files = list(context.get("changed_files") or [])
    base_manifest = base_evidence["manifest"]
    result = review_manifest_change(
        manifest=head_evidence["manifest"], previous_manifest=base_manifest,
        changed_files=changed_files,
        deployment_id=f"github:{repository_id}:{commit_sha}",
        manifest_source={
            "base": "ci_or_committed",
            "head": "ci",
        },
        base_sha=review.get("base_sha"), head_sha=commit_sha,
    )
    incident = result.get("incident") or {}
    health = incident.get("health")
    outcome = begin_review(
        store,
        organization_id=organization_id, repository_id=repository_id,
        environment=environment, pull_number=review.get("pull_number"),
        base_sha=review.get("base_sha"), head_sha=commit_sha,
        base_manifest=base_manifest, head_manifest=head_evidence["manifest"],
        changed_models=list(result.get("changed_models") or []),
        enforcement_mode=review.get("enforcement_mode") or "shadow",
        delivery_id=review.get("github_delivery_id"),
        code_health=int(health) if isinstance(health, int) else 100,
        code_findings=lifecycle_code_findings(result),
        semantic_evidence=semantic_evidence_from_incident(incident),
    )
    store.enqueue_review_recomputation(
        organization_id, repository_id, environment, review_id=review_id,
        event_type=PUBLICATION_EVENT_TYPE,
        payload={"review_id": review_id, "attempt": outcome.attempt,
                 "decision": outcome.decision, "publish_waiting": True},
        dedup_key=f"manifest-{commit_sha}-attempt-{outcome.attempt}",
    )
    store.append_audit(
        organization_id, repository_id, actor="worker:manifest",
        event_type="review.manifest_resumed", reference_type="review",
        reference_id=review_id,
        payload={"commit_sha": commit_sha, "attempt": outcome.attempt,
                 "lifecycle_state": outcome.lifecycle_state},
    )
    return {"review_id": review_id, "status": "resumed", "applied": True,
            "attempt": outcome.attempt,
            "lifecycle_state": outcome.lifecycle_state,
            "decision": outcome.decision}


def register(registry):
    @registry.register(EVENT_TYPE)
    def _handle(context):
        payload = context.payload or {}
        review_id = payload.get("review_id") or context.subject_id
        commit_sha = payload.get("commit_sha")
        if not review_id or not commit_sha:
            raise ManifestResumeError("manifest resume job is incomplete")
        return resume_manifest_review(
            context.store,
            organization_id=context.organization_id,
            repository_id=context.repository_id,
            environment=context.environment,
            review_id=review_id, commit_sha=commit_sha,
        )

    return _handle
