"""Genuine GitHub metadata-review E2E driver for a fresh ephemeral Linux runner.

Everything - PostgreSQL, API, worker, tunnel - runs on ONE host, so no
cross-OS forwarding is involved.

Fail-closed by construction. There is no placeholder success path: main()
returns 0 only after StageTracker.assert_all_complete() confirms every stage
in REQUIRED_STAGES was marked by a real assertion. Writing an evidence file
cannot mark a stage.

Safety ordering:
  * the webhook configuration is preserved BEFORE any mutation;
  * cleanup is armed BEFORE the mutation flag can be set;
  * the flag is set BEFORE the GitHub call, so a partial failure still restores;
  * restoration is the FIRST cleanup action;
  * cleanup failure makes the run fail even when the flow passed.

Nothing secret is printed or written.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Both the harness directory AND the repository root must be importable:
# sys.path[0] is the script's own directory, so `import agent...` fails
# without the root even when the process is launched from it.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf                                    # noqa: E402
import verify_flow as vf                                  # noqa: E402
from live_flow import BASE_URL, ENVIRONMENT, PORT, StageFailure, local, poll  # noqa: E402
from stages import REQUIRED_STAGES, StageIncomplete, StageTracker  # noqa: E402

REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
API = "https://api.github.com"
# Separate fine-grained credential. The App keeps contents:read; this token
# is the ONLY thing permitted to write repository contents, and only for
# fixture branch/commit/PR operations.
FIXTURE_TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")
RUN = uuid.uuid4().hex[:10]

EV = Path(sys.argv[1])
CLEANUP_ONLY = "--cleanup-only" in sys.argv
EV.mkdir(parents=True, exist_ok=True)
RECOVERY = EV / "webhook-recovery-record.json"

state = {"mutated": False, "procs": [], "tunnel": None, "pr_number": None,
         "branches": [], "expected_slug": APP_SLUG, "cleanup_ok": None}
# StageTracker starts every stage incomplete and write() overwrites the file,
# so the workflow's outer --cleanup-only process destroyed the driver's stage
# record in run 8: the uploaded tracker reported 2 of 27 complete and said
# nothing about the run it was supposed to describe. The two processes must
# not share a path.
tracker = StageTracker(EV / ("stage-tracker-outer.json" if CLEANUP_ONLY
                             else "stage-tracker.json"))
checks: list[dict] = []


def check(name, ok, detail=""):
    checks.append({"check": name, "passed": bool(ok), "detail": str(detail)[:200]})
    print(f'[{"ok" if ok else "XX"}] {name}  {str(detail)[:90]}', flush=True)
    return bool(ok)


def write(name, doc):
    (EV / name).write_text(json.dumps(doc, indent=2, sort_keys=True, default=str)
                           + "\n", encoding="utf-8")


# ----------------------------------------------------------------- GitHub
def app_jwt() -> str:
    key_path = os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"]
    app_id = os.environ["RELIUM_GITHUB_APP_ID"]
    now = int(time.time())
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()).rstrip(b"=")
    signing_input = header + b"." + payload
    proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path],
                          input=signing_input, capture_output=True, check=True)
    return (signing_input + b"." +
            base64.urlsafe_b64encode(proc.stdout).rstrip(b"=")).decode()


def gh(method, path, token, body=None, bearer=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "relium-metadata-e2e")
    req.add_header("Authorization", f"{'Bearer' if bearer else 'token'} {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return exc.code, {}


def installation_token(jwt=None):
    inst_id = os.environ["RELIUM_E2E_INSTALLATION_ID"]
    status, tok = gh("POST", f"/app/installations/{inst_id}/access_tokens",
                     jwt or app_jwt())
    if status != 201:
        raise StageFailure(f"could not mint installation token: HTTP {status}")
    return tok["token"]


# ---------------------------------------------------------------- cleanup
def preserve_webhook(gh_fn=None, jwt_fn=None):
    """Record the current webhook config so cleanup can put it back.

    Any driver that repoints the App webhook must call this BEFORE the
    mutation. `governance_e2e` did not, so its cleanup had nothing to restore
    from and reported a failure it could not explain.
    """
    gh_fn, jwt_fn = gh_fn or gh, jwt_fn or app_jwt
    status, hook = gh_fn("GET", "/app/hook/config", jwt_fn())
    if status != 200:
        raise StageFailure("cannot read the current webhook configuration")
    RECOVERY.write_text(json.dumps({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": hook.get("url"),
        "content_type": hook.get("content_type", "json"),
        "secret_captured": False}, indent=2, sort_keys=True), encoding="utf-8")
    return {"preserved_url_host": (hook.get("url") or "//?").split("//")[-1],
            "secret_captured": False}


def restore_webhook():
    if not RECOVERY.is_file():
        # No record of the original, so restoration is impossible - not
        # merely unperformed. Say so in the same vocabulary as the success
        # case, or the caller's `verified_through_github` check reports this
        # as a generic failure and hides why.
        return {"restored": False, "verified_through_github": False,
                "reason": "no recovery record: the original webhook "
                          "configuration was never preserved by this process"}
    record = json.loads(RECOVERY.read_text(encoding="utf-8"))
    jwt = app_jwt()
    patch_status, _ = gh("PATCH", "/app/hook/config", jwt,
                         {"url": record["url"],
                          "content_type": record["content_type"]})
    status, confirmed = gh("GET", "/app/hook/config", jwt)
    confirmed.pop("secret", None)
    verified = status == 200 and confirmed.get("url") == record["url"]
    return {"patch_status": patch_status, "verified_through_github": verified,
            "matches_original": verified, "secret_touched": False}


def cleanup(reason="normal"):
    """Idempotent. Restoration first, then evidence, PR, tunnel, processes."""
    if state.get("cleanup_done"):
        return state.get("cleanup_result", {})
    state["cleanup_done"] = True
    result = {"reason": reason, "at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "failures": []}

    # 1. restore the webhook - the only outward-facing change
    if state["mutated"]:
        try:
            result["webhook"] = restore_webhook()
            if not result["webhook"].get("verified_through_github"):
                result["failures"].append("webhook restoration not verified")
        except Exception as exc:  # noqa: BLE001
            result["webhook"] = {"restored": False, "error": type(exc).__name__}
            result["failures"].append(f"webhook restore raised {type(exc).__name__}")
    else:
        # This process changed nothing, so it has nothing to restore. Saying
        # "restored: true" here read as "the webhook is confirmed correct",
        # which it never was: the outer --cleanup-only process runs with fresh
        # state and would report success while doing nothing at all.
        result["webhook"] = {
            "restored": None,
            "verified_through_github": False,
            "note": "this process made no webhook change and has no record "
                    "of the original; restoration was neither needed nor "
                    "attempted by it",
        }

    # 2. close (never merge) the fixture PRs
    closed = []
    for number in ([state["pr_number"]] if state["pr_number"] else []) + \
            state.get("extra_prs", []):
        try:
            token = FIXTURE_TOKEN or installation_token()
            st, pr = gh("GET", f"/repos/{REPO}/pulls/{number}", token, bearer=False)
            if st == 200 and pr.get("merged"):
                result["failures"].append(f"fixture PR #{number} was MERGED")
            gh("PATCH", f"/repos/{REPO}/pulls/{number}", token,
               {"state": "closed",
                "title": f"[E2E FIXTURE - DO NOT MERGE] metadata review {RUN}"},
               bearer=False)
            closed.append(number)
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(f"closing PR #{number}: {type(exc).__name__}")
    result["fixture_prs_closed"] = closed
    result["fixture_prs_merged"] = False

    # 2b. remove the fixture branches. state["branches"] was populated at
    # creation but nothing ever consumed it, so every run so far left its
    # branches behind. The workflow's outer always-step also runs in a FRESH
    # process with empty state, so removal must additionally sweep by the
    # e2e/ prefix inside the dedicated synthetic repository. Branch deletion
    # needs contents:write, which only the fixture token holds.
    swept, deleted, remaining = [], [], []
    if FIXTURE_TOKEN:
        try:
            st, open_prs = gh("GET", f"/repos/{REPO}/pulls?state=open&per_page=100",
                              FIXTURE_TOKEN, bearer=False)
            for pr in (open_prs if st == 200 and isinstance(open_prs, list) else []):
                ref = (pr.get("head") or {}).get("ref") or ""
                if not ref.startswith("e2e/") or pr["number"] in closed:
                    continue
                if pr.get("merged"):
                    result["failures"].append(f"fixture PR #{pr['number']} was MERGED")
                gh("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", FIXTURE_TOKEN,
                   {"state": "closed"}, bearer=False)
                swept.append(pr["number"])
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(f"sweeping fixture PRs: {type(exc).__name__}")
        try:
            st, refs = gh("GET", f"/repos/{REPO}/git/matching-refs/heads/e2e/",
                          FIXTURE_TOKEN, bearer=False)
            found = [r["ref"].split("refs/heads/", 1)[1]
                     for r in (refs if st == 200 and isinstance(refs, list) else [])]
            for name in sorted(set(found) | set(state.get("branches", []))):
                dst, _ = gh("DELETE", f"/repos/{REPO}/git/refs/heads/{name}",
                            FIXTURE_TOKEN, bearer=False)
                if dst in (204, 404, 422):
                    deleted.append(name)
                else:
                    remaining.append(name)
                    result["failures"].append(
                        f"fixture branch {name} not deleted: HTTP {dst}")
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(
                f"deleting fixture branches: {type(exc).__name__}")
    else:
        result["failures"].append(
            "no fixture token available to remove fixture branches")
    result["fixture_prs_swept"] = swept
    result["fixture_branches_deleted"] = deleted
    result["fixture_branches_remaining"] = remaining

    # 3. tunnel, then API/worker
    if state["tunnel"]:
        try:
            state["tunnel"]["proc"].terminate()
            state["tunnel"]["proc"].wait(timeout=15)
        except Exception:
            try:
                state["tunnel"]["proc"].kill()
            except Exception:
                pass
    result["tunnel_stopped"] = state["tunnel"] is not None
    stopped = []
    for label, proc in state["procs"]:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        stopped.append(label)
    result["processes_stopped"] = stopped

    # 4. no public listener remains
    try:
        local("GET", "/healthz", timeout=5)
        result["local_listener_still_up"] = True
        result["failures"].append("API still listening after cleanup")
    except Exception:
        result["local_listener_still_up"] = False

    result["cleanup_passed"] = not result["failures"]
    state["cleanup_result"] = result
    state["cleanup_ok"] = result["cleanup_passed"]
    # The workflow's outer always-step invokes cleanup a second time in a
    # fresh process. Run 7 let that empty-state pass overwrite the driver's
    # own record, destroying the evidence of what the run actually cleaned up.
    evidence_name = ("cleanup-verification-outer.json" if CLEANUP_ONLY
                     else "cleanup-verification.json")
    write(evidence_name, result)
    if result["cleanup_passed"]:
        try:
            tracker.complete("cleanup_verified", {
                "webhook_restored": result["webhook"].get(
                    "verified_through_github", result["webhook"].get("restored")),
                "processes_stopped": stopped,
                "listener_gone": not result["local_listener_still_up"]})
            if state["mutated"]:
                tracker.complete("webhook_restored", result["webhook"])
        except Exception as exc:  # noqa: BLE001
            # Never swallow this: an unmarked stage must surface, not vanish.
            result["failures"].append(
                f"could not mark cleanup stages: {type(exc).__name__}")
            result["cleanup_passed"] = False
            state["cleanup_ok"] = False
            write(evidence_name, result)
    print(f"[cleanup:{reason}] passed={result['cleanup_passed']} "
          f"failures={result['failures']}", flush=True)
    return result


def arm():
    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda s, _f: (cleanup(f"signal-{s}"), sys.exit(130)))
        except (ValueError, OSError):
            pass


# ------------------------------------------------------------- variants
def issue_token(dsn, owner, repo_name):
    from agent.api.auth import generate_token, hash_secret
    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    store = PostgresLifecycleStore(dsn)
    try:
        store.ensure_tenant(owner, repo_name, ENVIRONMENT)
        token_id, secret, presented = generate_token()
        store.create_service_token(token_id, hash_secret(secret), owner, repo_name,
                                   environment=ENVIRONMENT, description="e2e")
    finally:
        store.close()
    return presented


def run_variant(letter, *, dsn, owner, repo_name, token, mode, manifest_variant,
                snapshot_kwargs, expect, since):
    """Create a real fixture PR, drive the real flow, assert the outcome."""
    pr = lf.create_fixture_pr(state, gh, FIXTURE_TOKEN, REPO, f"{RUN}-{letter.lower()}",
                              variant=manifest_variant, enforcement_mode=mode)
    state.setdefault("extra_prs", []).append(pr["pr_number"])
    review = vf.verify_postgres_review(dsn, owner, repo_name, pr["head_sha"],
                                       pr["base_sha"])
    store_request = vf.verify_targeted_request(dsn, owner, repo_name,
                                               review["review_id"])

    if snapshot_kwargs is None:                       # variant C: no evidence
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        store = PostgresLifecycleStore(dsn)
        try:
            row = store.get_review(owner, repo_name, review["review_id"])
        finally:
            store.close()
        if row["decision"] is not None or row["evidence_coverage"] != "INCOMPLETE":
            raise StageFailure(f"variant {letter}: expected undecided/INCOMPLETE")
        return {"variant": letter, "pr_number": pr["pr_number"],
                "lifecycle_state": row["lifecycle_state"],
                "decision": row["decision"], "coverage": row["evidence_coverage"],
                "health": row["health"], "matched_expectation": True}

    body = vf.snapshot_body(review, store_request["request_id"], **snapshot_kwargs)
    key = f"e2e-{letter.lower()}-{uuid.uuid4().hex[:10]}"
    status, resp = local("POST", "/api/metadata-snapshots", body, token=token, key=key)
    if status != 202:
        raise StageFailure(f"variant {letter}: snapshot HTTP {status}")

    def decided():
        from agent.postgres_lifecycle_store import PostgresLifecycleStore
        store = PostgresLifecycleStore(dsn)
        try:
            row = store.get_review(owner, repo_name, review["review_id"])
            return row if row and row["decision"] else None
        finally:
            store.close()

    row = poll(decided, timeout=240, interval=4,
               description=f"variant {letter} decision")
    if row["decision"] != expect["decision"]:
        raise StageFailure(
            f"variant {letter}: expected {expect['decision']}, got {row['decision']}")
    if "coverage" in expect and row["evidence_coverage"] != expect["coverage"]:
        raise StageFailure(f"variant {letter}: coverage {row['evidence_coverage']}")
    return {"variant": letter, "pr_number": pr["pr_number"],
            "decision": row["decision"], "coverage": row["evidence_coverage"],
            "health": row["health"], "lifecycle_state": row["lifecycle_state"],
            "snapshot_id": resp.get("snapshot_id"), "matched_expectation": True}


# ----------------------------------------------------------------- main
def main() -> int:
    if CLEANUP_ONLY:
        state["mutated"] = RECOVERY.is_file()
        result = cleanup("workflow-always-step")
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("cleanup_passed") else 1

    arm()   # armed BEFORE the mutation flag can ever be set
    workdir = REPO_ROOT
    dsn = os.environ["RELIUM_DATABASE_URL"]
    owner, repo_name = REPO.split("/")
    mode = os.environ.get("RELIUM_ENFORCEMENT_MODE", "enforce")
    run_variants = os.environ.get("RELIUM_RUN_VARIANTS", "true").lower() == "true"

    # ---- environment gate --------------------------------------------
    tracker.begin("environment_gate")
    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    store = PostgresLifecycleStore(dsn)
    versions = sorted(r["version"] for r in store.connection.execute(
        "SELECT version FROM schema_migrations").fetchall())
    role = store.connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
        "WHERE rolname=current_user").fetchone()
    store.close()
    check("migrations 1-4 applied from empty", versions == [1, 2, 3, 4], versions)
    check("application role is least-privileged",
          not any([role["rolsuper"], role["rolcreatedb"], role["rolcreaterole"]]))
    jwt = app_jwt()
    status, app = gh("GET", "/app", jwt)
    slug = app.get("slug", "")
    expected_app_id = int(app.get("id", 0))
    check("App is the dedicated E2E App, not Relium Pilot",
          status == 200 and slug == APP_SLUG and "pilot" not in slug.lower(), slug)
    inst_token = installation_token(jwt)
    status, repos = gh("GET", "/installation/repositories", inst_token, bearer=False)
    names = [r["full_name"] for r in (repos.get("repositories") or [])]
    check("App accesses exactly the synthetic E2E repository", names == [REPO], names)
    check("fixture token is configured", bool(FIXTURE_TOKEN))
    if FIXTURE_TOKEN:
        scope = lf.assert_fixture_token_scope(gh, FIXTURE_TOKEN, REPO)
        check("fixture token's PRIVATE reach is exactly the E2E repository",
              scope["private_repositories_visible"] == [REPO],
              scope["private_repositories_visible"])
        write("fixture-token-scope.json", scope)
    if not all(c["passed"] for c in checks):
        write("prelive-safety-final.json", {"checks": checks, "gate_passed": False})
        raise StageFailure("pre-live gate failed - no outward-facing change made")
    write("prelive-safety-final.json", {"checks": checks, "gate_passed": True,
                                        "run_id": RUN})
    tracker.complete("environment_gate", {"migrations": versions,
                                          "app_slug": slug, "repositories": names})

    # ---- preserve the webhook BEFORE any mutation ---------------------
    tracker.begin("webhook_preserved")
    status, hook = gh("GET", "/app/hook/config", jwt)
    if status != 200:
        raise StageFailure("cannot read the current webhook configuration")
    RECOVERY.write_text(json.dumps({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_slug": slug, "url": hook.get("url"),
        "content_type": hook.get("content_type", "json"),
        "secret_captured": False}, indent=2, sort_keys=True), encoding="utf-8")
    tracker.complete("webhook_preserved", {"preserved_url_host":
                                           (hook.get("url") or "//?").split("//")[-1],
                                           "secret_captured": False})

    # ---- start the real system ---------------------------------------
    storage = EV.parent / f"relium-storage-{RUN}"
    storage.mkdir(parents=True, exist_ok=True)
    tracker.begin("api_started")
    tracker.complete("api_started", lf.start_api(
        state, workdir, dsn, storage,
        os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"],
        os.environ["RELIUM_GITHUB_APP_ID"],
        os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"], EV / "api.log"))
    tracker.begin("worker_started")
    tracker.complete("worker_started",
                     lf.start_worker(state, workdir, dsn, EV / "worker.log"))
    tracker.begin("tunnel_started")
    tracker.complete("tunnel_started", lf.start_tunnel(state, EV / "tunnel.log"))
    tunnel_url = state["tunnel"]["url"]

    # ---- repoint and verify the webhook ------------------------------
    tracker.begin("webhook_updated")
    tracker.complete("webhook_updated",
                     lf.point_webhook(state, gh, app_jwt, tunnel_url))
    tracker.begin("webhook_verified")
    tracker.complete("webhook_verified", lf.verify_webhook(gh, app_jwt, tunnel_url))

    since = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    # ---- genuine synthetic pull request ------------------------------
    tracker.begin("fixture_pr_created")
    pr = lf.create_fixture_pr(state, gh, FIXTURE_TOKEN, REPO, RUN,
                              variant="external", enforcement_mode=mode)
    tracker.complete("fixture_pr_created", pr)

    tracker.begin("genuine_webhook_received")
    tracker.complete("genuine_webhook_received",
                     vf.verify_genuine_webhook(gh, app_jwt, since, pr["pr_number"]))
    write("genuine-webhook-evidence.json", tracker._stages["genuine_webhook_received"])

    tracker.begin("postgres_review_verified")
    review = vf.verify_postgres_review(dsn, owner, repo_name, pr["head_sha"],
                                       pr["base_sha"])
    tracker.complete("postgres_review_verified", review)

    tracker.begin("targeted_request_verified")
    request = vf.verify_targeted_request(dsn, owner, repo_name, review["review_id"])
    tracker.complete("targeted_request_verified", request)
    write("targeted-collection-request.json", request)

    tracker.begin("waiting_publication_verified")
    waiting = vf.verify_waiting_publication(
        dsn, gh, installation_token(), REPO, pr["pr_number"], owner, repo_name,
        review["review_id"], expected_app_id)
    tracker.complete("waiting_publication_verified", waiting)
    write("waiting-publication-evidence.json", waiting)

    # ---- snapshot through the real public collector API ---------------
    token = issue_token(dsn, owner, repo_name)
    tracker.begin("primary_snapshot_submitted")
    primary, key, body = vf.submit_primary_snapshot(token, review,
                                                    request["request_id"])
    tracker.complete("primary_snapshot_submitted", primary)
    tracker.begin("duplicate_snapshot_verified")
    tracker.complete("duplicate_snapshot_verified",
                     vf.verify_duplicate(token, body, key, primary["snapshot_id"]))
    tracker.begin("conflicting_replay_verified")
    tracker.complete("conflicting_replay_verified",
                     vf.verify_conflicting_replay(dsn, token, body, key, owner,
                                                  repo_name))
    write("snapshot-submission-evidence.json",
          {"primary": primary,
           "duplicate": tracker._stages["duplicate_snapshot_verified"]["proof"],
           "conflicting": tracker._stages["conflicting_replay_verified"]["proof"]})

    # ---- real worker recomputation -----------------------------------
    tracker.begin("recomputation_verified")
    recomputation = vf.verify_recomputation(dsn, owner, repo_name,
                                            review["review_id"])
    tracker.complete("recomputation_verified", recomputation)
    write("metadata-recomputation-evidence.json", recomputation)

    # ---- GitHub reconciliation ---------------------------------------
    tracker.begin("github_reconciliation_verified")
    reconciliation = vf.verify_reconciliation(
        gh, installation_token(), REPO, pr["pr_number"], pr["head_sha"],
        expected_app_id, waiting["comment_id"], waiting["check_run_id"])
    tracker.complete("github_reconciliation_verified", reconciliation)
    write("github-publication-reconciliation.json", reconciliation)

    # ---- dashboard ----------------------------------------------------
    tracker.begin("dashboard_verified")
    dashboard = vf.verify_dashboard(token, review["review_id"],
                                    request["request_id"], primary["snapshot_id"])
    tracker.complete("dashboard_verified", dashboard)
    write("dashboard-metadata-lifecycle.json", dashboard)

    write("github-metadata-review-E2E.json", {
        "run_id": RUN, "release": os.environ.get("GITHUB_SHA"),
        "pull_request": pr, "review": review, "targeted_request": request,
        "waiting": waiting, "snapshot": primary, "recomputation": recomputation,
        "reconciliation": reconciliation, "dashboard": dashboard})

    # ---- variants A-G -------------------------------------------------
    variant_results = {}
    if run_variants:
        specs = [
            # A and E assert ALLOW, so their fixtures must be code-health
            # neutral. On the plain 'external'/'head_derived' shapes the code
            # review scores health 80 for a revenue-named addition, which the
            # policy puts in the WARN band before production metadata is even
            # considered - ALLOW was unreachable and the variant proved
            # nothing. See build_manifests.
            ("a", "variant_a_verified", "enforce", "external_clean",
             {"null_rate": vf.HEALTHY_NULL_RATE}, {"decision": "ALLOW",
                                                   "coverage": "COMPLETE"}),
            ("b", "variant_b_verified", "enforce", "external",
             {"exists": False}, {"decision": "BLOCK"}),
            ("c", "variant_c_verified", "enforce", "external", None, {}),
            ("d", "variant_d_verified", "enforce", "external",
             {"observed_at": datetime.now(timezone.utc) - timedelta(hours=6),
              "ttl_seconds": 900}, {"decision": "BLOCK",
                                    "coverage": "INCOMPLETE"}),
            ("e", "variant_e_verified", "enforce", "head_derived_clean",
             {"null_rate": vf.HEALTHY_NULL_RATE}, {"decision": "ALLOW"}),
        ]
        for letter, stage, vmode, manifest, snap, expect in specs:
            tracker.begin(stage)
            result = run_variant(letter.upper(), dsn=dsn, owner=owner,
                                 repo_name=repo_name, token=token, mode=vmode,
                                 manifest_variant=manifest, snapshot_kwargs=snap,
                                 expect=expect, since=since)
            tracker.complete(stage, result)
            variant_results[letter.upper()] = result
        # F and G were proven on the primary review by real submissions
        tracker.begin("variant_f_verified")
        tracker.complete("variant_f_verified", {
            "variant": "F",
            "one_effective_snapshot": True,
            "duplicate_status": tracker._stages["duplicate_snapshot_verified"]["proof"]["status"],
            "completed_jobs": recomputation["completed_jobs"],
            "final_attempts": recomputation["attempts"],
            "duplicate_publications": reconciliation["duplicate_comments"]})
        tracker.begin("variant_g_verified")
        tracker.complete("variant_g_verified", {
            "variant": "G",
            **tracker._stages["conflicting_replay_verified"]["proof"]})
        variant_results["F"] = tracker._stages["variant_f_verified"]["proof"]
        variant_results["G"] = tracker._stages["variant_g_verified"]["proof"]
    else:
        raise StageFailure("variants are mandatory; run_variants was disabled")
    write("metadata-variant-results.json", variant_results)

    # ---- cleanup, then the fail-closed assertion ----------------------
    result = cleanup("normal")
    if not result.get("cleanup_passed"):
        raise StageFailure(f"cleanup failed: {result.get('failures')}")

    tracker.assert_all_complete()
    print(f"\nall {len(REQUIRED_STAGES)} required stages executed and verified",
          flush=True)
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except StageIncomplete as exc:
        tracker.fail(tracker.current or "unknown", str(exc))
        print(f"STAGE INCOMPLETE: {exc}", flush=True)
        code = 1
    except Exception as exc:  # noqa: BLE001
        tracker.fail(tracker.current or "unknown", f"{type(exc).__name__}: {exc}")
        print(f"FAILED at stage {tracker.current}: {type(exc).__name__}: {exc}",
              flush=True)
        code = 1
    finally:
        outcome = cleanup("normal")
        if not outcome.get("cleanup_passed", False):
            code = code or 1
            if code == 0:
                code = 1
    raise SystemExit(code)
