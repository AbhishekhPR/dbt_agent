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
from agent.postgres_lifecycle_store import ManifestEvidenceConflict

EVENT_TYPE = "review.manifest_resume_requested"

#: Action required: this commit's manifest evidence disagrees with what is
#: already recorded for it. Terminal for this delivery -- analysis does not
#: run -- and left behind only by a later delivery that finds no conflict.
CONFLICT_STATE = "MANIFEST_CONFLICT"


class ManifestResumeError(RuntimeError):
    """A waiting review cannot yet be resumed and should be retried."""


def manifest_evidence_key(repository_id, commit_sha) -> str:
    """The idempotency key for one commit's manifest evidence.

    Deliberately a function of repository and commit alone, matching the grain
    of the evidence itself: one commit has one manifest. The earlier keys
    embedded ``review_id``, which embeds the pull number and head SHA, so a
    second pull request opening from an already-analysed base produced a NEW
    key for a commit that already had evidence -- every time.
    """
    return f"github-manifest:{repository_id}:{commit_sha}"


def begin_manifest_wait(store, *, organization_id, repository_id, environment,
                        pull_number, base_sha, head_sha, base_manifest,
                        head_manifest=None, changed_files=(), enforcement_mode,
                        delivery_id=None):
    """Persist a webhook until both exact manifest revisions are available."""
    review_id = review_id_for(repository_id, pull_number, head_sha)
    policy = default_policy()
    store.ensure_tenant(organization_id, repository_id, environment)
    conflicts = []
    for side, sha, document in (("base", base_sha, base_manifest),
                                ("head", head_sha, head_manifest)):
        if document is None:
            continue
        canonical = {"commit_sha": sha, "manifest": document}
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        try:
            store.submit_manifest_evidence(
                organization_id, repository_id,
                commit_sha=sha, manifest=document,
                manifest_hash=manifest_hash(document),
                idempotency_key=manifest_evidence_key(repository_id, sha),
                payload_hash=payload_hash,
            )
        except ManifestEvidenceConflict as exc:
            # This commit already has evidence that means something else, and
            # both documents cannot be right. The stored row is immutable and
            # is what earlier decisions were computed from; the manifest just
            # delivered disagrees with it. Analysing either one would be
            # analysing code the other side did not send, so the review is
            # persisted and parked in MANIFEST_CONFLICT below -- it does NOT
            # fall back to the stored evidence.
            #
            # What must also NOT happen is this escaping: the submissions run
            # before the review row is written, so an exception here left the
            # pull request with no review at all, and every redelivery
            # repeated it with nothing to look at.
            conflicts.append({"side": side, "commit_sha": sha,
                              "reason": str(exc)})
    # A conflict is not a wait. Waiting means "the evidence will arrive";
    # here it has arrived and disagrees with what is already recorded, which
    # no amount of patience resolves.
    lifecycle_state = CONFLICT_STATE if conflicts else "WAITING_FOR_MANIFEST"
    conflicted_sides = {conflict["side"] for conflict in conflicts}

    review = store.upsert_pr_review(
        organization_id, repository_id, environment,
        review_id=review_id, pull_number=pull_number,
        base_sha=base_sha, head_sha=head_sha,
        enforcement_mode=enforcement_mode,
        policy_version=policy.version, policy_hash=policy.content_hash,
        github_delivery_id=delivery_id,
        lifecycle_state=lifecycle_state,
        payload={"manifest_wait": {
            "changed_files": list(changed_files or []),
            "evidence_conflicts": conflicts,
        }},
    )
    if review["lifecycle_state"] != lifecycle_state:
        # Recorded as a transition rather than a silent overwrite, so a review
        # that was waiting and is now conflicted says so in its history -- and
        # so does the reverse, which is how a fixed conflict is retried: a
        # later delivery that finds no conflict moves the review back to
        # WAITING_FOR_MANIFEST and analysis resumes from there.
        store.transition_review(
            organization_id, repository_id, review_id, lifecycle_state,
            reason=(conflicts[0]["reason"] if conflicts
                    else "exact head manifest not available"))

    for conflict in conflicts:
        store.append_audit(
            organization_id, repository_id, actor="github-app",
            event_type="review.manifest_evidence_conflict",
            reference_type="review", reference_id=review_id,
            payload=dict(conflict, delivery_id=delivery_id,
                         lifecycle_state=lifecycle_state),
        )
    if not conflicts:
        store.append_audit(
            organization_id, repository_id, actor="github-app",
            event_type="review.waiting_for_manifest", reference_type="review",
            reference_id=review_id,
            payload={"head_sha": head_sha, "delivery_id": delivery_id},
        )

    def _side(name, document):
        if name in conflicted_sides:
            return "CONFLICT"
        return "AVAILABLE" if document is not None else "PENDING"

    return LifecycleOutcome(
        review_id=review_id, attempt=int(review.get("attempt") or 1),
        lifecycle_state=lifecycle_state, decision=None,
        coverage="INCOMPLETE", health=100, metadata_required=False,
        request_id=None, plan={"changed_models": [], "targets": []},
        findings=[], evidence={
            "base_manifest": _side("base", base_manifest),
            "head_manifest": _side("head", head_manifest),
        },
        # Not waiting: nothing is expected to arrive that would resolve this.
        waiting=not conflicts,
        policy_version=policy.version, policy_hash=policy.content_hash,
    )


def resume_manifest_review(store, *, organization_id, repository_id,
                           environment, review_id, commit_sha):
    """Run code analysis once the exact manifest has arrived, then republish."""
    review = store.get_review(organization_id, repository_id, review_id)
    if review is None:
        return {"review_id": review_id, "status": "unknown_review", "applied": False}
    if review.get("lifecycle_state") == CONFLICT_STATE:
        # Analysis must not proceed from the stored evidence. Reported as its
        # own status rather than folded into "already_resumed", which would
        # read as success.
        return {"review_id": review_id, "status": "manifest_conflict",
                "applied": False,
                "conflicts": ((review.get("payload") or {})
                              .get("manifest_wait") or {}
                              ).get("evidence_conflicts") or []}
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
        health_explanation=result.get("health_explanation"),
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
