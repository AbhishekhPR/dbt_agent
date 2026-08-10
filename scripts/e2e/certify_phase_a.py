"""Certify a completed integrated-product run, and emit the Phase B input.

Run 31406121190 carried the entire product chain to a published WARN. The only
thing that failed was the verifier, which read GitHub's publication surfaces
with the fixture token instead of an App installation token. Re-running the
whole E2E to fix a read would create two more pull requests, repoint the App
webhook and ingest two more snapshots - all to re-derive a result that is
already durable in that run's database dump.

So this does not re-run anything. It restores THAT database, reads the
persisted truth out of it, verifies the exact publication ids it recorded
against GitHub with the correct credential, and writes the
`integrated-product-summary.json` the failed run never got to write.

Strictly read-only with respect to the product:

  * no pull request is created, closed or touched
  * the App webhook is never read or modified
  * no snapshot is ingested and no review is recomputed
  * the restored database is a local copy; the product's own state is not
    reachable from here

The only GitHub calls are GETs for one comment id and one check-run id, plus
the POST that mints an installation token - which creates no product state.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from live_flow import StageFailure  # noqa: E402
from metadata_review_e2e import app_jwt, gh, installation_token  # noqa: E402

SOURCE_RUN_ID = os.environ.get("RELIUM_SOURCE_RUN_ID", "31406121190")
REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
OWNER, REPO_NAME = REPO.split("/", 1)

#: The review this certifies, resolved by PULL NUMBER.
#:
#: `review_id_for` produces "gh-" plus 32 hex characters. The id quoted in the
#: certification request is a 26-character PREFIX of the real one - it came
#: from a report that truncated it for display. Resolving by pull number uses
#: an identifier that cannot be abridged, and the prefix is still asserted, so
#: neither a wrong review nor a mistyped prefix can pass.
EXPECTED_REVIEW_ID_PREFIX = os.environ.get(
    "RELIUM_EXPECTED_REVIEW_PREFIX", "gh-c1a35451991e924c41fb39d")
EXPECTED_PULL_NUMBER = int(os.environ.get("RELIUM_EXPECTED_PULL", "57"))
EXPECTED_COMMENT_ID = int(os.environ.get("RELIUM_EXPECTED_COMMENT", "5242686857"))
EXPECTED_CHECK_RUN_ID = int(os.environ.get("RELIUM_EXPECTED_CHECK", "93513476030"))

#: The product result run 31406121190 already produced.
EXPECTED_DECISION = "WARN"
EXPECTED_HEALTH = 100
EXPECTED_PRODUCTION_FINDING = "column.high_null_rate"
UNCHANGED_SIGNAL = "schema_fingerprint"

DUMP_NAME = "integrated-product-review-state.sql"
CHECKSUM_NAME = "integrated-product-review-state.sha256.txt"
SUMMARY_NAME = "integrated-product-summary.json"
MANIFEST_NAME = "phase-b-checksums.sha256.txt"

results = []


def check(name, ok, detail=""):
    results.append({"check": name, "passed": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""),
          flush=True)
    return bool(ok)


# ----------------------------------------------------------------- artifact

def verify_dump_checksum(evidence: Path) -> dict:
    """The dump must be the exact bytes run 31406121190 hashed."""
    dump = evidence / DUMP_NAME
    recorded = evidence / CHECKSUM_NAME
    if not dump.is_file():
        raise StageFailure(f"{DUMP_NAME} is missing from the source artifact")
    if not recorded.is_file():
        raise StageFailure(f"{CHECKSUM_NAME} is missing from the source artifact")

    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    # `sha256sum` writes "<hash>  <path>"; the path is the runner's, not ours.
    expected = recorded.read_text(encoding="utf-8").split()[0].strip()
    if digest != expected:
        raise StageFailure(
            f"dump checksum {digest} does not match the recorded {expected}")
    return {"file": DUMP_NAME, "sha256": digest,
            "matches_recorded_checksum": True, "bytes": dump.stat().st_size}


# ---------------------------------------------------------------- database

def _store(dsn):
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    return PostgresLifecycleStore(dsn, apply_schema=False) \
        if "apply_schema" in PostgresLifecycleStore.__init__.__code__.co_varnames \
        else PostgresLifecycleStore(dsn)


def read_persisted_truth(dsn) -> dict:
    """Everything Phase A proved, read back out of the restored database."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        review = conn.execute(
            "SELECT * FROM reviews WHERE organization_id=%s AND repository_id=%s "
            "AND pull_number=%s", (OWNER, REPO_NAME, EXPECTED_PULL_NUMBER)).fetchone()
        if review is None:
            raise StageFailure(
                f"no review for PR #{EXPECTED_PULL_NUMBER} in the restored database")
        review_id = review["review_id"]
        attempts = conn.execute(
            "SELECT * FROM review_attempts WHERE organization_id=%s AND "
            "repository_id=%s AND review_id=%s ORDER BY attempt",
            (OWNER, REPO_NAME, review_id)).fetchall()
        requests = conn.execute(
            "SELECT * FROM collection_requests WHERE organization_id=%s AND "
            "repository_id=%s AND review_id=%s",
            (OWNER, REPO_NAME, review_id)).fetchall()
        targets = conn.execute(
            "SELECT * FROM collection_request_targets WHERE organization_id=%s "
            "AND repository_id=%s ORDER BY request_id, target_index",
            (OWNER, REPO_NAME)).fetchall()
        snapshots = conn.execute(
            "SELECT * FROM metadata_snapshots WHERE organization_id=%s AND "
            "repository_id=%s ORDER BY observed_at", (OWNER, REPO_NAME)).fetchall()
        bindings = conn.execute(
            "SELECT * FROM snapshot_review_bindings WHERE organization_id=%s AND "
            "repository_id=%s AND review_id=%s",
            (OWNER, REPO_NAME, review_id)).fetchall()
        outbox = conn.execute(
            "SELECT event_type, state, attempts, last_error FROM outbox_events "
            "WHERE organization_id=%s AND repository_id=%s",
            (OWNER, REPO_NAME)).fetchall()
        migrations = [r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]

    return {"review": dict(review), "attempts": [dict(a) for a in attempts],
            "requests": [dict(r) for r in requests],
            "targets": [dict(t) for t in targets],
            "snapshots": [dict(s) for s in snapshots],
            "bindings": [dict(b) for b in bindings],
            "outbox": [dict(o) for o in outbox], "migrations": migrations}


def certify_review(truth) -> dict:
    """Assert the product result run 31406121190 already produced."""
    review = truth["review"]
    ok = True
    ok &= check("review identity matches the certified review",
                str(review["review_id"]).startswith(EXPECTED_REVIEW_ID_PREFIX),
                review["review_id"])
    ok &= check("review is bound to the expected pull request",
                int(review["pull_number"]) == EXPECTED_PULL_NUMBER,
                f"PR #{review['pull_number']}")
    ok &= check("base and head SHAs are persisted",
                bool(review["base_sha"]) and bool(review["head_sha"]),
                f"{str(review['base_sha'])[:12]} -> {str(review['head_sha'])[:12]}")
    ok &= check("final lifecycle state is decided",
                review["lifecycle_state"] in ("DECISION_READY", "PUBLISHED"),
                review["lifecycle_state"])
    ok &= check(f"decision is {EXPECTED_DECISION}",
                review["decision"] == EXPECTED_DECISION, str(review["decision"]))
    ok &= check(f"health is {EXPECTED_HEALTH}",
                int(review["health"]) == EXPECTED_HEALTH, str(review["health"]))
    ok &= check("evidence coverage is COMPLETE",
                review["evidence_coverage"] == "COMPLETE",
                str(review["evidence_coverage"]))
    if not ok:
        raise StageFailure("the restored review is not the certified result")
    return {"review_id": review["review_id"], "pull_number": review["pull_number"],
            "attempt": review["attempt"], "base_sha": review["base_sha"],
            "head_sha": review["head_sha"],
            "lifecycle_state": review["lifecycle_state"],
            "decision": review["decision"], "health": review["health"],
            "evidence_coverage": review["evidence_coverage"]}


def certify_attempts(truth) -> dict:
    review = truth["review"]
    attempts = truth["attempts"]
    final = [a for a in attempts if a["attempt"] == review["attempt"]]
    if not final:
        raise StageFailure("the deciding attempt is not persisted")
    final = final[0]
    waiting = [a for a in attempts if a["lifecycle_state"] == "WAITING_FOR_METADATA"]

    findings = (final.get("payload") or {}).get("findings", [])
    codes = [f.get("code") for f in findings]
    code_findings = [f for f in findings if f.get("category") == "code"]
    comparison_shaped = sorted(
        c for c in codes if isinstance(c, str)
        and ("comparison" in c or "drift" in c or "metadata_change" in c))

    ok = True
    ok &= check("a WAITING_FOR_METADATA attempt precedes the decision",
                bool(waiting), f"attempt {waiting[0]['attempt']}" if waiting else "none")
    ok &= check(f"{EXPECTED_PRODUCTION_FINDING} is present",
                EXPECTED_PRODUCTION_FINDING in codes, str(sorted(c for c in codes if c)))
    ok &= check("zero code findings", not code_findings, str(len(code_findings)))
    ok &= check("no comparison/drift-derived finding",
                not comparison_shaped, str(comparison_shaped))
    if not ok:
        raise StageFailure("the persisted findings are not the certified result")
    return {"attempt": final["attempt"],
            "waiting_attempt": waiting[0]["attempt"] if waiting else None,
            "trigger": final["trigger"], "snapshot_id": final["snapshot_id"],
            "finding_codes": sorted(c for c in codes if c),
            "code_findings": 0, "comparison_derived_findings": 0}


def certify_semantic(truth) -> dict:
    review = truth["review"]
    waiting = [a for a in truth["attempts"]
               if a["lifecycle_state"] == "WAITING_FOR_METADATA"]
    evidence = None
    for attempt in truth["attempts"]:
        if isinstance(attempt.get("semantic_evidence"), dict):
            evidence = attempt["semantic_evidence"]
            break
    if evidence is None:
        raise StageFailure("no attempt persisted semantic evidence")
    changes = [{**c, "model_name": c.get("model_name") or m.get("model_name")}
               for m in (evidence.get("models") or [])
               for c in (m.get("changes") or [])]
    required = [c for c in changes
                if c.get("kind") == "filter_changed"
                and c.get("model_name") == "int_customer_orders"
                and c.get("scope") == "where"]
    ok = check("semantic evidence is evaluated",
               evidence.get("status") in ("evaluated", "partial"),
               str(evidence.get("status")))
    ok &= check("filter_changed on int_customer_orders scope=where",
                bool(required),
                str([{k: c.get(k) for k in ("kind", "model_name", "scope")}
                     for c in changes]))
    if not ok:
        raise StageFailure("semantic evidence is not the certified result")
    return {"status": evidence.get("status"), "change_count": len(changes),
            "change_kinds": sorted({c.get("kind") for c in changes}),
            "models": sorted({c.get("model_name") for c in changes if c.get("model_name")}),
            "required_change": {"kind": "filter_changed",
                                "model_name": "int_customer_orders",
                                "scope": "where"},
            "waiting_attempt": waiting[0]["attempt"] if waiting else None,
            "review_id": review["review_id"]}


def certify_blast_radius(truth) -> dict:
    plan = (truth["review"].get("payload") or {}).get("plan") or {}
    downstream = sorted(plan.get("downstream_models") or [])
    ok = check("direct blast radius is persisted", bool(downstream), str(downstream))
    ok &= check("blast radius uses dbt node identities",
                all(str(n).startswith("model.") for n in downstream), str(downstream))
    if not ok:
        raise StageFailure("blast radius is not the certified result")
    return {"direct_downstream_models": downstream,
            "identity": "dbt node id", "transitive_expansion": False}


def certify_request(truth) -> dict:
    requests = truth["requests"]
    if len(requests) != 1:
        raise StageFailure(f"expected exactly one request, got {len(requests)}")
    request = requests[0]
    targets = [t for t in truth["targets"] if t["request_id"] == request["request_id"]]
    relations = sorted({t["relation_name"] for t in targets})
    columns = sorted({c for t in targets for c in (t.get("columns") or [])})
    signals = sorted({s for t in targets for s in (t.get("required_signals") or [])})
    forbidden = sorted(set(signals) & {"raw_rows", "sample_rows", "sql",
                                       "arbitrary_sql", "query"})
    ok = check("metadata request is bounded", len(relations) <= 3, str(relations))
    ok &= check("request asks for no raw rows or SQL", not forbidden, str(forbidden))
    if not ok:
        raise StageFailure("the metadata request is not the certified result")
    return {"request_id": request["request_id"], "state": request["state"],
            "relations": relations, "columns": columns,
            "required_signals": signals, "bounded": True,
            "raw_row_request": False, "arbitrary_sql_request": False}


def certify_comparison(truth) -> dict:
    review = truth["review"]
    final = [a for a in truth["attempts"] if a["attempt"] == review["attempt"]][0]
    comparison = final.get("metadata_comparison")
    if not isinstance(comparison, dict):
        raise StageFailure("the deciding attempt persisted no metadata comparison")

    by_signal = {}
    for change in comparison.get("changes") or []:
        by_signal.setdefault(change.get("signal"), []).append(change)

    snapshots = {s["snapshot_id"]: s for s in truth["snapshots"]}
    baseline_id = comparison.get("baseline_snapshot_id")
    current_id = comparison.get("current_snapshot_id")

    def delta(signal, field):
        found = by_signal.get(signal) or []
        return found[0].get(field) if len(found) == 1 else None

    ok = check("comparison status is evaluated",
               comparison.get("status") == "evaluated", str(comparison.get("status")))
    known = baseline_id in snapshots and current_id in snapshots
    ok &= check("baseline A and current B are both persisted snapshots",
                known, f"{baseline_id} -> {current_id}")
    if not known:
        # Everything below reads those snapshots. Continuing would raise a
        # TypeError on a missing row and disguise a clean refusal as a crash.
        raise StageFailure(
            f"the comparison names snapshots that are not in this database: "
            f"{baseline_id} -> {current_id}")
    ok &= check("row_count 1000 -> 800 (-200, -20%)",
                delta("row_count", "before") == 1000
                and delta("row_count", "after") == 800
                and delta("row_count", "absolute_delta") == -200
                and abs((delta("row_count", "relative_delta") or 0) + 0.2) < 1e-9,
                f"{delta('row_count', 'before')} -> {delta('row_count', 'after')}")
    ok &= check("null_rate 0.01 -> 0.82 (+81.0 percentage points)",
                abs((delta("null_rate", "percentage_point_delta") or 0) - 81.0) < 1e-6,
                str(delta("null_rate", "percentage_point_delta")))
    ok &= check("cardinality 0.37 -> 0.42 (+5.0 percentage points)",
                abs((delta("cardinality", "percentage_point_delta") or 0) - 5.0) < 1e-6,
                str(delta("cardinality", "percentage_point_delta")))
    ok &= check(f"unchanged {UNCHANGED_SIGNAL} is absent from the changes",
                UNCHANGED_SIGNAL not in by_signal, str(sorted(by_signal)))
    ok &= check("no column availability change in this fixture",
                "column_exists" not in by_signal, str(sorted(by_signal)))

    baseline = snapshots.get(baseline_id) or {}
    current = snapshots.get(current_id) or {}
    ok &= check("both observations are COMPLETE and CURRENT",
                baseline.get("completeness") == "COMPLETE"
                and current.get("completeness") == "COMPLETE"
                and baseline.get("freshness_state") == "CURRENT"
                and current.get("freshness_state") == "CURRENT",
                f"A {baseline.get('freshness_state')} / B {current.get('freshness_state')}")
    ok &= check("A strictly precedes B",
                baseline.get("observed_at") < current.get("observed_at"),
                f"{baseline.get('observed_at')} < {current.get('observed_at')}")
    if not ok:
        raise StageFailure("the metadata comparison is not the certified result")

    accepted = [b for b in truth["bindings"] if b["binding_state"] == "ACCEPTED"]
    return {"status": comparison["status"],
            "baseline_snapshot_id": baseline_id,
            "current_snapshot_id": current_id,
            "baseline_observed_at": str(baseline.get("observed_at")),
            "current_observed_at": str(current.get("observed_at")),
            "signals_changed": sorted(by_signal),
            "change_count": len(comparison.get("changes") or []),
            "row_count": [1000, 800, -200, -0.2],
            "null_rate_percentage_points": delta("null_rate", "percentage_point_delta"),
            "cardinality_percentage_points": delta("cardinality",
                                                   "percentage_point_delta"),
            "unchanged_signal_for_phase_b": UNCHANGED_SIGNAL,
            "accepted_bindings": [b["snapshot_id"] for b in accepted]}


def certify_publication_state(truth) -> dict:
    review = truth["review"]
    comment = review.get("github_comment_id")
    check_run = review.get("github_check_run_id")
    reconcile = [o for o in truth["outbox"]
                 if o["event_type"] == "review.publication_reconcile_requested"]
    ok = check("persisted comment id matches the certified publication",
               comment is not None and int(comment) == EXPECTED_COMMENT_ID, str(comment))
    ok &= check("persisted check run id matches the certified publication",
                check_run is not None and int(check_run) == EXPECTED_CHECK_RUN_ID,
                str(check_run))
    ok &= check("every publication job completed",
                bool(reconcile) and all(o["state"] == "COMPLETED" for o in reconcile),
                str([(o["state"], o["last_error"]) for o in reconcile]))
    if not ok:
        raise StageFailure("the persisted publication state is not the certified result")
    return {"github_comment_id": int(comment), "github_check_run_id": int(check_run),
            "publication_jobs": [{"event_type": o["event_type"], "state": o["state"],
                                  "attempts": o["attempts"]} for o in reconcile]}


# ------------------------------------------------------------------ GitHub

def verify_exact_publications(head_sha) -> dict:
    """Verify the EXACT recorded ids, never "the latest" publication.

    Read with an App installation token. The fixture token is deliberately not
    used and is not even read here: it carries no `checks: read`, and
    `assert_fixture_token_scope` already declares it is never used for
    comments, check runs or App authentication.
    """
    jwt = app_jwt()
    status, app = gh("GET", "/app", jwt)
    if status != 200:
        raise StageFailure(f"cannot read the App: HTTP {status}")
    slug, app_id = app.get("slug"), app.get("id")
    if slug != APP_SLUG or "pilot" in str(slug).lower():
        raise StageFailure(f"refusing to certify against app slug {slug!r}")

    token = installation_token(jwt)

    status, comment = gh("GET", f"/repos/{REPO}/issues/comments/{EXPECTED_COMMENT_ID}",
                         token, bearer=False)
    ok = check(f"comment {EXPECTED_COMMENT_ID} exists", status == 200, f"HTTP {status}")
    issue_url = str(comment.get("issue_url") or "")
    ok &= check(f"comment is on PR #{EXPECTED_PULL_NUMBER}",
                issue_url.endswith(f"/issues/{EXPECTED_PULL_NUMBER}"), issue_url)
    comment_app = (comment.get("performed_via_github_app") or {}).get("id")
    ok &= check("comment is owned by the expected App identity",
                comment_app == app_id, f"app {comment_app}")

    status, run = gh("GET", f"/repos/{REPO}/check-runs/{EXPECTED_CHECK_RUN_ID}",
                     token, bearer=False)
    ok &= check(f"check run {EXPECTED_CHECK_RUN_ID} exists", status == 200,
                f"HTTP {status}")
    ok &= check("check run head SHA matches the persisted review head",
                run.get("head_sha") == head_sha, str(run.get("head_sha"))[:12])
    ok &= check("check run is completed",
                run.get("status") == "completed",
                f"{run.get('status')}/{run.get('conclusion')}")
    run_app = (run.get("app") or {}).get("id")
    ok &= check("check run is owned by the expected App identity",
                run_app == app_id, f"app {run_app}")
    if not ok:
        raise StageFailure("the exact publications did not verify")

    return {"app_slug": slug, "app_id": app_id,
            "comment": {"id": EXPECTED_COMMENT_ID, "pull_number": EXPECTED_PULL_NUMBER,
                        "owned_by_app": True},
            "check_run": {"id": EXPECTED_CHECK_RUN_ID, "head_sha": run.get("head_sha"),
                          "status": run.get("status"),
                          "conclusion": run.get("conclusion"), "owned_by_app": True},
            "read_with": "App installation token",
            "fixture_token_used": False,
            "selected_by": "exact persisted ids, not a search"}


# ------------------------------------------------------------------- main

def main() -> int:
    evidence = Path(sys.argv[1] if len(sys.argv) > 1 else "phase-b-evidence")
    source = Path(sys.argv[2] if len(sys.argv) > 2 else "source-artifact")
    dsn = os.environ["RELIUM_RESTORED_DSN"]
    evidence.mkdir(parents=True, exist_ok=True)

    print(f"\n== 1. source artifact from run {SOURCE_RUN_ID} ==")
    dump = verify_dump_checksum(source)
    check("dump checksum matches the value run "
          f"{SOURCE_RUN_ID} recorded", True, dump["sha256"][:16] + "...")

    print("\n== 2. persisted Phase A truth ==")
    truth = read_persisted_truth(dsn)
    check("migrations 0001-0012 present in the restored database",
          truth["migrations"] == list(range(1, 13)), str(truth["migrations"]))
    review = certify_review(truth)
    attempts = certify_attempts(truth)

    print("\n== 3. semantic, blast radius, request ==")
    semantic = certify_semantic(truth)
    blast = certify_blast_radius(truth)
    request = certify_request(truth)

    print("\n== 4. metadata comparison A -> B ==")
    comparison = certify_comparison(truth)

    print("\n== 5. persisted publication ==")
    publication_state = certify_publication_state(truth)

    print("\n== 6. exact GitHub publications ==")
    publications = verify_exact_publications(review["head_sha"])

    failed = [r["check"] for r in results if not r["passed"]]
    if failed:
        raise StageFailure(f"certification failed: {failed}")

    summary = {
        "certification": "phase-a",
        "source_run_id": SOURCE_RUN_ID,
        "repository": REPO,
        "pull_number": review["pull_number"],
        "review_id": review["review_id"],
        "attempt": review["attempt"],
        "base_sha": review["base_sha"],
        "head_sha": review["head_sha"],
        "lifecycle_state": review["lifecycle_state"],
        "decision": review["decision"],
        "health": review["health"],
        "evidence_coverage": review["evidence_coverage"],
        "findings": attempts["finding_codes"],
        "code_findings": attempts["code_findings"],
        "comparison_derived_findings": attempts["comparison_derived_findings"],
        "semantic": semantic,
        "blast_radius": blast,
        "metadata_request": request,
        "baseline_snapshot_id": comparison["baseline_snapshot_id"],
        "current_snapshot_id": comparison["current_snapshot_id"],
        "metadata_comparison": comparison,
        "publication": {**publication_state, "verified": publications},
        "phase_b_inputs": {
            "review_id": review["review_id"],
            "attempt": review["attempt"],
            "baseline_snapshot_id": comparison["baseline_snapshot_id"],
            "current_snapshot_id": comparison["current_snapshot_id"],
            "evidence_download_route":
                f"/api/reviews/{review['review_id']}/attempts/{review['attempt']}"
                f"/metadata-evidence.json",
            "unchanged_signal": UNCHANGED_SIGNAL,
            "database_dump": DUMP_NAME,
        },
        "product_mutation": {
            "pull_requests_created": 0, "pull_requests_modified": 0,
            "webhook_read_or_modified": False, "snapshots_ingested": 0,
            "reviews_recomputed": 0,
            "github_writes": 0,
            "note": ("read-only certification of run "
                     f"{SOURCE_RUN_ID}; the restored database is a local copy"),
        },
        "checks": results,
    }
    (evidence / SUMMARY_NAME).write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")

    # Carry the ORIGINAL dump through unmodified, so the Phase B artifact holds
    # the exact database run 31406121190 produced.
    import shutil

    shutil.copy2(source / DUMP_NAME, evidence / DUMP_NAME)
    shutil.copy2(source / CHECKSUM_NAME, evidence / CHECKSUM_NAME)

    manifest = []
    for name in sorted((DUMP_NAME, SUMMARY_NAME, CHECKSUM_NAME)):
        digest = hashlib.sha256((evidence / name).read_bytes()).hexdigest()
        manifest.append(f"{digest}  {name}")
    (evidence / MANIFEST_NAME).write_text("\n".join(manifest) + "\n", encoding="utf-8")

    print("\n" + "=" * 62)
    print(f"  {sum(1 for r in results if r['passed'])}/{len(results)} checks passed")
    print(f"  Phase B artifact written to {evidence}")
    for line in manifest:
        print("   ", line)
    print("  PHASE A CERTIFIED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageFailure as exc:
        print(f"PHASE A CERTIFICATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
