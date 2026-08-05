"""Live-flow stage implementations for the genuine GitHub metadata-review E2E.

Every function here performs a real operation against a real system - a
subprocess, an HTTP call to the served API, or a GitHub API call - and returns
a proof mapping describing what was actually observed. None of them fabricate
success, and none are satisfied by writing an evidence file.

Kept separate from the driver so a static reachability test can assert that
each mandatory operation has a call path from main().
"""
from __future__ import annotations

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
        "import os, time, uvicorn;"
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
        " log_level='warning')")
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


def create_fixture_pr(state, gh, token, repo, run_id, variant="external",
                      enforcement_mode="enforce"):
    """Create a REAL unmerged pull request in the synthetic E2E repository."""
    base, head, changed = build_manifests(variant)

    status, repo_info = gh("GET", f"/repos/{repo}", token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read repository: HTTP {status}")
    default_branch = repo_info["default_branch"]

    status, ref = gh("GET", f"/repos/{repo}/git/ref/heads/{default_branch}",
                     token, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read default branch ref: HTTP {status}")
    base_sha = ref["object"]["sha"]

    branch = f"e2e/metadata-{variant}-{run_id}"

    def put_file(path, content, message, branch_name, parent_sha=None):
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

    # branch from the default branch, then commit the BASE manifest so the
    # base SHA carries the pre-change artifact
    st, _ = gh("POST", f"/repos/{repo}/git/refs", token,
               {"ref": f"refs/heads/{branch}", "sha": base_sha}, bearer=False)
    if st not in (200, 201):
        raise StageFailure(f"could not create branch: HTTP {st}")

    put_file("relium.yml",
             f"enabled: true\nenforcement_mode: {enforcement_mode}\n",
             f"e2e {run_id}: relium config", branch)
    real_base_sha = put_file(
        "target/manifest.json", json.dumps(base, indent=2),
        f"e2e {run_id}: base manifest", branch)
    real_head_sha = put_file(
        "target/manifest.json", json.dumps(head, indent=2),
        f"e2e {run_id}: head manifest introducing an external dependency", branch)

    st, pr = gh("POST", f"/repos/{repo}/pulls", token, {
        "title": f"[E2E FIXTURE - DO NOT MERGE] metadata review {variant} {run_id}",
        "head": branch, "base": default_branch,
        "body": ("Synthetic fixture for the Relium metadata-review E2E. "
                 "Never merge. Closed automatically during cleanup."),
        "draft": False}, bearer=False)
    if st not in (200, 201):
        raise StageFailure(f"could not open pull request: HTTP {st}")

    state["pr_number"] = pr["number"]
    state.setdefault("branches", []).append(branch)
    return {"pr_number": pr["number"], "branch": branch,
            "base_sha": real_base_sha, "head_sha": pr["head"]["sha"],
            "changed_models": changed, "merged": False,
            "enforcement_mode": enforcement_mode}
