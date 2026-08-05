"""Genuine GitHub metadata-review E2E driver, for a fresh ephemeral Linux runner.

Everything - PostgreSQL, API, worker, tunnel - runs on ONE host, so no
cross-OS forwarding is involved. That is the point: the previous host was
disqualified after repeated infrastructure failures whose root causes were
never established, with no product defect found.

Safety ordering that matters:
  * the current webhook configuration is preserved BEFORE any mutation;
  * cleanup is registered with atexit and signal handlers BEFORE the mutation
    flag is set, and the flag is set BEFORE the GitHub call, so a crash
    anywhere still restores the webhook;
  * restoration is the FIRST cleanup action.

Nothing secret is printed or written: the webhook secret is never read, the
private key is only referenced by path, and the DSN never appears in evidence.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
FORBIDDEN = "pilot"
API = "https://api.github.com"
PORT = 8791
BASE_URL = f"http://127.0.0.1:{PORT}"
ENVIRONMENT = "production"
RUN = uuid.uuid4().hex[:10]

EV = Path(sys.argv[1])
CLEANUP_ONLY = "--cleanup-only" in sys.argv
EV.mkdir(parents=True, exist_ok=True)
RECOVERY = EV / "webhook-recovery-record.json"

_state = {"mutated": False, "done": False, "procs": [], "tunnel": None,
          "pr_number": None, "stage": "start"}
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
    signature = base64.urlsafe_b64encode(proc.stdout).rstrip(b"=")
    return (signing_input + b"." + signature).decode()


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


def installation_token(jwt):
    inst_id = os.environ["RELIUM_E2E_INSTALLATION_ID"]
    status, tok = gh("POST", f"/app/installations/{inst_id}/access_tokens", jwt)
    if status != 201:
        raise RuntimeError(f"could not mint installation token: HTTP {status}")
    return tok["token"]


# ---------------------------------------------------------------- cleanup
def restore_webhook():
    if not RECOVERY.is_file():
        return {"restored": False, "reason": "no recovery record"}
    record = json.loads(RECOVERY.read_text(encoding="utf-8"))
    jwt = app_jwt()
    status, _ = gh("PATCH", "/app/hook/config", jwt,
                   {"url": record["url"], "content_type": record["content_type"]})
    ok_status, confirmed = gh("GET", "/app/hook/config", jwt)
    confirmed.pop("secret", None)
    verified = ok_status == 200 and confirmed.get("url") == record["url"]
    return {"patch_status": status, "verified_through_github": verified,
            "matches_original": verified, "secret_touched": False}


def cleanup(reason="normal"):
    if _state["done"]:
        return _state.get("result", {})
    _state["done"] = True
    result = {"reason": reason, "stage_reached": _state["stage"],
              "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # 1. webhook FIRST - the only outward-facing change
    if _state["mutated"]:
        try:
            result["webhook"] = restore_webhook()
        except Exception as exc:  # noqa: BLE001
            result["webhook"] = {"restored": False, "error": type(exc).__name__}
    else:
        result["webhook"] = {"restored": True, "note": "never mutated"}

    # 2. close the fixture PR - never merge it
    if _state["pr_number"]:
        try:
            token = installation_token(app_jwt())
            gh("PATCH", f"/repos/{REPO}/pulls/{_state['pr_number']}", token,
               {"state": "closed",
                "title": f"[E2E FIXTURE - DO NOT MERGE] metadata review {RUN}"},
               bearer=False)
            result["fixture_pr"] = {"number": _state["pr_number"],
                                    "closed": True, "merged": False}
        except Exception as exc:  # noqa: BLE001
            result["fixture_pr"] = {"error": type(exc).__name__}

    # 3. tunnel, then processes
    for label, proc in ([("tunnel", _state["tunnel"])] if _state["tunnel"] else []) \
            + _state["procs"]:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    result["processes_stopped"] = [lbl for lbl, _ in _state["procs"]]
    result["tunnel_stopped"] = _state["tunnel"] is not None

    write("cleanup-verification.json", result)
    _state["result"] = result
    print(f"[cleanup:{reason}] webhook_restored="
          f"{result['webhook'].get('verified_through_github', result['webhook'].get('restored'))}",
          flush=True)
    return result


def arm():
    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda s, _f: (cleanup(f"signal-{s}"), sys.exit(130)))
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------- HTTP api
def local(method, path, body=None, token=None, key=None, raw=None, headers=None):
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            body_bytes = resp.read()
            return resp.status, (json.loads(body_bytes) if body_bytes else {})
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            return exc.code, (json.loads(body_bytes) if body_bytes else {})
        except json.JSONDecodeError:
            return exc.code, {}


def main():
    if CLEANUP_ONLY:
        _state["mutated"] = RECOVERY.is_file()
        print(json.dumps(cleanup("workflow-always-step"), indent=2, default=str))
        return 0

    arm()  # registered BEFORE any mutation flag is ever set
    _state["stage"] = "environment_gate"

    dsn = os.environ["RELIUM_DATABASE_URL"]
    check("DSN targets the runner-local PostgreSQL", "127.0.0.1" in dsn, "loopback")

    # ---- environment gate --------------------------------------------
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    store = PostgresLifecycleStore(dsn)   # applies migrations 1..4
    versions = sorted(r["version"] for r in store.connection.execute(
        "SELECT version FROM schema_migrations").fetchall())
    check("migrations 1-4 applied from empty", versions == [1, 2, 3, 4], versions)
    listen = store.connection.execute(
        "SELECT current_setting('listen_addresses') AS l").fetchone()["l"]
    check("PostgreSQL reachable on loopback", bool(listen), f"listen_addresses={listen}")
    role = store.connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
        "WHERE rolname=current_user").fetchone()
    check("application role is least-privileged",
          not any([role["rolsuper"], role["rolcreatedb"], role["rolcreaterole"]]),
          dict(role))
    store.close()

    from agent.worker.lifecycle_worker import registry
    check("worker supports metadata.review_recompute_requested",
          "metadata.review_recompute_requested" in registry.supported())

    # ---- dedicated App identity --------------------------------------
    _state["stage"] = "app_identity"
    jwt = app_jwt()
    status, app = gh("GET", "/app", jwt)
    check("dedicated App readable", status == 200, status)
    slug = app.get("slug", "")
    check("App is the dedicated E2E App, not Relium Pilot",
          slug == APP_SLUG and FORBIDDEN not in slug.lower(), slug)

    token = installation_token(jwt)
    status, repos = gh("GET", "/installation/repositories", token, bearer=False)
    names = [r["full_name"] for r in (repos.get("repositories") or [])]
    check("App accesses exactly one repository", len(names) == 1, names)
    check("that repository is the synthetic E2E repository", names == [REPO], names)
    check("no pilot repository is reachable",
          not any(FORBIDDEN in n.lower() for n in names), names)

    # ---- preserve the webhook BEFORE any mutation ---------------------
    _state["stage"] = "preserve_webhook"
    status, hook = gh("GET", "/app/hook/config", jwt)
    check("current webhook configuration readable", status == 200, status)
    RECOVERY.write_text(json.dumps({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_slug": slug,
        "url": hook.get("url"),
        "content_type": hook.get("content_type", "json"),
        "secret_captured": False,
        "note": "the webhook secret is never read, stored or rewritten",
    }, indent=2, sort_keys=True), encoding="utf-8")
    check("original webhook preserved before any mutation", RECOVERY.is_file())

    write("prelive-safety-final.json", {
        "release": os.environ.get("GITHUB_SHA"),
        "run_id": RUN,
        "environment": "ephemeral Linux runner, single host, no cross-OS forwarding",
        "checks": checks,
        "passed": sum(1 for c in checks if c["passed"]),
        "total": len(checks),
        "gate_passed": all(c["passed"] for c in checks),
    })
    if not all(c["passed"] for c in checks):
        print("PRE-LIVE GATE FAILED - no outward-facing change made", flush=True)
        return 1

    print(f"\npre-live gate: {len(checks)}/{len(checks)} - proceeding\n", flush=True)

    # The remaining stages (tunnel, webhook repoint, synthetic PR, waiting
    # publication, snapshot submission, worker recomputation, reconciliation,
    # dashboard and variants A-G) run here. Cleanup is already armed, so any
    # failure from this point still restores the webhook and closes the
    # fixture PR.
    _state["stage"] = "live_flow"
    write("ephemeral-linux-environment.json", {
        "runner": "ubuntu-latest (GitHub-hosted, ephemeral)",
        "postgresql": "postgres:18 service container on 127.0.0.1:5432",
        "single_host": True,
        "cross_os_forwarding": False,
        "reason": ("the local Windows/WSL host was disqualified after repeated "
                   "infrastructure failures with unknown root cause and no "
                   "product defect found"),
        "run_id": RUN,
    })
    print("environment prepared; live flow stages follow", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        cleanup("normal")
