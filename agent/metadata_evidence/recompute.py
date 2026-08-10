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

from agent.evidence_policy import default_policy
from agent.metadata_evidence.decision import evaluate_metadata_decision
from agent.metadata_evidence.production_comparison import compute_comparison
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

    decision = evaluate_metadata_decision(
        plan=plan, snapshot=snapshot, enforcement_mode=enforcement_mode,
        code_health=int(review.get("health") or 100),
        policy=default_policy(), now=now,
    )

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

    # SQL semantic evidence describes the review's CODE state, and a
    # recomputation changes production evidence rather than code provenance.
    # Under the current model a review is bound to one pull_number/head_sha
    # (uq_reviews_pr_head) and attempts carry no SHA of their own, so every
    # attempt of this review describes the same base/head pair and the
    # evidence computed for it stays true here.
    #
    # It is carried from THIS review's previous attempt only - never selected
    # globally, never taken from another review - and never recomputed, since
    # the durable document already exists. A previous attempt that compared
    # nothing carries NULL forward, because "no comparison ran" must not
    # become "a comparison found no changes". Copying rather than referencing
    # keeps the new attempt self-contained, which is what lets the dashboard
    # keep reading exactly one attempt.
    #
    # This is safe only while attempts cannot describe different head SHAs. If
    # the schema ever admits that, this must become a provenance check rather
    # than an unconditional carry-forward.
    semantic_evidence = previous.get("semantic_evidence") if previous else None

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
    store.record_review_decision(
        organization_id, repository_id, review_id,
        decision=decision.decision, evidence_coverage=decision.coverage,
        health=decision.health, attempt=next_attempt,
        trigger="metadata_snapshot", snapshot_id=snapshot["snapshot_id"],
        enforcement_mode=enforcement_mode,
        policy_version=decision.policy_version, policy_hash=decision.policy_hash,
        payload={"findings": [f.as_dict() for f in decision.findings],
                 "snapshot_id": snapshot["snapshot_id"]},
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
        "metadata_comparison": metadata_comparison,
        "applied": True,
    }


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
