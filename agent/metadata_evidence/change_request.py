"""Submit a reviewer's request-changes to GitHub, durably.

The dashboard records an intent; this handler performs it. Running on the
outbox rather than inline in the request matters for one reason: a GitHub
outage must not turn into a lost decision or a fabricated success. The row
stays PENDING, the job retries, and the UI keeps saying "pending" until
GitHub actually accepts the review.

Uses ``pull_requests: write``, which the App's enforced permission set
already includes. Nothing here broadens what Relium may do.
"""
from __future__ import annotations

EVENT_TYPE = "review.change_request_submitted"


class ChangeRequestError(RuntimeError):
    """Submission failed and should be retried."""


def _body_for(review, attempt, message):
    """The review body GitHub will show.

    Leads with Relium's decision and the attempt it came from, so the request
    is anchored to specific evidence rather than to an opinion.
    """
    decision = (attempt or {}).get("decision") or review.get("decision") or "not decided"
    lines = [
        "## Relium — changes requested",
        "",
        f"**Relium decision: `{decision}`** (attempt {(attempt or {}).get('attempt')})",
        "",
        message.strip(),
    ]
    findings = [f for f in ((attempt or {}).get("payload") or {}).get("findings", [])
                if isinstance(f, dict) and f.get("severity") in ("warn", "block")]
    if findings:
        lines += ["", "### Findings behind this request", ""]
        for finding in findings[:5]:
            target = ".".join(p for p in (finding.get("relation"),
                                          finding.get("column")) if p)
            detail = finding.get("detail") or {}
            measured = ", ".join(f"`{k}={v}`" for k, v in sorted(detail.items()))
            lines.append(
                f"- **{finding.get('code')}**"
                + (f" — `{target}`" if target else "")
                + f": {finding.get('message')}"
                + (f" ({measured})" if measured else ""))
    return "\n".join(lines)


def submit_change_request(store, *, organization_id, repository_id, environment,
                          change_request_id, publisher):
    """Submit one recorded change request to GitHub.

    Idempotent: a row already PUBLISHED is returned untouched, so a retry
    after a crash that happened *after* GitHub accepted cannot submit twice.
    """
    record = store.get_change_request(organization_id, repository_id, change_request_id)
    if record is None:
        return {"change_request_id": change_request_id, "status": "unknown", "published": False}
    if record["state"] == "PUBLISHED":
        return {"change_request_id": change_request_id, "status": "already_published",
                "remote_review_id": record["remote_review_id"], "published": False}

    if publisher is None:
        # No credentials configured. Leave it PENDING and say so rather than
        # marking it failed - nothing was attempted, so nothing failed.
        return {"change_request_id": change_request_id, "status": "no_publisher",
                "published": False}

    review = store.get_review(organization_id, repository_id, record["review_id"])
    attempts = store.review_attempts(organization_id, repository_id, record["review_id"])
    attempt = next((a for a in attempts if a["attempt"] == record["attempt"]), None)

    try:
        result = publisher.submit_request_changes(
            pull_number=record["pull_number"],
            body=_body_for(review or {}, attempt, record["message"]))
    except Exception as exc:
        # A failure is recorded and stays visible. It is NOT reported as
        # success, and the row is not silently dropped.
        store.complete_change_request(
            organization_id, repository_id, change_request_id,
            failure_reason=f"{type(exc).__name__}: {str(exc)[:300]}")
        store.append_audit(
            organization_id, repository_id, actor="worker:change-request",
            event_type="review.change_request_failed", reference_type="review",
            reference_id=record["review_id"],
            payload={"change_request_id": change_request_id,
                     "error": type(exc).__name__})
        raise ChangeRequestError(str(exc)) from None

    remote_id = (result or {}).get("id")
    store.complete_change_request(
        organization_id, repository_id, change_request_id, remote_review_id=remote_id)
    store.append_audit(
        organization_id, repository_id, actor="worker:change-request",
        event_type="review.change_request_published", reference_type="review",
        reference_id=record["review_id"],
        payload={"change_request_id": change_request_id,
                 "remote_review_id": remote_id,
                 "pull_number": record["pull_number"],
                 "actor": record["actor"]})
    return {"change_request_id": change_request_id, "status": "published",
            "remote_review_id": remote_id, "published": True}


def register(registry, publisher_factory=None):
    """Register the submission handler on the shared worker registry."""

    @registry.register(EVENT_TYPE)
    def _handle(context):
        payload = context.payload or {}
        change_request_id = payload.get("change_request_id")
        if not change_request_id:
            raise ChangeRequestError("job carries no change_request_id")
        publisher = None
        if publisher_factory is not None:
            publisher = publisher_factory(
                organization_id=context.organization_id,
                repository_id=context.repository_id,
                environment=context.environment)
        return submit_change_request(
            context.store,
            organization_id=context.organization_id,
            repository_id=context.repository_id,
            environment=context.environment,
            change_request_id=change_request_id, publisher=publisher)

    return _handle
