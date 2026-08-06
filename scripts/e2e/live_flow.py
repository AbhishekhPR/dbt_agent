"""Live-flow stage implementations for the genuine GitHub metadata-review E2E.

Every function here performs a real operation against a real system - a
subprocess, an HTTP call to the served API, or a GitHub API call - and returns
a proof mapping describing what was actually observed. None of them fabricate
success, and none are satisfied by writing an evidence file.

Kept separate from the driver so a static reachability test can assert that
each mandatory operation has a call path from main().
"""
from __future__ import annotations

import sys
from pathlib import Path

# The repository root must be importable for `agent...` imports.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

PORT = 8791
BASE_URL = f"http://127.0.0.1:{PORT}"
ENVIRONMENT = "production"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class StageFailure(RuntimeError):
    """A live stage did not achieve its required outcome."""


# --------------------------------------------------------------- HTTP
def local(method, path, body=None, token=None, key=None, raw=None, headers=None,
          timeout=45):
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if key:
        req.add_header("Idempotency-Key", key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, (json.loads(payload) if payload else {})
        except json.JSONDecodeError:
            return exc.code, {}


def poll(fn, *, timeout=180, interval=3, description="condition"):
    """Poll a real system until fn() returns a truthy value."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    raise StageFailure(f"timed out waiting for {description}; last={str(last)[:200]}")


# ------------------------------------------------------- process startup
def start_api(state, workdir, dsn, storage_root, webhook_secret, app_id, key_path,
              log_path):
    """Start the REAL application through build_application under uvicorn."""
    launcher = (
        # Run 7 produced an EMPTY api.log because uvicorn ran at warning level
        # and the application's own loggers had no handler. A silent skip
        # inside the review path was therefore invisible and cost a full run
        # to diagnose. The application must be able to speak.
        "import os, time, logging, uvicorn;"
        "logging.basicConfig(level=logging.INFO,"
        " format='%(levelname)s %(name)s %(message)s');"
        "from agent.github_app.server import build_application;"
        "from agent.github_app.settings import load_settings;"
        "s=load_settings({"
        "'RELIUM_GITHUB_APP_ID': os.environ['RELIUM_GITHUB_APP_ID'],"
        "'RELIUM_GITHUB_WEBHOOK_SECRET': os.environ['RELIUM_GITHUB_WEBHOOK_SECRET'],"
        "'RELIUM_GITHUB_PRIVATE_KEY_PATH': os.environ['RELIUM_GITHUB_PRIVATE_KEY_PATH'],"
        "'RELIUM_STORAGE_ROOT': os.environ['RELIUM_STORAGE_ROOT'],"
        "'RELIUM_DATABASE_URL': os.environ['RELIUM_DATABASE_URL'],"
        f"'RELIUM_PORT': '{PORT}', 'RELIUM_HOST': '127.0.0.1',"
        "'RELIUM_WORKER_COUNT': '2'});"
        f"uvicorn.run(build_application(s), host='127.0.0.1', port={PORT},"
        " log_level='info')")
    env = {**os.environ,
           "RELIUM_DATABASE_URL": dsn,
           "RELIUM_STORAGE_ROOT": str(storage_root),
           "RELIUM_GITHUB_WEBHOOK_SECRET": webhook_secret,
           "RELIUM_GITHUB_APP_ID": app_id,
           "RELIUM_GITHUB_PRIVATE_KEY_PATH": key_path,
           "PYTHONPATH": str(workdir)}
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", launcher], cwd=str(workdir),
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    state["procs"].append(("api", proc))

    def healthy():
        if proc.poll() is not None:
            raise StageFailure(f"api exited early rc={proc.returncode}")
        return local("GET", "/healthz", timeout=10)[0] == 200

    poll(healthy, timeout=120, interval=2, description="api /healthz")
    status, readiness = local("GET", "/readyz", timeout=20)
    checks = readiness.get("checks", {})
    if checks.get("review_lifecycle") != "postgresql":
        raise StageFailure(f"review_lifecycle is {checks.get('review_lifecycle')}")
    if checks.get("database") != "ok" or checks.get("migrations") != "current":
        raise StageFailure(f"readiness degraded: {checks}")
    unauth = local("GET", "/api/collection-requests", timeout=15)[0]
    if unauth != 401:
        raise StageFailure(f"protected route returned {unauth}, expected 401")
    return {"pid": proc.pid, "healthz": 200, "readyz_status": status,
            "review_lifecycle": checks.get("review_lifecycle"),
            "database": checks.get("database"), "migrations": checks.get("migrations"),
            "unauthenticated_protected_route": unauth}


def start_worker(state, workdir, dsn, log_path):
    """Start the REAL lifecycle/metadata worker subprocess."""
    from agent.worker.lifecycle_worker import registry
    if "metadata.review_recompute_requested" not in registry.supported():
        raise StageFailure("worker does not support metadata.review_recompute_requested")
    env = {**os.environ, "RELIUM_DATABASE_URL": dsn, "PYTHONPATH": str(workdir)}
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.worker.lifecycle_worker", "--poll-seconds", "2"],
        cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT)
    state["procs"].append(("worker", proc))
    time.sleep(5)
    if proc.poll() is not None:
        raise StageFailure(f"worker exited early rc={proc.returncode}")
    return {"pid": proc.pid, "alive": True,
            "supported_events": sorted(registry.supported())}


def start_tunnel(state, log_path):
    """Start a temporary Cloudflare quick tunnel and capture its public URL."""
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate", "--url", BASE_URL],
        stdout=log, stderr=subprocess.STDOUT)

    def url():
        if proc.poll() is not None:
            raise StageFailure(f"cloudflared exited rc={proc.returncode}")
        text = open(log_path, encoding="utf-8", errors="replace").read()
        match = TUNNEL_RE.search(text)
        return match.group(0) if match else None

    public = poll(url, timeout=120, interval=2, description="tunnel URL")
    state["tunnel"] = {"proc": proc, "url": public}
    return {"pid": proc.pid, "url_host": public.split("//")[1], "scheme": "https"}


# ------------------------------------------------------------- webhook
def point_webhook(state, gh, app_jwt, tunnel_url):
    """Repoint ONLY the dedicated E2E App webhook at the temporary tunnel.

    The mutation flag is set BEFORE the call so cleanup restores even when the
    call fails partway.
    """
    jwt = app_jwt()
    status, app = gh("GET", "/app", jwt)
    slug = app.get("slug", "")
    if status != 200 or slug != state["expected_slug"] or "pilot" in slug.lower():
        raise StageFailure(f"refusing to modify webhook for app slug {slug!r}")
    target = tunnel_url.rstrip("/") + "/github/webhook"
    state["mutated"] = True          # set BEFORE the mutating call
    patch_status, _ = gh("PATCH", "/app/hook/config", jwt,
                         {"url": target, "content_type": "json"})
    if patch_status != 200:
        raise StageFailure(f"webhook update failed HTTP {patch_status}")
    return {"app_slug": slug, "patch_status": patch_status,
            "target_host": target.split("//")[1].split("/")[0],
            "secret_read_or_rewritten": False}


def verify_webhook(gh, app_jwt, tunnel_url):
    jwt = app_jwt()
    status, hook = gh("GET", "/app/hook/config", jwt)
    hook.pop("secret", None)
    expected = tunnel_url.rstrip("/") + "/github/webhook"
    if status != 200 or hook.get("url") != expected:
        raise StageFailure("webhook configuration did not verify through GitHub")
    return {"verified_through_github": True,
            "content_type": hook.get("content_type"),
            "matches_intended_target": True}


# ---------------------------------------------------------- fixture PR
def _model(name, deps, columns, schema="analytics"):
    return {"resource_type": "model", "name": name, "schema": schema,
            "alias": name, "database": "warehouse",
            "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in columns},
            "original_file_path": f"models/{name}.sql"}


def build_manifests(variant="external"):
    """Base and head dbt manifests for the scenario.

    'external'     - head newly references raw.orders.discount_amount, which no
                     model in the PR creates.
    'head_derived' - an upstream head model creates the column a downstream
                     head model consumes (variant E).
    """
    sources = {"source.a.raw.orders": {
        "schema": "raw", "name": "orders", "database": "warehouse",
        "columns": {"order_id": {}, "discount_amount": {}}}}
    if variant == "head_derived":
        base = {"nodes": {"model.a.fct_orders": _model(
            "fct_orders", ["source.a.raw.orders"], ["order_id"])},
            "sources": sources}
        head = {"nodes": {
            "model.a.stg_orders": _model("stg_orders", ["source.a.raw.orders"],
                                         ["order_id", "margin_amount"]),
            "model.a.fct_orders": _model("fct_orders", ["model.a.stg_orders"],
                                         ["order_id", "margin_amount"])},
            "sources": sources}
        return base, head, ["stg_orders", "fct_orders"]
    base = {"nodes": {"model.a.fct_orders": _model(
        "fct_orders", ["source.a.raw.orders"], ["order_id"])}, "sources": sources}
    head = {"nodes": {"model.a.fct_orders": _model(
        "fct_orders", ["source.a.raw.orders"], ["order_id", "net_revenue"])},
        "sources": sources}
    return base, head, ["fct_orders"]


def assert_fixture_token_scope(gh, fixture_token, repo):
    """Fail closed unless the token's PRIVATE repository reach is exactly `repo`.

    Two earlier versions of this check were wrong, and the second was actively
    misleading:

      * counting every visible repository treats public repositories as
        "reach". A fine-grained PAT retains read access to public
        repositories, so that count says nothing about the selected-repository
        grant;
      * reading permissions.push/admin from GET /repos on a PUBLIC repository
        cannot establish the token's private grant either. GET /repos is
        publicly accessible, and the permissions object there reflects the
        resource owner's underlying access. Using it produced E2E-SEC-01, an
        ambiguous result that was NOT evidence of an over-privileged token.

    The selected-repository grant is observable in exactly one place: the set
    of PRIVATE repositories the token can see. Write capability is not probed
    synthetically here - it is proven later by the real run-scoped fixture
    branch creation, which either succeeds or fails the run.
    """
    private_seen = []
    page = 1
    while page <= 5:
        status, listing = gh("GET", f"/user/repos?per_page=100&page={page}",
                             fixture_token, bearer=False)
        if status != 200 or not isinstance(listing, list) or not listing:
            break
        private_seen.extend(r["full_name"] for r in listing if r.get("private"))
        if len(listing) < 100:
            break
        page += 1

    private_set = sorted(set(private_seen))
    if private_set != [repo]:
        extra = [n for n in private_set if n != repo]
        if extra:
            raise StageFailure(
                f"fixture token reaches {len(extra)} unrelated PRIVATE "
                f"repositories: {', '.join(extra)}")
        raise StageFailure(
            f"fixture token cannot see the target private repository {repo}; "
            f"private set was {private_set}")

    return {"target_private_repository": repo,
            "private_repositories_visible": private_set,
            "public_repositories_ignored": True,
            "scope_basis": "private-repository visibility only",
            "write_capability_proof": ("deferred to the real fixture branch "
                                       "creation; not probed synthetically"),
            "public_repo_permissions_used": False,
            "used_only_for": ["branch creation", "file commits",
                              "pull request creation", "pull request closure",
                              "fixture branch deletion"],
            "never_used_for": ["review execution", "webhook management",
                               "comments", "check runs", "App authentication",
                               "Relium APIs", "dashboard APIs"]}


def _model_files(manifest):
    """The dbt model .sql files a manifest describes.

    Run 7 failed here. The review path derives changed models by matching
    changed FILE PATHS against each node's original_file_path
    (load_changed_models_from_manifest). The old fixture committed only
    relium.yml and target/manifest.json, so no model file ever changed, the
    reviewer raised "At least one changed model is required.", and the runner
    published a NEUTRAL skip and returned BEFORE the lifecycle ever ran - no
    review row was possible. The application was right; the fixture simply was
    not a dbt model change.

    The body is derived from the node's columns, so a column difference
    between base and head is a genuine file modification.
    """
    files = {}
    for node in (manifest.get("nodes") or {}).values():
        columns = list(node.get("columns") or {})
        deps = list((node.get("depends_on") or {}).get("nodes") or [])
        if deps and not deps[0].startswith("source."):
            frm = "{{ ref('" + deps[0].split(".")[-1] + "') }}"
        else:
            frm = "{{ source('raw', 'orders') }}"
        body = ["-- Relium E2E fixture model. Generated; never merged.",
                "select"]
        body.append("    " + ",\n    ".join(columns))
        body.append("from " + frm)
        files[node["original_file_path"]] = "\n".join(body) + "\n"
    return files


def create_fixture_pr(state, gh, token, repo, run_id, variant="external",
                      enforcement_mode="enforce"):
    """Create a REAL unmerged pull request in the synthetic E2E repository.

    The pull request is opened from a head branch against a BASE BRANCH, not
    against the default branch. That matters: the runner reads the base
    manifest at pull_request.base.sha. Opening against the default branch
    binds the review to a tree with no manifest at all, so the scenario would
    be evaluated against nothing.

    ``token`` here is the FIXTURE token, never the App installation token: the
    App deliberately holds contents:read and must not gain write access. This
    call is also the write-capability proof for the scope assertion - it
    performs a real write or the run fails.
    """
    base, head, changed = build_manifests(variant)

    status, repo_info = gh("GET", f"/repos/{repo}", token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read repository: HTTP {status}")
    default_branch = repo_info["default_branch"]

    status, ref = gh("GET", f"/repos/{repo}/git/ref/heads/{default_branch}",
                     token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read default branch ref: HTTP {status}")
    default_sha = ref["object"]["sha"]

    base_branch = f"e2e/base-{variant}-{run_id}"
    head_branch = f"e2e/head-{variant}-{run_id}"

    def put_file(path, content, message, branch_name):
        body = {"message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch_name}
        existing_status, existing = gh(
            "GET", f"/repos/{repo}/contents/{path}?ref={branch_name}", token,
            bearer=False)
        if existing_status == 200 and isinstance(existing, dict):
            body["sha"] = existing["sha"]
        st, resp = gh("PUT", f"/repos/{repo}/contents/{path}", token, body,
                      bearer=False)
        if st not in (200, 201):
            raise StageFailure(f"could not write {path}: HTTP {st}")
        return resp["commit"]["sha"]

    def make_branch(name, from_sha):
        st, _ = gh("POST", f"/repos/{repo}/git/refs", token,
                   {"ref": f"refs/heads/{name}", "sha": from_sha}, bearer=False)
        if st not in (200, 201):
            raise StageFailure(f"could not create branch {name}: HTTP {st}")
        state.setdefault("branches", []).append(name)

    # --- base branch: the pre-change project, complete and self-consistent
    make_branch(base_branch, default_sha)
    put_file("relium.yml",
             f"enabled: true\nenforcement_mode: {enforcement_mode}\n",
             f"e2e {run_id}: relium config", base_branch)
    base_files = _model_files(base)
    for path in sorted(base_files):
        put_file(path, base_files[path], f"e2e {run_id}: base model {path}",
                 base_branch)
    base_tip = put_file("target/manifest.json", json.dumps(base, indent=2),
                        f"e2e {run_id}: base manifest", base_branch)

    # --- head branch: the proposed change, branched from the base branch
    make_branch(head_branch, base_tip)
    head_files = _model_files(head)
    changed_paths = [p for p in sorted(head_files)
                     if base_files.get(p) != head_files[p]]
    if not changed_paths:
        raise StageFailure(
            f"variant {variant} changes no dbt model file - the application "
            f"would correctly skip it and no review could exist")
    for path in changed_paths:
        put_file(path, head_files[path], f"e2e {run_id}: head model {path}",
                 head_branch)
    put_file("target/manifest.json", json.dumps(head, indent=2),
             f"e2e {run_id}: head manifest", head_branch)

    st, pr = gh("POST", f"/repos/{repo}/pulls", token, {
        "title": f"[E2E FIXTURE - DO NOT MERGE] metadata review {variant} {run_id}",
        "head": head_branch, "base": base_branch,
        "body": ("Synthetic fixture for the Relium metadata-review E2E. "
                 "Never merge. Closed and deleted automatically during cleanup."),
        "draft": False}, bearer=False)
    if st not in (200, 201):
        raise StageFailure(f"could not open pull request: HTTP {st}")

    state["pr_number"] = pr["number"]
    # Read the SHAs back from GitHub rather than inferring them: these are the
    # exact values the webhook payload will carry.
    return {"pr_number": pr["number"], "branch": head_branch,
            "base_branch": base_branch,
            "base_sha": pr["base"]["sha"], "head_sha": pr["head"]["sha"],
            "changed_models": changed, "changed_model_files": changed_paths,
            "merged": False, "enforcement_mode": enforcement_mode,
            "write_capability_proved": True}
