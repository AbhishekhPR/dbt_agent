"""Durable review recomputation, executed by the real lifecycle worker.

Runs on the existing outbox and worker runtime - the same claim, lease,
bounded-attempt and dead-letter machinery the deployment path uses. There is
no second queue.

Idempotence is structural rather than best-effort: the decision for an attempt
is written with ON CONFLICT DO NOTHING against
(organization, repository, review, attempt), so a retry after a crash that
happened *after* the commit cannot produce a second decision.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.deployment_review_service import (
    lifecycle_code_findings,
    review_manifest_change,
    semantic_evidence_from_incident,
)
from agent.evidence_policy import default_policy
from agent.metadata_evidence.decision import evaluate_metadata_decision
from agent.metadata_evidence.production_comparison import compute_comparison
from agent.metadata_evidence.decision_explanation import build_attempt_payload
from agent.metadata_evidence.publication_reconcile import (
    EVENT_TYPE as PUBLICATION_EVENT_TYPE,
)
from agent.metadata_evidence.review_lifecycle import _evidence_rows

EVENT_TYPE = "metadata.review_recompute_requested"


class RecomputationError(RuntimeError):
    """Raised when a recomputation cannot proceed and should be retried."""


def recompute_review(store, *, organization_id, repository_id, environment,
                     review_id, now=None):
    """Recompute one review against its accepted production snapshot.

    Returns a dict describing the effective outcome. Safe to call repeatedly:
    a second call for an already-decided attempt reports the existing decision
    rather than producing another one.
    """
    now = now or datetime.now(timezone.utc)

    review = store.get_review(organization_id, repository_id, review_id)
    if review is None:
        # Nothing to recompute and nothing will make it appear. Treat as done
        # rather than retrying forever into the dead-letter queue.
        return {"review_id": review_id, "status": "unknown_review", "applied": False}

    attempt = int(review.get("attempt") or 1)
    plan = (review.get("payload") or {}).get("plan") or {}
    enforcement_mode = review.get("enforcement_mode") or "shadow"

    snapshot = store.latest_accepted_snapshot(organization_id, repository_id, review_id)
    if snapshot is None:
        raise RecomputationError(
            "no accepted snapshot is bound to this review yet")

    # Idempotence keys on the SNAPSHOT that triggered this recomputation, not
    # on the attempt counter. Keying on the counter was wrong: the first
    # recomputation advances it, so a retry would always look like new work
    # and would produce a second decision for the same evidence.
    attempts = store.review_attempts(organization_id, repository_id, review_id)
    for recorded in attempts:
        if recorded.get("snapshot_id") == snapshot["snapshot_id"]:
            return {
                "review_id": review_id,
                "status": "already_recomputed",
                "attempt": recorded["attempt"],
                "decision": recorded["decision"],
                "snapshot_id": snapshot["snapshot_id"],
                "applied": False,
            }

    # The recomputed attempt is the next one, so the waiting attempt stays in
    # history exactly as it was published.
    previous = max(attempts, key=lambda a: a["attempt"]) if attempts else None
    next_attempt = (previous["attempt"] if previous else attempt) + 1
    previous_payload = (previous or {}).get("payload") or {}
    code_findings = [
        finding for finding in (previous_payload.get("findings") or [])
        if isinstance(finding, dict) and finding.get("category") == "code"
    ]
    semantic_evidence = previous.get("semantic_evidence") if previous else None
    recovered = None
    if semantic_evidence is None:
        recovered = _recover_exact_manifest_analysis(
            store,
            organization_id=organization_id,
            repository_id=repository_id,
            review=review,
            changed_models=plan.get("changed_models") or [],
        )
        if recovered is not None:
            semantic_evidence = recovered["semantic_evidence"]
            code_findings = _merge_code_findings(
                code_findings, recovered["code_findings"]
            )

    code_health = review.get("health")
    if code_health is None:
        code_health = 100
    if recovered is not None and recovered.get("code_health") is not None:
        code_health = recovered["code_health"]
    decision = evaluate_metadata_decision(
        plan=plan, snapshot=snapshot, enforcement_mode=enforcement_mode,
        code_health=int(code_health), code_findings=code_findings,
        policy=default_policy(), now=now,
    )

    # SQL semantic evidence describes the review's CODE state, and a
    # recomputation changes production evidence rather than code provenance.
    # Under the current model a review is bound to one pull_number/head_sha
    # (uq_reviews_pr_head) and attempts carry no SHA of their own, so every
    # attempt of this review describes the same base/head pair and the
    # evidence computed for it stays true here.
    #
    # It is carried from THIS review's previous attempt only. For reviews made
    # before semantic evidence was persisted, the exact SHA-bound manifests
    # may still exist in the immutable manifest store. In that one case the
    # comparison is recovered from those same documents for the NEW attempt;
    # historical attempts remain untouched. If either manifest, hash, changed
    # model or readable SQL side is absent, recovery returns NULL rather than
    # inventing a clean comparison.
    #
    # This is safe only while attempts cannot describe different head SHAs. If
    # the schema ever admits that, this must become a provenance check rather
    # than an unconditional carry-forward.
    # Computed exactly once, here, where the current snapshot is already known
    # to be durably stored and accepted - and then written down. The read path
    # never re-selects "the latest previous snapshot", so an attempt keeps
    # naming the same baseline after newer observations arrive.
    #
    # This is evidence only. It is deliberately computed AFTER the decision and
    # is not an input to it: no change found here can move a verdict.
    metadata_comparison = compute_comparison(
        store, organization_id=organization_id, repository_id=repository_id,
        environment=environment, current_snapshot=snapshot,
    )

    store.transition_review(organization_id, repository_id, review_id,
                            decision.lifecycle_state,
                            reason="production metadata evaluated")
    attempt_payload = build_attempt_payload(
        decision=decision.decision,
        health=decision.health,
        findings=[f.as_dict() for f in decision.findings],
        policy_reasons=decision.reasons,
        health_explanation=(
            (recovered or {}).get("health_explanation")
            or previous_payload.get("health_explanation")
        ),
        snapshot_id=snapshot["snapshot_id"],
    )
    store.record_review_decision(
        organization_id, repository_id, review_id,
        decision=decision.decision, evidence_coverage=decision.coverage,
        health=decision.health, attempt=next_attempt,
        trigger="metadata_snapshot", snapshot_id=snapshot["snapshot_id"],
        enforcement_mode=enforcement_mode,
        policy_version=decision.policy_version, policy_hash=decision.policy_hash,
        payload=attempt_payload,
        semantic_evidence=semantic_evidence,
        metadata_comparison=metadata_comparison,
    )
    store.record_evidence_states(organization_id, repository_id, review_id,
                                 next_attempt, _evidence_rows(decision))

    if decision.decision is not None:
        store.transition_review(organization_id, repository_id, review_id,
                                "DECISION_READY",
                                reason="metadata-backed decision computed")

    store.append_audit(
        organization_id, repository_id, actor="worker",
        event_type="review.recomputed", reference_type="review", reference_id=review_id,
        payload={"attempt": next_attempt, "decision": decision.decision,
                 "coverage": decision.coverage,
                 "snapshot_id": snapshot["snapshot_id"]},
    )

    # A decision nobody is told about is not a decision anyone can act on.
    # Republication runs as its own durable job so a GitHub or Slack outage
    # retries on the outbox instead of failing the recomputation that already
    # committed. Dedup is per attempt: each recomputation republishes once.
    if decision.decision is not None:
        store.enqueue_review_recomputation(
            organization_id, repository_id, environment, review_id=review_id,
            event_type=PUBLICATION_EVENT_TYPE,
            payload={"review_id": review_id, "attempt": next_attempt,
                     "decision": decision.decision},
            dedup_key=f"attempt-{next_attempt}",
        )
    return {
        "review_id": review_id,
        "status": "recomputed",
        "attempt": next_attempt,
        "decision": decision.decision,
        "coverage": decision.coverage,
        "health": decision.health,
        "lifecycle_state": decision.lifecycle_state,
        "snapshot_id": snapshot["snapshot_id"],
        "findings": [f.as_dict() for f in decision.findings],
        "primary_reason": attempt_payload["primary_reason"],
        "health_explanation": attempt_payload["health_explanation"],
        "metadata_comparison": metadata_comparison,
        "applied": True,
    }


def _recover_exact_manifest_analysis(store, *, organization_id, repository_id,
                                     review, changed_models):
    """Re-run code analysis only from this review's immutable manifest pair.

    This is a compatibility bridge for attempts written before semantic/code
    evidence reached ``review_attempts``. It never reads warehouse evidence,
    never changes an existing attempt, and refuses provenance that does not
    match the hashes recorded on the review.
    """
    if not changed_models:
        return None
    base_sha = review.get("base_sha")
    head_sha = review.get("head_sha")
    if not base_sha or not head_sha:
        return None
    base = store.get_manifest_evidence(organization_id, repository_id, base_sha)
    head = store.get_manifest_evidence(organization_id, repository_id, head_sha)
    if base is None or head is None:
        return None
    if (base.get("manifest_hash") != review.get("base_manifest_hash")
            or head.get("manifest_hash") != review.get("head_manifest_hash")):
        return None

    try:
        result = review_manifest_change(
            manifest=head.get("manifest"),
            previous_manifest=base.get("manifest"),
            changed_files=[],
            changed_models=list(changed_models),
            deployment_id=f"recompute:{review.get('review_id')}:{head_sha}",
            manifest_source={"base": "ci", "head": "ci"},
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except ValueError:
        # Compatibility recovery is optional. A stale plan/model identity must
        # remain unavailable rather than poison an otherwise valid metadata
        # recomputation job.
        return None
    incident = result.get("incident") or {}
    semantic_evidence = semantic_evidence_from_incident(incident)
    if semantic_evidence is None:
        return None
    health = incident.get("health")
    return {
        "semantic_evidence": semantic_evidence,
        "code_findings": lifecycle_code_findings(result),
        "code_health": int(health) if isinstance(health, int) else None,
        "health_explanation": result.get("health_explanation"),
    }


def _merge_code_findings(existing, recovered):
    """Keep prior code evidence and add newly recoverable findings once."""
    merged = list(existing or [])
    identities = {
        (item.get("code"), item.get("relation"), item.get("column"))
        for item in merged if isinstance(item, dict)
    }
    for item in recovered or []:
        if not isinstance(item, dict):
            continue
        identity = (item.get("code"), item.get("relation"), item.get("column"))
        if identity not in identities:
            merged.append(item)
            identities.add(identity)
    return merged


def register(registry):
    """Register the recomputation handler on the shared worker registry."""

    @registry.register(EVENT_TYPE)
    def _handle(context):
        payload = context.payload or {}
        review_id = payload.get("review_id") or context.subject_id
        if not review_id:
            raise RecomputationError("recomputation job carries no review id")
        return recompute_review(
            context.store,
            organization_id=context.organization_id,
            repository_id=context.repository_id,
            environment=context.environment,
            review_id=review_id,
        )

    return _handle
