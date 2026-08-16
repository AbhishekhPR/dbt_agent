"""Republish a review's GitHub and Slack surfaces after a recomputation.

Publication used to happen in exactly one place - the synchronous webhook
path in ``ReviewRunner._publish`` - so the comment and check run were written
once, at the WAITING_FOR_METADATA publication, and then frozen. A review that
later became WARN or BLOCK from arriving production evidence still showed a
neutral "waiting" check, and Slack was never told at all.

This closes that gap on the existing durable outbox, using the identities the
store already kept for exactly this purpose:

  * ``reviews.github_comment_id``   - the sticky comment, updated in place
  * ``reviews.github_check_run_id`` - the check run, PATCHed to its new
                                      conclusion rather than re-created

Nothing here decides anything. It reads the decision the worker already
committed and publishes it. The Slack rules are the adapter's own
``classify`` - BLOCK always, WARN only when the sink is configured for it,
ALLOW and WAITING never - and are neither consulted nor relaxed here.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.github_app.checks import CHECK_NAME, build_check_run_payload
from agent.github_app.review_comment import render_review_comment

EVENT_TYPE = "review.publication_reconcile_requested"

# Severity for the incident block the comment renderer reads. Derived from the
# decision, never from a narrative.
_SEVERITY = {"BLOCK": "HIGH", "WARN": "MEDIUM", "ALLOW": "LOW"}


class ReconciliationError(RuntimeError):
    """Publication could not be reconciled and should be retried."""


def build_review_result(review, attempt):
    """The publication payload for a decided review.

    Shaped for the existing renderers so the comment and check a recomputation
    writes are produced by the same code as the original publication.
    """
    findings = list((attempt.get("payload") or {}).get("findings") or [])
    decision = attempt.get("decision") or review.get("decision")

    if decision is None:
        from agent.metadata_evidence.waiting_publication import render_waiting_result

        plan = (attempt.get("payload") or {}).get("plan") or {}
        outcome = SimpleNamespace(
            review_id=review["review_id"], attempt=attempt.get("attempt"),
            lifecycle_state=attempt.get("lifecycle_state"),
            coverage=attempt.get("evidence_coverage") or "INCOMPLETE",
            health=attempt.get("health") or review.get("health") or 100,
            request_id=None, plan=plan, evidence={},
        )
        result = render_waiting_result(
            outcome, base_sha=review.get("base_sha"),
            head_sha=review.get("head_sha"))
        result["enforcement_mode"] = (
            attempt.get("enforcement_mode") or review.get("enforcement_mode"))
        return result

    # The renderer's finding contract is title/impact/recommended_fix. A
    # metadata finding already carries all three in its own vocabulary, so it
    # is mapped rather than re-described.
    material = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("severity") not in ("warn", "block"):
            continue
        detail = finding.get("detail") or {}
        measured = ", ".join(f"{k}={v}" for k, v in sorted(detail.items()))
        target = ".".join(p for p in (finding.get("relation"),
                                      finding.get("column")) if p)
        material.append({
            "rule": finding.get("code"),
            "title": f"{finding.get('code')}{f' — {target}' if target else ''}",
            "impact": finding.get("message"),
            "recommended_fix": (
                f"Measured: {measured}." if measured
                else "Review the production evidence for this relation."),
        })

    return {
        "decision": decision,
        "final": True,
        "coverage": attempt.get("evidence_coverage"),
        "health": attempt.get("health"),
        "lifecycle_state": attempt.get("lifecycle_state"),
        "review_id": review["review_id"],
        "attempt": attempt.get("attempt"),
        "enforcement_mode": attempt.get("enforcement_mode")
        or review.get("enforcement_mode"),
        "material_findings": material,
        "changed_models": list(
            ((review.get("payload") or {}).get("plan") or {}).get("changed_models") or []),
        "incident": {
            "decision": decision,
            "health": attempt.get("health"),
            "severity": _SEVERITY.get(str(decision).upper(), "LOW"),
            "affected_models": sorted({
                f["relation"] for f in findings
                if isinstance(f, dict) and f.get("relation")
            }),
        },
    }


def reconcile_publication(store, *, organization_id, repository_id, environment,
                          review_id, publisher, attempt=None,
                          publish_waiting=False):
    """Update the sticky comment, the check run and Slack for one review.

    ``publisher`` supplies the outbound surfaces. When it is None the review
    is recorded as unpublished rather than silently treated as delivered: a
    decision nobody was told about must not look like one that was.
    """
    review = store.get_review(organization_id, repository_id, review_id)
    if review is None:
        return {"review_id": review_id, "status": "unknown_review", "published": False}

    attempts = store.review_attempts(organization_id, repository_id, review_id)
    if not attempts:
        raise ReconciliationError("review has no recorded attempt to publish")
    if attempt is None:
        record = max(attempts, key=lambda a: a["attempt"])
    else:
        matching = [a for a in attempts if a["attempt"] == int(attempt)]
        if not matching:
            raise ReconciliationError(f"attempt {attempt} is not recorded")
        record = matching[0]

    if record.get("decision") is None and not publish_waiting:
        # Ordinary WAITING_FOR_METADATA reviews were already published by the
        # webhook path. Only a manifest-resume job needs to replace its older
        # WAITING_FOR_MANIFEST surface with the newly computed waiting state.
        return {"review_id": review_id, "status": "no_decision_yet",
                "attempt": record["attempt"], "published": False}

    result = build_review_result(review, record)

    if publisher is None:
        store.append_audit(
            organization_id, repository_id, actor="worker:publication",
            event_type="review.publication_skipped", reference_type="review",
            reference_id=review_id,
            payload={"attempt": record["attempt"], "reason": "no publisher configured"})
        return {"review_id": review_id, "status": "no_publisher",
                "attempt": record["attempt"], "decision": record["decision"],
                "published": False}

    enforcement_mode = result["enforcement_mode"] or "shadow"
    comment_body = render_review_comment(result)
    outcome = {
        "review_id": review_id, "attempt": record["attempt"],
        "decision": record["decision"], "coverage": record.get("evidence_coverage"),
        "status": "reconciled", "published": True,
    }

    # -- GitHub comment: update the SAME comment, never create a second ----
    comment_id = review.get("github_comment_id")
    comment = publisher.publish_comment(
        pull_number=review.get("pull_number"), body=comment_body,
        comment_id=comment_id)
    outcome["comment_id"] = str((comment or {}).get("id") or comment_id or "")
    outcome["comment_reused"] = bool(
        comment_id and str(outcome["comment_id"]) == str(comment_id))

    # -- GitHub check run: PATCH the SAME run to its new conclusion --------
    check_result = dict(result)
    check_result["rendered"] = {"markdown": comment_body}
    check_payload = build_check_run_payload(
        head_sha=review.get("head_sha"), result=check_result,
        enforcement_mode=enforcement_mode,
        external_id=f"review-{review_id}")
    check_run_id = review.get("github_check_run_id")
    check = publisher.publish_check(
        head_sha=review.get("head_sha"), payload=check_payload,
        check_run_id=check_run_id)
    outcome["check_run_id"] = str((check or {}).get("id") or check_run_id or "")
    outcome["check_reused"] = bool(
        check_run_id and str(outcome["check_run_id"]) == str(check_run_id))
    outcome["check_conclusion"] = check_payload["conclusion"]
    outcome["check_name"] = CHECK_NAME

    store.record_review_publication(
        organization_id, repository_id, review_id,
        comment_id=outcome["comment_id"] or None,
        check_run_id=outcome["check_run_id"] or None)

    # -- Slack: the adapter's own rules, unchanged -------------------------
    outcome["slack"] = publisher.publish_slack(
        publication_id=f"review-{review_id}-attempt-{record['attempt']}",
        pull_number=review.get("pull_number"), result=result)

    # Journal each channel. A channel is marked delivered ONLY when it really
    # delivered: a Slack alert the policy suppressed carries a publication id
    # but was never sent, and recording it as PUBLISHED would make the
    # dashboard claim an alert nobody received.
    slack_state = str((outcome["slack"] or {}).get("state") or "").lower()
    slack_delivered = slack_state == "complete"
    deliveries = (
        ("github", outcome["comment_id"], True),
        ("slack", (outcome["slack"] or {}).get("publication_id"), slack_delivered),
    )
    for channel, remote_id, delivered in deliveries:
        payload = {"decision": record["decision"],
                   "coverage": record.get("evidence_coverage"),
                   "attempt": record["attempt"]}
        if channel == "slack":
            payload["state"] = slack_state or "unknown"
            reason = (outcome["slack"] or {}).get("reason")
            if reason:
                payload["reason"] = reason
        journal = store.record_delivery(
            organization_id, repository_id, environment, channel=channel,
            event_key=f"{review_id}:attempt-{record['attempt']}",
            payload=payload)
        if delivered and remote_id:
            store.mark_delivered(organization_id, repository_id,
                                 journal["journal_id"], remote_id=str(remote_id))

    store.append_audit(
        organization_id, repository_id, actor="worker:publication",
        event_type="review.publication_reconciled", reference_type="review",
        reference_id=review_id,
        payload={"attempt": record["attempt"], "decision": record["decision"],
                 "comment_reused": outcome["comment_reused"],
                 "check_reused": outcome["check_reused"],
                 "check_conclusion": outcome["check_conclusion"]})
    return outcome


def register(registry, publisher_factory=None):
    """Register the reconciliation handler on the shared worker registry.

    ``publisher_factory`` is called per job with the tenant scope, so a worker
    serving several repositories resolves the right installation credential
    rather than holding one client for all of them.
    """

    @registry.register(EVENT_TYPE)
    def _handle(context):
        payload = context.payload or {}
        review_id = payload.get("review_id") or context.subject_id
        if not review_id:
            raise ReconciliationError("publication job carries no review id")
        publisher = None
        if publisher_factory is not None:
            publisher = publisher_factory(
                organization_id=context.organization_id,
                repository_id=context.repository_id,
                environment=context.environment)
        return reconcile_publication(
            context.store,
            organization_id=context.organization_id,
            repository_id=context.repository_id,
            environment=context.environment,
            review_id=review_id, publisher=publisher,
            attempt=payload.get("attempt"),
            publish_waiting=bool(payload.get("publish_waiting")))

    return _handle
