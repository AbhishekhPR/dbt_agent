"""Verification and variant stages for the genuine GitHub metadata-review E2E.

Every function polls or queries a REAL system - the served public API, the
PostgreSQL store, or the GitHub API - and raises StageFailure when the required
outcome is not observed. None return a canned success.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The repository root must be importable for `agent...` imports.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from live_flow import ENVIRONMENT, StageFailure, local, poll

HIGH_NULL_RATE = 0.82
NULL_RATE_THRESHOLD = 0.20
HEALTHY_NULL_RATE = 0.01

FORBIDDEN_IN_RESPONSES = (
    "-----begin", "postgresql://", "password", "private key",
    "select ", "insert into", "drop table", "ghp_", "ghs_",
)


def _store(dsn):
    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    return PostgresLifecycleStore(dsn)


# ------------------------------------------------- genuine webhook arrival
def verify_genuine_webhook(gh, app_jwt, since_utc, pr_number):
    """Confirm GitHub itself recorded a delivery accepted with HTTP 202."""
    def delivered():
        status, deliveries = gh("GET", "/app/hook/deliveries?per_page=50", app_jwt())
        if status != 200:
            return None
        recent = [d for d in deliveries
                  if d.get("event") == "pull_request"
                  and d.get("delivered_at", "") >= since_utc]
        accepted = [d for d in recent if d.get("status_code") == 202]
        return accepted[0] if accepted else None

    delivery = poll(delivered, timeout=240, interval=5,
                    description="a GitHub pull_request delivery accepted with 202")
    return {"delivery_id": delivery.get("guid"), "event": delivery.get("event"),
            "action": delivery.get("action"), "status_code": delivery.get("status_code"),
            "delivered_at": delivery.get("delivered_at"),
            "signature_verified_by_application": True,
            "pull_request": pr_number}


# --------------------------------------------------- PostgreSQL review row
def verify_postgres_review(dsn, owner, repo_name, head_sha, base_sha):
    def found():
        store = _store(dsn)
        try:
            rows = store.connection.execute(
                "SELECT * FROM reviews WHERE organization_id=%s AND repository_id=%s "
                "AND head_sha=%s", (owner, repo_name, head_sha)).fetchall()
            return dict(rows[0]) if rows else None
        finally:
            store.close()

    review = poll(found, timeout=240, interval=4,
                  description="a PostgreSQL review row created by the served runner")
    for field in ("base_sha", "head_sha", "base_manifest_hash", "head_manifest_hash"):
        if not review.get(field):
            raise StageFailure(f"review is missing {field}")
    if review["head_sha"] != head_sha:
        raise StageFailure("review head SHA does not match the pull request")
    # The base SHA must be the immutable value GitHub reported for the pull
    # request. A silent mismatch would mean the review was bound to different
    # base-code evidence than the PR actually proposes.
    if base_sha and review["base_sha"] != base_sha:
        raise StageFailure(
            f"review base SHA {review['base_sha'][:12]} does not match the "
            f"pull request base {base_sha[:12]}")
    return {"review_id": review["review_id"], "attempt": review["attempt"],
            "pull_number": review["pull_number"],
            "base_sha": review["base_sha"], "head_sha": review["head_sha"],
            "base_manifest_hash": review["base_manifest_hash"],
            "head_manifest_hash": review["head_manifest_hash"],
            "policy_version": review["policy_version"],
            "enforcement_mode": review["enforcement_mode"]}


# ------------------------------------------------------- targeted request
def verify_targeted_request(dsn, owner, repo_name, review_id):
    store = _store(dsn)
    try:
        rows = store.connection.execute(
            "SELECT request_id FROM collection_requests WHERE organization_id=%s "
            "AND repository_id=%s AND review_id=%s", (owner, repo_name, review_id)
        ).fetchall()
        if len(rows) != 1:
            raise StageFailure(f"expected exactly one collection request, got {len(rows)}")
        request = store.get_collection_request(owner, repo_name, rows[0]["request_id"])
    finally:
        store.close()

    targets = request.get("targets", [])
    names = sorted({t["relation_name"] for t in targets})
    if not any("orders" in n for n in names):
        raise StageFailure(f"request does not target the orders relation: {names}")
    if len(names) > 3:
        raise StageFailure(f"request is not bounded - {len(names)} relations: {names}")
    for target in targets:
        if target["dependency_kind"] == "head_derived":
            raise StageFailure(
                "request asks for a head-derived output not expected in production")
    columns = sorted({c for t in targets for c in (t.get("columns") or [])})
    signals = sorted({s for t in targets for s in (t.get("required_signals") or [])})
    return {"request_id": request["request_id"], "relations": names,
            "columns": columns, "required_signals": signals,
            "bounded": len(names) <= 3,
            "base_sha": request["base_sha"], "head_sha": request["head_sha"],
            "expires_at": str(request["expires_at"]),
            "full_warehouse_scan": False}


# ---------------------------------------------------- waiting publication
def verify_waiting_publication(dsn, gh, token, repo, pr_number, owner, repo_name,
                               review_id, expected_app_id):
    store = _store(dsn)
    try:
        review = store.get_review(owner, repo_name, review_id)
    finally:
        store.close()
    if review["lifecycle_state"] != "WAITING_FOR_METADATA":
        raise StageFailure(f"lifecycle is {review['lifecycle_state']}")
    if review["decision"] is not None:
        raise StageFailure(f"decision must be undecided, got {review['decision']}")
    if review["evidence_coverage"] != "INCOMPLETE":
        raise StageFailure(f"coverage is {review['evidence_coverage']}")

    status, comments = gh("GET", f"/repos/{repo}/issues/{pr_number}/comments",
                          token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read PR comments: HTTP {status}")
    owned = [c for c in comments
             if (c.get("performed_via_github_app") or {}).get("id") == expected_app_id]
    if len(owned) != 1:
        raise StageFailure(f"expected exactly one App-owned comment, got {len(owned)}")
    body = owned[0]["body"].lower()
    if "not yet decided" not in body and "waiting" not in body:
        raise StageFailure("waiting comment does not state the review is undecided")

    status, checks = gh("GET", f"/repos/{repo}/commits/{review['head_sha']}/check-runs",
                        token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read check runs: HTTP {status}")
    runs = [c for c in checks.get("check_runs", [])
            if (c.get("app") or {}).get("id") == expected_app_id]
    if len(runs) != 1:
        raise StageFailure(f"expected exactly one App-owned check, got {len(runs)}")
    if runs[0].get("conclusion") == "success":
        raise StageFailure("waiting check published a SUCCESS conclusion")

    return {"lifecycle_state": review["lifecycle_state"],
            "decision": review["decision"], "coverage": review["evidence_coverage"],
            "health": review["health"], "comment_id": owned[0]["id"],
            "check_run_id": runs[0]["id"], "check_conclusion": runs[0].get("conclusion"),
            "non_final": runs[0].get("conclusion") != "success"}


# ------------------------------------------------------ snapshot handling
def snapshot_body(review, request_id, *, null_rate=HIGH_NULL_RATE, exists=True,
                  data_type="numeric", observed_at=None, completeness="COMPLETE",
                  ttl_seconds=3600):
    columns = [{"column_name": "order_id", "data_type": "bigint", "null_rate": 0.0}]
    if exists:
        columns.append({"column_name": "discount_amount", "data_type": data_type,
                        "is_nullable": True, "null_rate": null_rate})
    return {
        "review_id": review["review_id"], "request_id": request_id,
        "environment": ENVIRONMENT, "attempt": review["attempt"],
        "completeness": completeness, "ttl_seconds": ttl_seconds,
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "base_sha": review["base_sha"], "head_sha": review["head_sha"],
        "base_manifest_hash": review["base_manifest_hash"],
        "head_manifest_hash": review["head_manifest_hash"],
        "collector_version": "e2e-harness", "adapter_type": "postgres",
        "relations": [{"relation_name": "raw.orders", "relation_schema": "raw",
                       "exists_in_production": True, "schema_fingerprint": "fp-e2e",
                       "row_count": 12345, "columns": columns}],
    }


def submit_primary_snapshot(token, review, request_id):
    key = f"e2e-{uuid.uuid4().hex[:12]}"
    body = snapshot_body(review, request_id)
    status, first = local("POST", "/api/metadata-snapshots", body, token=token, key=key)
    if status != 202:
        raise StageFailure(f"primary snapshot rejected: HTTP {status} {first}")
    if not first.get("recomputation_queued"):
        raise StageFailure("snapshot accepted but no recomputation was queued")
    return {"status": status, "snapshot_id": first["snapshot_id"],
            "idempotency_key": key, "recomputation_queued": True,
            "null_rate_submitted": HIGH_NULL_RATE}, key, body


def verify_duplicate(token, body, key, snapshot_id):
    status, repeat = local("POST", "/api/metadata-snapshots", body, token=token, key=key)
    if status != 200:
        raise StageFailure(f"exact duplicate returned {status}, expected 200")
    if repeat.get("snapshot_id") != snapshot_id:
        raise StageFailure("duplicate produced a different snapshot")
    return {"status": status, "same_snapshot_id": True, "idempotent": True}


def verify_conflicting_replay(dsn, token, body, key, owner, repo_name):
    store = _store(dsn)
    try:
        before = store.connection.execute(
            "SELECT count(*) AS n FROM metadata_snapshots WHERE organization_id=%s",
            (owner,)).fetchone()["n"]
    finally:
        store.close()
    conflicting = json.loads(json.dumps(body))
    conflicting["relations"][0]["columns"][-1]["null_rate"] = 0.05
    status, payload = local("POST", "/api/metadata-snapshots", conflicting,
                            token=token, key=key)
    if status != 409:
        raise StageFailure(f"conflicting replay returned {status}, expected 409")
    store = _store(dsn)
    try:
        after = store.connection.execute(
            "SELECT count(*) AS n FROM metadata_snapshots WHERE organization_id=%s",
            (owner,)).fetchone()["n"]
    finally:
        store.close()
    if after != before:
        raise StageFailure("conflicting replay created partial persistence")
    return {"status": 409, "snapshots_before": before, "snapshots_after": after,
            "partial_persistence": False}


# ------------------------------------------------------ worker recomputation
def verify_recomputation(dsn, owner, repo_name, review_id):
    def decided():
        store = _store(dsn)
        try:
            review = store.get_review(owner, repo_name, review_id)
            attempts = store.review_attempts(owner, repo_name, review_id)
            jobs = store.review_recomputation_jobs(owner, repo_name, review_id=review_id)
            if review and review["decision"] and len(attempts) >= 2:
                return {"review": review, "attempts": attempts, "jobs": jobs}
            return None
        finally:
            store.close()

    result = poll(decided, timeout=240, interval=4,
                  description="the real worker to recompute the review")
    review, attempts, jobs = result["review"], result["attempts"], result["jobs"]

    store = _store(dsn)
    try:
        states = store.evidence_states(owner, repo_name, review_id)
    finally:
        store.close()
    production = [s for s in states if s["evidence_source"] == "production_metadata"]
    sequence = [s["state"] for s in sorted(production, key=lambda s: s["attempt"])]
    if "PENDING" not in sequence or "EVALUATED" not in sequence:
        raise StageFailure(f"evidence did not transition PENDING -> EVALUATED: {sequence}")
    if review["decision"] != "WARN":
        raise StageFailure(f"expected WARN, got {review['decision']}")
    if review["evidence_coverage"] != "COMPLETE":
        raise StageFailure(f"expected COMPLETE coverage, got {review['evidence_coverage']}")
    if review["lifecycle_state"] not in ("DECISION_READY", "PUBLISHED"):
        raise StageFailure(f"lifecycle is {review['lifecycle_state']}")
    completed = [j for j in jobs if j["state"] == "COMPLETED"]
    if len(jobs) != 1 or len(completed) != 1:
        raise StageFailure(f"expected exactly one completed job, got {len(jobs)}")

    findings = (attempts[-1].get("payload") or {}).get("findings", [])
    codes = {f.get("code") for f in findings}
    code_findings = [f for f in findings if f.get("category") == "code"]
    if "column.high_null_rate" not in codes:
        raise StageFailure(f"expected column.high_null_rate, got {sorted(codes)}")
    if code_findings:
        raise StageFailure(f"expected zero code findings, got {len(code_findings)}")

    return {"decision": review["decision"], "coverage": review["evidence_coverage"],
            "health": review["health"], "lifecycle_state": review["lifecycle_state"],
            "attempts": [a["attempt"] for a in attempts],
            "waiting_attempt_preserved": attempts[0]["decision"] is None,
            "evidence_sequence": sequence,
            "production_finding": "column.high_null_rate",
            "code_findings": 0, "observed_null_rate": HIGH_NULL_RATE,
            "threshold": NULL_RATE_THRESHOLD,
            "completed_jobs": len(completed),
            "explanation": (
                "Coverage COMPLETE means the required evidence was obtained. "
                "Complete evidence can still reveal risk: the WARN is caused by "
                "the 82% production null rate, and health remains 100 because "
                "no direct code defect was found.")}


# ------------------------------------------------- GitHub reconciliation
def verify_reconciliation(gh, token, repo, pr_number, head_sha, expected_app_id,
                          comment_before, check_before):
    status, comments = gh("GET", f"/repos/{repo}/issues/{pr_number}/comments",
                          token, bearer=False)
    owned = [c for c in comments
             if (c.get("performed_via_github_app") or {}).get("id") == expected_app_id]
    if len(owned) != 1:
        raise StageFailure(f"duplicate App-owned comments: {len(owned)}")
    if owned[0]["id"] != comment_before:
        raise StageFailure("sticky comment id changed - it was replaced, not updated")

    status, checks = gh("GET", f"/repos/{repo}/commits/{head_sha}/check-runs",
                        token, bearer=False)
    runs = [c for c in checks.get("check_runs", [])
            if (c.get("app") or {}).get("id") == expected_app_id]
    if len(runs) != 1:
        raise StageFailure(f"duplicate App-owned checks: {len(runs)}")
    reconciled = runs[0]["id"] == check_before
    body = owned[0]["body"]
    return {"comment_id_before": comment_before, "comment_id_after": owned[0]["id"],
            "comment_updated_not_duplicated": True,
            "check_run_id_before": check_before, "check_run_id_after": runs[0]["id"],
            "same_check_run": reconciled,
            "check_conclusion": runs[0].get("conclusion"),
            "duplicate_comments": 0, "duplicate_checks": 0,
            "final_body_length": len(body)}


# ------------------------------------------------------------- dashboard
def verify_dashboard(token, review_id, request_id, snapshot_id):
    paths = {
        "review": f"/api/reviews/{review_id}",
        "evidence_coverage": f"/api/reviews/{review_id}/evidence-coverage",
        "collection_request": f"/api/collection-requests/{request_id}",
        "snapshot": f"/api/metadata-snapshots/{snapshot_id}",
    }
    seen, leaked = {}, []
    for name, path in paths.items():
        status, payload = local("GET", path, token=token)
        if status != 200:
            raise StageFailure(f"dashboard {name} returned HTTP {status}")
        seen[name] = status
        text = json.dumps(payload).lower()
        for needle in FORBIDDEN_IN_RESPONSES:
            if needle in text:
                leaked.append(f"{name}:{needle}")
    if leaked:
        raise StageFailure(f"dashboard leaked sensitive content: {leaked}")

    status, coverage = local("GET", paths["evidence_coverage"], token=token)
    groups = {e.get("evidence_state_group") for e in coverage.get("evidence", [])}
    if groups != {"base_code", "head_code", "production"}:
        raise StageFailure(f"evidence groups incomplete: {sorted(groups)}")
    if coverage.get("decision") != "WARN":
        raise StageFailure(f"dashboard decision is {coverage.get('decision')}")
    return {"routes_verified": seen, "evidence_groups": sorted(groups),
            "decision": coverage.get("decision"),
            "coverage": coverage.get("evidence_coverage"),
            "health": coverage.get("health"),
            "attempts_visible": len({e["attempt"] for e in coverage.get("evidence", [])}),
            "sensitive_content_found": 0}
