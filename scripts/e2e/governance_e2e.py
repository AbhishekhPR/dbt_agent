"""Live GitHub proof for the review governance actions.

Proves ONE path end to end, against the real dedicated E2E App:

    POST /api/reviews/{id}/request-changes
      -> durable review_change_requests row (PENDING)
      -> outbox
      -> real lifecycle worker
      -> GitHub App installation token
      -> POST /repos/{owner}/{repo}/pulls/{n}/reviews  event=REQUEST_CHANGES
      -> GitHub returns a review id
      -> local row becomes PUBLISHED with that id

Reuses the existing harness for everything that already had a safe path:
the fixture PR, the tunnel, the webhook repoint/restore, and the API/worker
processes. Nothing here broadens a permission, and no credential is printed:
the App key is read from the path the workflow wrote it to, and the fixture
token is used only for fixture branch/PR operations, exactly as before.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf                                        # noqa: E402
import verify_flow as vf                                      # noqa: E402
from live_flow import ENVIRONMENT, StageFailure, local, poll  # noqa: E402
from metadata_review_e2e import (                             # noqa: E402
    app_jwt, cleanup, gh, installation_token, issue_token,
    preserve_webhook, state,
)

REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
OWNER, REPO_NAME = REPO.split("/", 1)
FIXTURE_TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")
RUN = uuid.uuid4().hex[:10]
EV = Path(sys.argv[1] if len(sys.argv) > 1 else "governance-evidence")
EV.mkdir(parents=True, exist_ok=True)

# Importing the metadata-review driver constructs its StageTracker as a side
# effect, which writes a stage-tracker.json reporting 0 of 27 stages. That file
# describes a run that did not happen here, and would read as a failure in this
# run's evidence, so it is removed.
_STRAY_TRACKER = EV / "stage-tracker.json"
if _STRAY_TRACKER.exists():
    _STRAY_TRACKER.unlink()

checks: list[dict] = []


def check(name, ok, detail=""):
    checks.append({"check": name, "passed": bool(ok), "detail": str(detail)[:300]})
    print(f'[{"ok" if ok else "XX"}] {name}  {str(detail)[:110]}', flush=True)
    if not ok:
        raise StageFailure(f"{name}: {detail}")
    return True


def write(name, doc):
    (EV / name).write_text(
        json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _store(dsn):
    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    return PostgresLifecycleStore(dsn)


def main() -> int:
    dsn = os.environ["RELIUM_DATABASE_URL"]
    mode = os.environ.get("RELIUM_ENFORCEMENT_MODE", "enforce")

    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda s, _f: (cleanup(f"signal-{s}"), sys.exit(130)))
        except (ValueError, OSError):
            pass

    expected_app_id = int(os.environ["RELIUM_GITHUB_APP_ID"])

    # ---- services, tunnel, webhook -----------------------------------
    storage = EV / f"relium-storage-{RUN}"
    storage.mkdir(parents=True, exist_ok=True)
    api_report = lf.start_api(
        state, str(REPO_ROOT), dsn, storage,
        os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"],
        os.environ["RELIUM_GITHUB_APP_ID"],
        os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"], EV / "api.log")
    check("api_started", api_report.get("healthz") == 200, api_report)
    lf.start_worker(state, str(REPO_ROOT), dsn, EV / "worker.log")
    lf.start_tunnel(state, EV / "tunnel.log")
    # start_tunnel returns a REPORT dict; the url is on state.
    tunnel_url = state["tunnel"]["url"]
    preserve_webhook()          # required before any repoint
    lf.point_webhook(state, gh, app_jwt, tunnel_url)
    lf.verify_webhook(gh, app_jwt, tunnel_url)
    check("services_started", True, tunnel_url)

    # ---- a genuine fixture pull request ------------------------------
    lf.assert_fixture_token_scope(gh, FIXTURE_TOKEN, REPO)
    pr = lf.create_fixture_pr(state, gh, FIXTURE_TOKEN, REPO, RUN,
                              variant="external", enforcement_mode=mode)
    check("fixture_pr_created", bool(pr["pr_number"]), f"PR #{pr['pr_number']}")

    review = vf.verify_postgres_review(dsn, OWNER, REPO_NAME,
                                       pr["head_sha"], pr["base_sha"])
    review_id = review["review_id"]
    check("review_created_by_genuine_webhook", True, review_id)

    request = vf.verify_targeted_request(dsn, OWNER, REPO_NAME, review_id)
    token = issue_token(dsn, OWNER, REPO_NAME)

    # Reach a decided state where requesting changes is appropriate. The
    # submitted evidence carries a null rate above policy, so the engine
    # decides WARN/BLOCK on its own - the harness never sets a decision.
    primary, _key, _body = vf.submit_primary_snapshot(token, review, request["request_id"])
    recomputation = vf.verify_recomputation(dsn, OWNER, REPO_NAME, review_id)
    decided = recomputation.get("decision")
    check("review_reached_a_decision", decided in ("WARN", "BLOCK"), decided)

    store = _store(dsn)
    try:
        current = store.get_review(OWNER, REPO_NAME, review_id)
        attempt_before = current["attempt"]
    finally:
        store.close()

    # ---- the action under test ---------------------------------------
    message = ("analytics.orders.discount_amount is above the configured null "
               "rate in production. Please confirm the backfill before merging.")
    status, response = local(
        "POST", f"/api/reviews/{review_id}/request-changes",
        {"message": message, "actor": "relium-e2e"},
        token=token, key=f"cr-{RUN}")
    check("request_changes_accepted", status == 202, f"HTTP {status} {response}")
    change_request_id = response["change_request_id"]
    check("record_starts_pending", response["state"] == "PENDING", response["state"])

    # ---- the worker submits it to GitHub -----------------------------
    def published():
        store = _store(dsn)
        try:
            row = store.get_change_request(OWNER, REPO_NAME, change_request_id)
            return row if row and row["state"] in ("PUBLISHED", "FAILED") else None
        finally:
            store.close()

    row = poll(published, timeout=240, interval=3,
               description="the worker to submit the review to GitHub")
    check("local_record_published", row["state"] == "PUBLISHED",
          f"state={row['state']} failure={row.get('failure_reason')}")
    remote_review_id = str(row["remote_review_id"])
    check("remote_review_id_persisted", bool(remote_review_id), remote_review_id)

    # ---- verify DIRECTLY against GitHub ------------------------------
    itoken = installation_token()
    _status, reviews = gh("GET", f"/repos/{REPO}/pulls/{pr['pr_number']}/reviews",
                          itoken, bearer=False)
    ours = [r for r in reviews if str(r.get("id")) == remote_review_id]
    check("github_has_the_review", len(ours) == 1,
          f"{len(ours)} match(es) of {len(reviews)} review(s)")
    remote = ours[0]

    check("github_state_is_changes_requested",
          remote.get("state") == "CHANGES_REQUESTED", remote.get("state"))
    author_app = (remote.get("user") or {}).get("login")
    check("authored_by_the_dedicated_app",
          str(author_app).startswith(os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")),
          f"author={author_app}")
    check("targets_the_correct_pull_request",
          str(remote.get("pull_request_url", "")).endswith(f"/pulls/{pr['pr_number']}"),
          f"{remote.get('pull_request_url')} (expected .../pulls/{pr['pr_number']})")
    check("ids_match", str(remote["id"]) == remote_review_id,
          f"github={remote['id']} local={remote_review_id}")

    # ---- idempotency: a second submit must not create a second review -
    status2, response2 = local(
        "POST", f"/api/reviews/{review_id}/request-changes",
        {"message": message, "actor": "relium-e2e"},
        token=token, key=f"cr-{RUN}-again")
    check("second_submit_is_not_a_new_request", status2 == 200,
          f"HTTP {status2} {response2.get('status')}")
    check("second_submit_returns_the_same_record",
          response2.get("change_request_id") == change_request_id,
          response2.get("change_request_id"))

    _s, reviews_after = gh("GET", f"/repos/{REPO}/pulls/{pr['pr_number']}/reviews",
                           installation_token(), bearer=False)
    app_reviews = [r for r in reviews_after
                   if (r.get("user") or {}).get("type") == "Bot"
                   and r.get("state") == "CHANGES_REQUESTED"]
    check("no_duplicate_github_review", len(app_reviews) == 1,
          f"{len(app_reviews)} request-changes review(s) by the App")

    # ---- a published request is never resubmitted by the worker ------
    from agent.metadata_evidence.change_request import submit_change_request

    store = _store(dsn)
    try:
        again = submit_change_request(
            store, organization_id=OWNER, repository_id=REPO_NAME,
            environment=ENVIRONMENT, change_request_id=change_request_id,
            publisher=_ExplodingPublisher())
    finally:
        store.close()
    check("published_request_is_not_resubmitted",
          again["status"] == "already_published", again["status"])

    # ---- failure semantics never fabricate success -------------------
    failure = _failure_semantics(dsn, review_id)
    check("github_failure_is_recorded_not_faked", failure["state"] == "FAILED",
          failure)

    # ---- publication surfaces stay distinct --------------------------
    store = _store(dsn)
    try:
        final = store.get_review(OWNER, REPO_NAME, review_id)
    finally:
        store.close()
    ids = {"comment_id": final.get("github_comment_id"),
           "check_run_id": final.get("github_check_run_id"),
           "request_changes_review_id": remote_review_id}
    distinct = len({str(v) for v in ids.values() if v}) == len([v for v in ids.values() if v])
    check("comment_check_and_review_ids_are_distinct", distinct, ids)

    # The store keeps these ids as TEXT; GitHub returns them as integers.
    # verify_reconciliation compares with !=, so passing the strings made a
    # correctly reconciled comment look replaced.
    def _as_int(value):
        return int(value) if value not in (None, "") else None

    reconciliation = vf.verify_reconciliation(
        gh, installation_token(), REPO, pr["pr_number"], pr["head_sha"],
        expected_app_id, _as_int(final.get("github_comment_id")),
        _as_int(final.get("github_check_run_id")))
    check("sticky_comment_reconciled_not_duplicated",
          reconciliation["duplicate_comments"] == 0, reconciliation)

    document = {
        "run_id": RUN,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "repository": REPO,
        "pull_request": pr["pr_number"],
        "pull_request_url": f"https://github.com/{REPO}/pull/{pr['pr_number']}",
        "review_id": review_id,
        "attempt_at_request": attempt_before,
        "decision_at_request": decided,
        "change_request_id": change_request_id,
        "remote_review_id": remote_review_id,
        "github_review_state": remote.get("state"),
        "github_review_url": remote.get("html_url"),
        "github_review_author": author_app,
        "local_transition": ["PENDING", "PUBLISHED"],
        "idempotency": {"second_submit_status": status2,
                        "app_request_changes_reviews": len(app_reviews)},
        "failure_semantics": failure,
        "publication_ids": ids,
        "reconciliation": reconciliation,
        "checks": checks,
    }
    write("governance-e2e.json", document)
    print(json.dumps({k: document[k] for k in
                      ("pull_request", "review_id", "change_request_id",
                       "remote_review_id", "github_review_state")}, indent=2))
    return 0


class _ExplodingPublisher:
    """Proves the worker does not call GitHub for a published request."""

    def submit_request_changes(self, **_kwargs):
        raise AssertionError("a published request was resubmitted to GitHub")


def _failure_semantics(dsn, review_id):
    """A genuine GitHub failure must leave FAILED, never a fabricated success."""
    from agent.metadata_evidence.change_request import (
        ChangeRequestError, submit_change_request,
    )

    store = _store(dsn)
    try:
        row, created = store.create_change_request(
            OWNER, REPO_NAME, ENVIRONMENT,
            change_request_id=f"cr-failure-{RUN}", review_id=review_id,
            attempt=999, pull_number=999999, head_sha="0" * 40,
            actor="relium-e2e", message="deliberate failure probe")

        class _Failing:
            def submit_request_changes(self, **_kwargs):
                raise RuntimeError("404 Not Found (deliberate probe)")

        try:
            submit_change_request(
                store, organization_id=OWNER, repository_id=REPO_NAME,
                environment=ENVIRONMENT,
                change_request_id=f"cr-failure-{RUN}", publisher=_Failing())
        except ChangeRequestError:
            pass
        final = store.get_change_request(OWNER, REPO_NAME, f"cr-failure-{RUN}")
        return {"state": final["state"], "failure_reason": final["failure_reason"],
                "remote_review_id": final["remote_review_id"]}
    finally:
        store.close()


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        # Print the traceback. The first run failed on a one-line
        # AttributeError with no location, which cost a whole run to place.
        import traceback
        detail = traceback.format_exc()
        print(f"GOVERNANCE E2E FAILED: {type(exc).__name__}: {exc}", flush=True)
        print(detail, flush=True)
        write("governance-e2e-failure.json",
              {"error": type(exc).__name__, "detail": str(exc)[:2000],
               "traceback": detail[-4000:], "checks": checks})
        code = 1
    raise SystemExit(code)
