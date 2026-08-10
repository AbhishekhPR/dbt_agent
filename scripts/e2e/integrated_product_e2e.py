"""Integrated product E2E: one review, the whole chain, remote phase A.

Every specialised driver in this directory proves one leg well. None of them
proves that the legs are the SAME review. This one does, and it does nothing
else: it is orchestration over the already-proven helpers, and it deliberately
owns no new product logic.

    baseline review + request  -> production observation A
    real fixture PR            -> genuine App webhook
    semantic Before/After      -> direct blast radius
    WAITING_FOR_METADATA       -> real targeted metadata request
    observation B              -> production metadata comparison A -> B
    worker recomputation       -> final decision / findings
    GitHub publication         -> durable PostgreSQL export

The browser leg is NOT run here. Phase B reads the exported database, so this
driver records what Phase B will need rather than asserting anything about a
frontend that is not present in this repository.

WHY THE FIXTURE IS THE `allow` SEMANTIC SHAPE

`semantic_fixtures.allow_fixture_files` changes a WHERE predicate in
`int_customer_orders`. It is the only proven fixture that produces genuine AST
evidence while leaving code health at 100 with zero code findings. That
matters here more than anywhere else: with the code side provably neutral, the
final decision is attributable to production metadata alone, so
"the comparison added no finding and no health penalty" becomes a measurable
claim rather than an argument. The BLOCK shape would confound it.

WHAT IS ASSERTED AND WHAT IS DERIVED

The expected direct blast radius is computed from the REAL parsed manifest,
never hardcoded, so the assertion cannot drift into asserting the harness's
own opinion. The expected final decision is the one the EXISTING deterministic
policy already establishes for a high production null rate on a required
column - the same expectation `verify_flow.verify_recomputation` encodes - and
it is declared explicitly so a different outcome fails loudly instead of
passing quietly.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf  # noqa: E402
import verify_flow as vf  # noqa: E402
from live_flow import StageFailure  # noqa: E402
from metadata_review_e2e import app_jwt, gh  # noqa: E402

import dbt_fixture_project as dfp  # noqa: E402
import semantic_fixtures as sf  # noqa: E402

REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
FIXTURE_TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")
RUN = os.environ.get("RELIUM_E2E_RUN_ID", uuid.uuid4().hex[:10])
EV = Path(sys.argv[1] if len(sys.argv) > 1 else "integrated-product-evidence")
CLEANUP_ONLY = "--cleanup-only" in sys.argv
EV.mkdir(parents=True, exist_ok=True)
RUN_RECOVERY = EV / "integrated-product-recovery.json"

# Importing the shared auth helper constructs the metadata driver's tracker in
# this evidence directory. It describes no run of this driver and must never be
# uploaded as if it did.
_STRAY_TRACKER = EV / "stage-tracker.json"
if _STRAY_TRACKER.exists():
    _STRAY_TRACKER.unlink()

OWNER, REPO_NAME = REPO.split("/", 1)
ENVIRONMENT = lf.ENVIRONMENT

#: The two reviews this run creates. Ordered: the baseline must exist before
#: the main review's observation can have anything to compare against.
BASELINE = "baseline"
MAIN = "main"

#: The existing deterministic policy expectation for a high production null
#: rate on a required column, with a code-neutral fixture. Declared, not
#: invented: it is the outcome `verify_flow.verify_recomputation` already
#: encodes for exactly this metadata condition.
EXPECTED_FINAL_DECISION = "WARN"
EXPECTED_PRODUCTION_FINDING = "column.high_null_rate"
EXPECTED_CODE_HEALTH = 100

#: Controlled bounded metadata. A -> B carries four truthful differences and
#: one signal that deliberately does NOT move, so Phase B can prove the
#: dashboard shows changes while the download carries the whole observation.
A_ROW_COUNT, B_ROW_COUNT = 1000, 800
A_CARDINALITY, B_CARDINALITY = 0.37, 0.42
UNCHANGED_SIGNAL = "schema_fingerprint"
UNCHANGED_FINGERPRINT = "fp-integrated-e2e"

#: B's null rate is the EXISTING policy fixture's value, imported rather than
#: restated. An illustrative 12% would sit under the 20% threshold, so
#: `column.high_null_rate` would never fire and the run would prove a decision
#: nobody's policy made. The constant moves if the policy fixture moves.
A_NULL_RATE = 0.01
B_NULL_RATE = vf.HIGH_NULL_RATE
NULL_RATE_THRESHOLD = vf.NULL_RATE_THRESHOLD

#: Both observations declare a one-hour TTL, so A only has to be far enough
#: back to order strictly before B - not far enough back to be STALE.
#:
#: Run 31394411123 backdated A by six hours against that same one-hour TTL.
#: `classify_freshness` correctly returned STALE, and the baseline review
#: recomputed to METADATA_STALE / BLOCK. The product was right; the fixture was
#: asking it to treat a six-hour-old observation as current. Baseline
#: SELECTION only needs strict precedence in
#: (observed_at, received_at, snapshot_id), which two minutes provides.
OBSERVATION_TTL_SECONDS = 3600
A_BACKDATE_SECONDS = 120

state = {"procs": [], "tunnel": None, "cleanup_done": False,
         "cleanup_result": None, "expected_slug": APP_SLUG, "mutated": False}

#: Indirection so fault-injection tests can substitute a recording adapter.
#: Production paths never reassign these.
GH = gh
APP_JWT = app_jwt


# --------------------------------------------------------------- evidence

def _write(name: str, document) -> None:
    (EV / name).write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")


def _initial_recovery() -> dict:
    return {"run_id": RUN, "repository": REPO, "branches": [], "pulls": [],
            "webhook_preserved": False, "original_webhook": None,
            "processes": [], "database_artifacts": [], "reviews": {}}


def _load_recovery() -> dict | None:
    if not RUN_RECOVERY.is_file():
        return None
    try:
        return json.loads(RUN_RECOVERY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_recovery(record: dict) -> None:
    """Durable before the mutating call, so a crash still leaves ownership."""
    RUN_RECOVERY.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")


def _record(**updates):
    record = _load_recovery() or _initial_recovery()
    record.update(updates)
    _write_recovery(record)
    return record


# ------------------------------------------------------------- webhook

def preserve_webhook() -> dict:
    """Capture the App webhook BEFORE anything can repoint it."""
    record = _load_recovery()
    if record is None:
        raise StageFailure("recovery record must exist before webhook preservation")
    status, hook = GH("GET", "/app/hook/config", APP_JWT())
    if status != 200:
        raise StageFailure(f"cannot preserve webhook: HTTP {status}")
    if "insecure_ssl" not in hook:
        raise StageFailure("webhook preservation returned no TLS configuration")
    original = {"url": hook.get("url"),
                "content_type": hook.get("content_type", "json"),
                "insecure_ssl": hook.get("insecure_ssl")}
    if not original["url"]:
        raise StageFailure("webhook preservation returned no URL")
    record["original_webhook"] = original
    record["webhook_preserved"] = True
    _write_recovery(record)
    # The secret is never read and never rewritten by this driver.
    return {"preserved": True, "secret_captured": False,
            "url_host": original["url"].split("//")[-1]}


def restore_webhook(record: dict) -> dict:
    original = record.get("original_webhook")
    if not record.get("webhook_preserved") or not isinstance(original, dict):
        return {"restored": False, "verified_through_github": False,
                "reason": "original webhook was not durably preserved"}
    jwt = APP_JWT()
    patch_status, _ = GH(
        "PATCH", "/app/hook/config", jwt,
        {"url": original.get("url"), "content_type": original.get("content_type"),
         "insecure_ssl": original.get("insecure_ssl")})
    get_status, confirmed = GH("GET", "/app/hook/config", jwt)
    url_matches = get_status == 200 and confirmed.get("url") == original.get("url")
    type_matches = (get_status == 200
                    and confirmed.get("content_type") == original.get("content_type"))
    ssl_matches = (get_status == 200
                   and confirmed.get("insecure_ssl") == original.get("insecure_ssl"))
    verified = patch_status == 200 and url_matches and type_matches and ssl_matches
    return {"restored": patch_status == 200, "patch_status": patch_status,
            "get_status": get_status, "url_matches_original": url_matches,
            "content_type_matches_original": type_matches,
            "insecure_ssl_matches_original": ssl_matches,
            "verified_through_github": verified, "secret_touched": False}


# ------------------------------------------------------- owned artifacts

def make_branch(branch: str, from_sha: str) -> None:
    """Create a ref, recording the intent BEFORE the call."""
    record = _load_recovery() or _initial_recovery()
    if branch not in record["branches"]:
        record["branches"].append(branch)
    _write_recovery(record)
    status, _ = GH("POST", f"/repos/{REPO}/git/refs", FIXTURE_TOKEN,
                   {"ref": f"refs/heads/{branch}", "sha": from_sha}, bearer=False)
    if status not in (200, 201):
        raise StageFailure(f"could not create branch {branch}: HTTP {status}")


def commit_file(branch: str, path: str, content: str, message: str) -> str:
    import base64

    body = {"message": message, "branch": branch,
            "content": base64.b64encode(content.encode()).decode()}
    status, existing = GH("GET", f"/repos/{REPO}/contents/{path}?ref={branch}",
                          FIXTURE_TOKEN, bearer=False)
    if status == 200 and isinstance(existing, dict):
        body["sha"] = existing["sha"]
    status, resp = GH("PUT", f"/repos/{REPO}/contents/{path}", FIXTURE_TOKEN, body,
                      bearer=False)
    if status not in (200, 201):
        raise StageFailure(f"could not write {path} on {branch}: HTTP {status}")
    return resp["commit"]["sha"]


def open_pull(head: str, base: str, title: str) -> dict:
    record = _load_recovery() or _initial_recovery()
    status, pr = GH("POST", f"/repos/{REPO}/pulls", FIXTURE_TOKEN,
                    {"title": title, "head": head, "base": base, "draft": False,
                     "body": ("Synthetic fixture for the Relium integrated "
                              "product E2E. Never merge. Closed and deleted "
                              "automatically during cleanup.")}, bearer=False)
    if status not in (200, 201):
        raise StageFailure(f"could not open pull request: HTTP {status}")
    record["pulls"].append(pr["number"])
    _write_recovery(record)
    return {"pr_number": pr["number"], "head_branch": head, "base_branch": base,
            "base_sha": pr["base"]["sha"], "head_sha": pr["head"]["sha"],
            "merged": False}


# ------------------------------------------------------------- cleanup

_REF_ABSENCE_ATTEMPTS = 6
_REF_ABSENCE_INTERVAL = 2.0
_TERM_GRACE_SECONDS = 10.0


def _ref_absent(branch: str) -> bool:
    """GitHub's read-after-delete is eventually consistent; retry the read."""
    import time

    for attempt in range(_REF_ABSENCE_ATTEMPTS):
        status, _ = GH("GET", f"/repos/{REPO}/git/ref/heads/{branch}",
                       FIXTURE_TOKEN, bearer=False)
        if status == 404:
            return True
        if attempt < _REF_ABSENCE_ATTEMPTS - 1:
            time.sleep(_REF_ABSENCE_INTERVAL)
    return False


def _close_owned_pulls(record: dict, result: dict) -> bool:
    ok = True
    closed = []
    for number in record.get("pulls") or []:
        status, pull = GH("GET", f"/repos/{REPO}/pulls/{number}", FIXTURE_TOKEN,
                          bearer=False)
        if status != 200:
            result.setdefault("failures", []).append(
                f"cannot read owned fixture PR #{number}: HTTP {status}")
            ok = False
            continue
        if pull.get("merged"):
            result.setdefault("failures", []).append(
                f"owned fixture PR #{number} was MERGED")
            ok = False
            continue
        if pull.get("state") != "closed":
            patch_status, _ = GH("PATCH", f"/repos/{REPO}/pulls/{number}",
                                 FIXTURE_TOKEN, {"state": "closed"}, bearer=False)
            if patch_status != 200:
                result.setdefault("failures", []).append(
                    f"could not close PR #{number}: HTTP {patch_status}")
                ok = False
                continue
        closed.append(number)
    result["pulls_closed_unmerged"] = closed
    return ok


def _delete_owned_branches(record: dict, result: dict) -> bool:
    ok = True
    deleted = []
    for branch in record.get("branches") or []:
        status, _ = GH("DELETE", f"/repos/{REPO}/git/refs/heads/{branch}",
                       FIXTURE_TOKEN, bearer=False)
        # A DELETE that reports anything unexpected is reconciled by re-reading
        # the exact ref rather than trusted or assumed failed.
        if status in (204, 404) or _ref_absent(branch):
            deleted.append(branch)
            continue
        result.setdefault("failures", []).append(
            f"branch {branch} still exists after DELETE (HTTP {status})")
        ok = False
    result["branches_deleted"] = deleted
    return ok


def _listener_up(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def register_process(label, proc, marker=None):
    """Record a child so cleanup stops it.

    `live_flow.start_api` appends to `state["procs"]` itself; `start_tunnel`
    does NOT - it stores `{"proc": ..., "url": ...}` under `state["tunnel"]`
    and reports the process only through this callback. Run 31392463042 tried
    to stop the tunnel by calling `.poll()` on that dict, which is not a
    process, so cleanup raised inside atexit. Registering it here puts the
    tunnel through the same SIGTERM/grace/SIGKILL/reap path as everything else.
    """
    state["procs"].append((label, proc))
    return {"label": label, "pid": getattr(proc, "pid", None), "marker": marker}


def tunnel_url(state_mapping):
    """The public URL `start_tunnel` recorded, as a string.

    `start_tunnel` RETURNS a proof mapping and PUBLISHES the URL into
    `state["tunnel"]["url"]`. Run 31392463042 passed the returned mapping
    straight into `point_webhook`, which does `tunnel_url.rstrip("/")` - a
    dict has no `rstrip`, so the run died before the webhook was ever
    repointed.
    """
    tunnel = state_mapping.get("tunnel")
    if not isinstance(tunnel, dict):
        raise StageFailure("no tunnel was started")
    url = tunnel.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StageFailure(f"tunnel URL is {url!r}, not a public https URL")
    return url


def _stop_processes(result: dict) -> bool:
    """SIGTERM, grace, SIGKILL, then reap. A zombie is not a live process."""
    import time

    ok = True
    stopped = []
    for label, proc in state.get("procs") or []:
        if proc is None:
            continue
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            deadline = time.monotonic() + _TERM_GRACE_SECONDS
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.25)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            proc.wait(timeout=5)          # reap, so no zombie is counted alive
        except Exception:                 # noqa: BLE001
            ok = False
            result.setdefault("failures", []).append(f"{label} did not exit")
        stopped.append({"label": label, "returncode": proc.returncode})
    result["processes_stopped"] = stopped
    return ok


def cleanup(reason: str = "normal") -> dict:
    """Mandatory, idempotent, and safe to run after any StageFailure."""
    if state.get("cleanup_done"):
        return state.get("cleanup_result") or {"reason": reason, "repeat": True}
    state["cleanup_done"] = True
    result = {"reason": reason, "failures": []}
    record = _load_recovery() or _initial_recovery()

    # Webhook first: a stranded webhook is the only failure that keeps
    # affecting the App after this run ends.
    if state.get("mutated") or record.get("webhook_preserved"):
        result["webhook"] = restore_webhook(record)
        if not result["webhook"].get("verified_through_github"):
            result["failures"].append("webhook was not verifiably restored")

    if not _close_owned_pulls(record, result):
        result["failures"].append("owned pull request closure incomplete")
    if not _delete_owned_branches(record, result):
        result["failures"].append("owned branch deletion incomplete")
    if not _stop_processes(result):
        result["failures"].append("process shutdown incomplete")

    for port in (lf.PORT,):
        if _listener_up(port):
            result["failures"].append(f"listener still up on {port}")
    result["listeners_clear"] = not any(
        f.startswith("listener still up") for f in result["failures"])
    result["passed"] = not result["failures"]
    state["cleanup_result"] = result
    _write("integrated-product-cleanup.json", result)
    return result


def _arm_cleanup() -> None:
    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda value, _frame: (
                cleanup(f"signal-{value}"), sys.exit(130)))
        except (ValueError, OSError):
            pass


# --------------------------------------------------------- environment gate

def environment_gate() -> dict:
    """Refuse to run unless this is unambiguously the dedicated E2E App."""
    status, app = GH("GET", "/app", APP_JWT())
    if status != 200:
        raise StageFailure(f"cannot read the App: HTTP {status}")
    slug = app.get("slug", "")
    if slug != APP_SLUG or "pilot" in slug.lower():
        raise StageFailure(f"refusing to run against app slug {slug!r}")
    if not FIXTURE_TOKEN:
        raise StageFailure("fixture token is not configured")
    scope = lf.assert_fixture_token_scope(GH, FIXTURE_TOKEN, REPO)
    return {"app_slug": slug, "app_id": app.get("id"), "fixture_scope": scope}


def readiness_gate(api_proof: dict) -> dict:
    """Migration currency by /readyz semantics, never a pinned version list.

    A pinned list is a maintenance trap: it fails the moment a legitimate
    migration lands, for a reason unrelated to whatever the run is proving.
    """
    if api_proof.get("migrations") != "current":
        raise StageFailure(f"migrations are {api_proof.get('migrations')!r}, not current")
    if api_proof.get("database") != "ok":
        raise StageFailure(f"database check is {api_proof.get('database')!r}")
    if api_proof.get("review_lifecycle") != "postgresql":
        raise StageFailure(
            f"review lifecycle is {api_proof.get('review_lifecycle')!r}")
    return {"migrations": "current", "pinned_version_list_used": False,
            "database": "ok", "review_lifecycle": "postgresql"}


# ------------------------------------------------------ blast radius expectation

def expected_direct_downstream(manifest: dict, changed_model: str) -> dict:
    """Direct consumers of the changed model, from the REAL parsed manifest.

    Derived rather than declared: a hardcoded list would make the assertion an
    opinion of this harness instead of a property of the project.

    The identity is the dbt NODE ID, not the model name. `collection_plan`
    stores `downstream.add(node_id)`, and the proven blast-radius E2E compares
    against `direct_model_ids` for the same reason. Run 31394411123 failed here
    with `['model.relium_e2e_dbt.dim_customers'] != ['dim_customers']` - the
    set was right and only this harness's identity format was wrong.
    """
    nodes = manifest.get("nodes") or {}
    changed_ids = [nid for nid, node in nodes.items()
                   if node.get("name") == changed_model
                   and node.get("resource_type") == "model"]
    if len(changed_ids) != 1:
        raise StageFailure(
            f"manifest has {len(changed_ids)} models named {changed_model}")
    changed_id = changed_ids[0]
    direct_ids = sorted(
        nid for nid, node in nodes.items()
        if node.get("resource_type") == "model"
        and changed_id in ((node.get("depends_on") or {}).get("nodes") or []))
    if not direct_ids:
        raise StageFailure(
            f"{changed_model} has no direct downstream model; the fixture "
            f"cannot exercise blast radius")
    return {"changed_model": changed_model, "changed_model_id": changed_id,
            "direct_downstream_models": direct_ids,
            "direct_downstream_names": [nodes[n].get("name") for n in direct_ids],
            "identity": "dbt node id, as collection_plan persists it",
            "derived_from": "real dbt-parsed manifest"}


# --------------------------------------------------------------- assertions

#: The exact semantic change the allow fixture is built to produce. Pinned
#: rather than merely "non-empty": `allow_fixture_files` inserts a predicate
#: into the WHERE clause of int_customer_orders, and `_filter_changes` emits
#: scope="where" for that. Requiring only non-emptiness would pass on a
#: fixture that had silently started proving something else.
REQUIRED_SEMANTIC_CHANGE = {"kind": "filter_changed",
                            "model_name": sf.ALLOW_MUTATED_MODELS[0],
                            "scope": "where"}


def assert_semantic(row: dict) -> dict:
    """The proven filter_changed evidence, attributed only to the edited model.

    Additional truthful evidence about the SAME model is allowed - the engine
    may legitimately observe more than one thing about a real SQL change - but
    the required change must be present, and nothing may be attributed to a
    model the fixture never touched.
    """
    evidence = row.get("semantic_evidence")
    if not isinstance(evidence, dict):
        raise StageFailure("attempt recorded no semantic evidence")
    if evidence.get("status") not in ("evaluated", "partial"):
        raise StageFailure(f"semantic status is {evidence.get('status')!r}")

    changes = []
    for model in (evidence.get("models") or []):
        for change in (model.get("changes") or []):
            changes.append({**change,
                            "model_name": change.get("model_name")
                            or model.get("model_name")})
    if not changes:
        raise StageFailure("semantic evidence is empty; the fixture proved nothing")

    matched = [c for c in changes
               if all(c.get(k) == v for k, v in REQUIRED_SEMANTIC_CHANGE.items())]
    if not matched:
        raise StageFailure(
            f"required semantic change {REQUIRED_SEMANTIC_CHANGE} is absent; "
            f"observed {[{k: c.get(k) for k in ('kind', 'model_name', 'scope')} for c in changes]}")

    models = {c.get("model_name") for c in changes}
    unexplained = sorted(m for m in models if m not in sf.ALLOW_MUTATED_MODELS)
    if unexplained:
        raise StageFailure(f"semantic changes about untouched models: {unexplained}")
    return {"status": evidence.get("status"), "change_count": len(changes),
            "change_kinds": sorted({c.get("kind") for c in changes}),
            "models": sorted(m for m in models if m),
            "required_change": dict(REQUIRED_SEMANTIC_CHANGE),
            "required_change_present": True}


def assert_blast_radius(plan: dict, expectation: dict) -> dict:
    """Exactly the direct consumers; no transitive or exposure expansion."""
    persisted = sorted(plan.get("downstream_models") or [])
    expected = sorted(expectation["direct_downstream_models"])
    if persisted != expected:
        raise StageFailure(
            f"direct blast radius is {persisted}, expected {expected}")
    return {"direct_downstream_models": persisted,
            "direct_downstream_names": expectation.get("direct_downstream_names"),
            "transitive_expansion": False, "exposure_expansion": False,
            "matches_parsed_manifest": True}


def assert_metadata_request(request: dict) -> dict:
    """Bounded, targeted, and never a request for rows or arbitrary SQL."""
    if not request.get("request_id"):
        raise StageFailure("no collection request exists for this review")
    if not request.get("bounded"):
        raise StageFailure("collection request is not bounded")
    signals = set(request.get("required_signals") or [])
    forbidden = {"raw_rows", "sample_rows", "sql", "query", "arbitrary_sql"}
    leaked = sorted(signals & forbidden)
    if leaked:
        raise StageFailure(f"request asks for prohibited signals: {leaked}")
    return {"request_id": request["request_id"],
            "relations": request.get("relations"),
            "columns": request.get("columns"),
            "required_signals": sorted(signals),
            "bounded": True, "raw_row_request": False,
            "arbitrary_sql_request": False}


def assert_comparison(comparison: dict, snapshot_a: str, snapshot_b: str) -> dict:
    """Exactly A -> B, with the deterministic deltas the fixture controls."""
    if not isinstance(comparison, dict):
        raise StageFailure("attempt recorded no metadata comparison")
    if comparison.get("status") != "evaluated":
        raise StageFailure(f"comparison status is {comparison.get('status')!r}")
    if comparison.get("baseline_snapshot_id") != snapshot_a:
        raise StageFailure(
            f"baseline is {comparison.get('baseline_snapshot_id')!r}, not A {snapshot_a!r}")
    if comparison.get("current_snapshot_id") != snapshot_b:
        raise StageFailure(
            f"current is {comparison.get('current_snapshot_id')!r}, not B {snapshot_b!r}")

    by_signal = {}
    for change in comparison.get("changes") or []:
        by_signal.setdefault(change.get("signal"), []).append(change)

    def one(signal):
        found = by_signal.get(signal) or []
        if len(found) != 1:
            raise StageFailure(
                f"expected exactly one {signal} change, got {len(found)}")
        return found[0]

    rows = one("row_count")
    if (rows.get("before"), rows.get("after")) != (A_ROW_COUNT, B_ROW_COUNT):
        raise StageFailure(f"row_count {rows.get('before')} -> {rows.get('after')}")
    if rows.get("absolute_delta") != B_ROW_COUNT - A_ROW_COUNT:
        raise StageFailure(f"row_count absolute delta {rows.get('absolute_delta')}")

    nulls = one("null_rate")
    points = round((B_NULL_RATE - A_NULL_RATE) * 100, 6)
    if abs((nulls.get("percentage_point_delta") or 0) - points) > 1e-6:
        raise StageFailure(
            f"null_rate points {nulls.get('percentage_point_delta')}, expected {points}")

    cardinality = one("cardinality")
    card_points = round((B_CARDINALITY - A_CARDINALITY) * 100, 6)
    if abs((cardinality.get("percentage_point_delta") or 0) - card_points) > 1e-6:
        raise StageFailure(
            f"cardinality points {cardinality.get('percentage_point_delta')}")

    column = one("column_exists")
    if column.get("before") is not True or column.get("after") is not False:
        raise StageFailure(
            f"column existence {column.get('before')} -> {column.get('after')}")

    if UNCHANGED_SIGNAL in by_signal:
        raise StageFailure(
            f"{UNCHANGED_SIGNAL} did not move but was reported as a change")

    return {"status": comparison["status"],
            "baseline_snapshot_id": snapshot_a, "current_snapshot_id": snapshot_b,
            "baseline_observed_at": comparison.get("baseline_observed_at"),
            "current_observed_at": comparison.get("current_observed_at"),
            "change_count": len(comparison.get("changes") or []),
            "signals_changed": sorted(by_signal),
            "unchanged_signal_for_phase_b": UNCHANGED_SIGNAL,
            "unchanged_signal_value": UNCHANGED_FINGERPRINT,
            "row_count": [A_ROW_COUNT, B_ROW_COUNT],
            "null_rate_percentage_points": points,
            "cardinality_percentage_points": card_points,
            "column_present_then_missing": True}


def assert_comparison_is_evidence_only(waiting: dict, final: dict) -> dict:
    """The comparison must produce no finding and reach no policy input.

    An earlier version of this proved the wrong thing. It required
    `final health == waiting health`, which passes UNCONDITIONALLY: the policy
    contract in agent/evidence_policy.py states that policy "cannot manufacture
    a finding or subtract health", and `_coverage` passes `code_health`
    straight through, so health never moves for ANY metadata finding -
    including the `column.high_null_rate` this fixture deliberately produces.
    The equality therefore held whether or not the comparison had contributed
    a finding, and proved nothing about the comparison at all.

    What actually isolates the comparison is two facts:

      * no persisted finding is attributable to it, and
      * it is not an INPUT to the decision - `recompute_review` computes it
        after `evaluate_metadata_decision` has already returned, and never
        passes it in.

    Health is still recorded, but as the existing policy expectation for this
    fixture (`semantic_diff_e2e.assert_allow_expectations` pins the allow
    shape at 100) rather than as a second formula invented here.
    """
    findings = (final.get("payload") or {}).get("findings", [])
    codes = {f.get("code") for f in findings}
    categories = {f.get("category") for f in findings}
    comparison_shaped = sorted(
        c for c in codes
        if isinstance(c, str) and (
            "comparison" in c or "drift" in c or "metadata_change" in c))
    if comparison_shaped:
        raise StageFailure(
            f"the comparison produced policy findings: {comparison_shaped}")
    bad_categories = sorted(
        c for c in categories
        if isinstance(c, str) and (
            "comparison" in c or "drift" in c or "metadata_change" in c))
    if bad_categories:
        raise StageFailure(
            f"findings were categorised as comparison-derived: {bad_categories}")
    if final.get("health") != EXPECTED_CODE_HEALTH:
        raise StageFailure(
            f"health is {final.get('health')!r}, expected "
            f"{EXPECTED_CODE_HEALTH} from the existing policy contract "
            f"(code health passed through; evidence never subtracts health)")
    return {"finding_codes": sorted(c for c in codes if c),
            "comparison_findings": 0,
            "comparison_derived_categories": 0,
            "health": final.get("health"),
            "health_source": "code health, passed through by evidence policy",
            "health_before_metadata": waiting.get("health"),
            "policy_contract": (
                "agent/evidence_policy.py: policy cannot manufacture a finding "
                "or subtract health"),
            "equality_with_waiting_health_used_as_proof": False}


def assert_comparison_is_not_a_policy_input() -> dict:
    """The comparison cannot reach the decision, structurally.

    This is the assertion the health equality was pretending to be. It reads
    the real recomputation path and requires that the decision is produced
    BEFORE the comparison is computed, and that the comparison is never handed
    to the policy engine. A source-level check because that ordering is the
    guarantee - no runtime value can demonstrate the absence of an input.
    """
    source = (REPO_ROOT / "agent" / "metadata_evidence"
              / "recompute.py").read_text(encoding="utf-8")
    decision_at = source.index("decision = evaluate_metadata_decision(")
    comparison_at = source.index("metadata_comparison = compute_comparison(")
    if decision_at > comparison_at:
        raise StageFailure(
            "the comparison is computed before the decision; it could be an input")
    call_end = source.index(")", source.index("(", decision_at))
    decision_call = source[decision_at:call_end]
    for forbidden in ("comparison", "metadata_comparison"):
        if forbidden in decision_call:
            raise StageFailure(
                f"the decision call references {forbidden!r}; the comparison "
                f"is feeding policy")
    return {"decision_computed_before_comparison": True,
            "comparison_passed_to_policy": False,
            "checked": "agent/metadata_evidence/recompute.py"}


def assert_final_decision(review: dict, final: dict) -> dict:
    """The existing deterministic policy outcome, asserted exactly."""
    if review.get("decision") is None:
        raise StageFailure("review never reached a decision")
    if review["decision"] != EXPECTED_FINAL_DECISION:
        raise StageFailure(
            f"decision is {review['decision']!r}, expected "
            f"{EXPECTED_FINAL_DECISION!r} from the existing policy")
    if review.get("lifecycle_state") not in ("DECISION_READY", "PUBLISHED"):
        raise StageFailure(f"lifecycle is {review.get('lifecycle_state')!r}")
    if review.get("evidence_coverage") != "COMPLETE":
        raise StageFailure(f"coverage is {review.get('evidence_coverage')!r}")
    findings = (final.get("payload") or {}).get("findings", [])
    codes = {f.get("code") for f in findings}
    if EXPECTED_PRODUCTION_FINDING not in codes:
        raise StageFailure(
            f"expected {EXPECTED_PRODUCTION_FINDING}, got {sorted(codes)}")
    code_findings = [f for f in findings if f.get("category") == "code"]
    if code_findings:
        raise StageFailure(f"expected zero code findings, got {len(code_findings)}")
    return {"decision": review["decision"], "health": review.get("health"),
            "coverage": review.get("evidence_coverage"),
            "lifecycle_state": review.get("lifecycle_state"),
            "attempt": final.get("attempt"),
            "snapshot_id": final.get("snapshot_id"),
            "production_finding": EXPECTED_PRODUCTION_FINDING,
            "code_findings": 0}


# ------------------------------------------------- observations A and B

def observation_body(review, request_id, *, row_count, null_rate, cardinality,
                     column_exists, observed_at):
    """One controlled production observation, in the collector's own contract.

    Bounded aggregates and catalogue facts only. No raw row is transmitted,
    and a column the collector cannot find carries NO metrics - reporting a
    null rate for a column that is not there would be inventing a measurement,
    which is exactly the shape the comparison engine refuses to compare.
    """
    columns = [{"column_name": "order_id", "data_type": "bigint",
                "exists": True, "null_rate": 0.0, "is_nullable": False}]
    if column_exists:
        columns.append({"column_name": "discount_amount", "data_type": "numeric",
                        "exists": True, "is_nullable": True,
                        "null_rate": null_rate,
                        "distinct_count": int(row_count * cardinality),
                        "cardinality": cardinality})
    else:
        columns.append({"column_name": "discount_amount", "exists": False,
                        "data_type": None})
    return {
        "review_id": review["review_id"], "request_id": request_id,
        "environment": ENVIRONMENT, "attempt": review["attempt"],
        "completeness": "COMPLETE", "ttl_seconds": OBSERVATION_TTL_SECONDS,
        "observed_at": observed_at.isoformat(),
        "base_sha": review["base_sha"], "head_sha": review["head_sha"],
        "base_manifest_hash": review["base_manifest_hash"],
        "head_manifest_hash": review["head_manifest_hash"],
        "collector_version": "integrated-e2e", "adapter_type": "postgres",
        "relations": [{
            "relation_name": "raw.orders", "relation_schema": "raw",
            "exists_in_production": True,
            # Deliberately identical in A and B: Phase B proves this reaches
            # the downloadable observation without becoming a dashboard card.
            "schema_fingerprint": UNCHANGED_FINGERPRINT,
            "row_count": row_count, "columns": columns}],
    }


def submit_observation(token, body, label):
    """Ingest through the real public collector API. Never a direct INSERT."""
    key = f"integrated-{label}-{uuid.uuid4().hex[:12]}"
    status, resp = lf.local("POST", "/api/metadata-snapshots", body,
                            token=token, key=key)
    if status != 202:
        raise StageFailure(f"{label} observation rejected: HTTP {status} {resp}")
    snapshot_id = resp.get("snapshot_id")
    if not snapshot_id:
        raise StageFailure(f"{label} observation returned no snapshot id")
    return {"snapshot_id": snapshot_id, "status": status,
            "idempotency_key": key,
            "recomputation_queued": bool(resp.get("recomputation_queued")),
            "observed_at": body["observed_at"],
            "ingest_path": "POST /api/metadata-snapshots",
            "direct_insert_used": False}


def assert_observation_immutable(dsn, snapshot_id):
    """The stored observation refuses mutation at the database boundary."""
    store = _store(dsn)
    try:
        before = store.get_snapshot(OWNER, REPO_NAME, snapshot_id)
        if before is None:
            raise StageFailure(f"snapshot {snapshot_id} is not durable")
        refused = []
        for sql in (
            "UPDATE metadata_snapshots SET completeness='FAILED' "
            "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s",
            "UPDATE snapshot_relations SET row_count=1 "
            "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s",
            "UPDATE snapshot_columns SET null_rate=0.99 "
            "WHERE organization_id=%s AND repository_id=%s AND snapshot_id=%s",
        ):
            accepted = False
            try:
                store.connection.execute(sql, (OWNER, REPO_NAME, snapshot_id))
                accepted = True
            except Exception as exc:                     # noqa: BLE001
                refused.append("immutable" in str(exc))
            finally:
                store.connection.rollback()
            if accepted:
                raise StageFailure(f"the database accepted a mutation: {sql[:40]}")
        after = store.get_snapshot(OWNER, REPO_NAME, snapshot_id)
        if after != before:
            raise StageFailure("snapshot changed despite refused mutations")
        return {"snapshot_id": snapshot_id, "mutations_refused": len(refused),
                "all_refused_as_immutable": all(refused), "unchanged": True}
    finally:
        store.close()


# ----------------------------------------------------------- store reads

def _store(dsn):
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    return PostgresLifecycleStore(dsn)


def resolve_review(dsn, base_sha, head_sha):
    """The review for THIS pull request, by base/head provenance only."""
    def found():
        store = _store(dsn)
        try:
            rows = store.connection.execute(
                "SELECT review_id FROM reviews WHERE organization_id=%s AND "
                "repository_id=%s AND base_sha=%s AND head_sha=%s",
                (OWNER, REPO_NAME, base_sha, head_sha)).fetchall()
            if len(rows) != 1:
                return None
            return store.get_review(OWNER, REPO_NAME, rows[0]["review_id"])
        finally:
            store.close()

    return lf.poll(found, timeout=240, interval=3,
                   description=f"review for head {head_sha[:8]}")


def wait_for_waiting(dsn, review_id):
    def waiting():
        store = _store(dsn)
        try:
            review = store.get_review(OWNER, REPO_NAME, review_id)
            if review and review["lifecycle_state"] == "WAITING_FOR_METADATA":
                return review
            return None
        finally:
            store.close()

    return lf.poll(waiting, timeout=240, interval=3,
                   description="WAITING_FOR_METADATA")


def attempt_row(dsn, review_id, attempt):
    store = _store(dsn)
    try:
        for row in store.review_attempts(OWNER, REPO_NAME, review_id):
            if row["attempt"] == attempt:
                return row
        return None
    finally:
        store.close()


def wait_for_comparison(dsn, review_id, after_attempt):
    """The first attempt after the waiting one that records a comparison."""
    def recorded():
        store = _store(dsn)
        try:
            for row in store.review_attempts(OWNER, REPO_NAME, review_id):
                if (row["attempt"] > after_attempt
                        and isinstance(row.get("metadata_comparison"), dict)):
                    return row
            return None
        finally:
            store.close()

    return lf.poll(recorded, timeout=300, interval=4,
                   description="a durable metadata comparison")


def wait_for_decision(dsn, review_id):
    def decided():
        store = _store(dsn)
        try:
            review = store.get_review(OWNER, REPO_NAME, review_id)
            if review and review["decision"] is not None:
                return review
            return None
        finally:
            store.close()

    return lf.poll(decided, timeout=300, interval=4,
                   description="the worker to reach a final decision")


# --------------------------------------------------------------- one review

def open_review_pull(label, main_sha, main_files, marker):
    """One real fixture PR carrying the proven code-neutral SQL mutation."""
    base_branch = f"e2e/integrated-{label}-base-{RUN}"
    head_branch = f"e2e/integrated-{label}-head-{RUN}"
    head_files = sf.allow_fixture_files(main_files, marker=marker)
    changed = [p for p in sorted(head_files) if main_files.get(p) != head_files[p]]
    if not changed:
        raise StageFailure(f"{label}: fixture mutated nothing")

    make_branch(base_branch, main_sha)
    commit_file(base_branch, "relium.yml", sf.relium_config(),
                f"integrated e2e {RUN}: {label} relium config")
    base_manifest = dfp.parse_manifest(
        dict(main_files), prefix=f"relium-integrated-{label}-base-")
    base_tip = commit_file(base_branch, "target/manifest.json",
                           json.dumps(base_manifest, indent=2),
                           f"integrated e2e {RUN}: {label} base manifest")

    make_branch(head_branch, base_tip)
    for path in changed:
        commit_file(head_branch, path, head_files[path],
                    f"integrated e2e {RUN}: {label} head {path}")
    head_manifest = dfp.parse_manifest(
        dict(head_files), prefix=f"relium-integrated-{label}-head-")
    commit_file(head_branch, "target/manifest.json",
                json.dumps(head_manifest, indent=2),
                f"integrated e2e {RUN}: {label} head manifest")

    pull = open_pull(head_branch, base_branch,
                     f"[E2E FIXTURE - DO NOT MERGE] integrated {label} {RUN}")
    pull["changed_files"] = changed
    pull["head_manifest"] = head_manifest
    return pull


def main() -> int:
    _record()                       # ownership exists before anything mutates
    _arm_cleanup()
    if CLEANUP_ONLY:
        result = cleanup("cleanup-only")
        return 0 if result.get("passed") else 1

    dsn = os.environ["RELIUM_DATABASE_URL"]
    summary = {"run_id": RUN, "repository": REPO, "phase": "A"}

    gate = environment_gate()
    summary["environment_gate"] = gate
    _write("integrated-environment-gate.json", gate)

    # --- real system ------------------------------------------------------
    workdir = REPO_ROOT
    storage_root = EV / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    api = lf.start_api(state, workdir, dsn, storage_root,
                       os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"],
                       os.environ["RELIUM_GITHUB_APP_ID"],
                       os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"],
                       EV / "api.log")
    summary["readiness"] = readiness_gate(api)
    summary["worker"] = lf.start_worker(state, workdir, dsn, EV / "worker.log")

    # --- webhook: preserve BEFORE the tunnel can repoint it ---------------
    summary["webhook_preserved"] = preserve_webhook()
    summary["tunnel"] = lf.start_tunnel(state, EV / "tunnel.log",
                                        on_start=register_process)
    public_url = tunnel_url(state)
    summary["webhook_pointed"] = lf.point_webhook(state, GH, APP_JWT, public_url)
    summary["webhook_verified"] = lf.verify_webhook(GH, APP_JWT, public_url)

    main_sha, main_files = _fixture_main()
    summary["fixture_commit"] = main_sha

    token = _issue_collector_token(dsn)

    # --- BASELINE review: the only legitimate way to create observation A --
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    baseline_pull = open_review_pull(BASELINE, main_sha, main_files,
                                     f"{RUN}-baseline")
    _poll_correlated_delivery(since, baseline_pull["pr_number"],
                              baseline_pull["head_sha"])
    baseline_review = resolve_review(dsn, baseline_pull["base_sha"],
                                     baseline_pull["head_sha"])
    baseline_review = wait_for_waiting(dsn, baseline_review["review_id"])
    baseline_request = vf.verify_targeted_request(
        dsn, OWNER, REPO_NAME, baseline_review["review_id"])
    observation_a = submit_observation(
        token,
        observation_body(baseline_review, baseline_request["request_id"],
                         row_count=A_ROW_COUNT, null_rate=A_NULL_RATE,
                         cardinality=A_CARDINALITY, column_exists=True,
                         observed_at=datetime.now(timezone.utc)
                         - timedelta(seconds=A_BACKDATE_SECONDS)),
        "a")
    summary["baseline_review"] = {
        "review_id": baseline_review["review_id"],
        "pr_number": baseline_pull["pr_number"],
        "request_id": baseline_request["request_id"],
        "provenance": "genuine PR -> webhook -> review -> targeted request"}
    summary["observation_a"] = observation_a
    summary["observation_a_immutable"] = assert_observation_immutable(
        dsn, observation_a["snapshot_id"])

    # --- MAIN review ------------------------------------------------------
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    pull = open_review_pull(MAIN, main_sha, main_files, f"{RUN}-main")
    summary["pull_request"] = {k: pull[k] for k in
                               ("pr_number", "base_sha", "head_sha",
                                "base_branch", "head_branch", "merged")}
    summary["delivery"] = _poll_correlated_delivery(
        since, pull["pr_number"], pull["head_sha"])

    review = resolve_review(dsn, pull["base_sha"], pull["head_sha"])
    review = wait_for_waiting(dsn, review["review_id"])
    review_id = review["review_id"]
    waiting_attempt = int(review["attempt"])
    waiting_row = attempt_row(dsn, review_id, waiting_attempt)
    if waiting_row is None:
        raise StageFailure("the waiting attempt was not persisted")

    summary["review"] = {"review_id": review_id, "attempt": waiting_attempt,
                         "lifecycle_state": review["lifecycle_state"],
                         "decision": review["decision"]}
    summary["semantic"] = assert_semantic(waiting_row)
    expectation = expected_direct_downstream(pull["head_manifest"],
                                             sf.ALLOW_MUTATED_MODELS[0])
    summary["blast_radius_expectation"] = expectation
    plan = (review.get("payload") or {}).get("plan") or {}
    summary["blast_radius"] = assert_blast_radius(plan, expectation)
    request = vf.verify_targeted_request(dsn, OWNER, REPO_NAME, review_id)
    summary["metadata_request"] = assert_metadata_request(request)

    # --- observation B ----------------------------------------------------
    observation_b = submit_observation(
        token,
        observation_body(review, request["request_id"],
                         row_count=B_ROW_COUNT, null_rate=B_NULL_RATE,
                         cardinality=B_CARDINALITY, column_exists=False,
                         observed_at=datetime.now(timezone.utc)),
        "b")
    summary["observation_b"] = observation_b
    summary["observation_b_immutable"] = assert_observation_immutable(
        dsn, observation_b["snapshot_id"])

    # --- comparison, recomputation, decision ------------------------------
    compared = wait_for_comparison(dsn, review_id, waiting_attempt)
    summary["metadata_comparison"] = assert_comparison(
        compared["metadata_comparison"], observation_a["snapshot_id"],
        observation_b["snapshot_id"])

    final_review = wait_for_decision(dsn, review_id)
    final_row = attempt_row(dsn, review_id, int(final_review["attempt"]))
    if final_row is None:
        raise StageFailure("the final attempt was not persisted")
    summary["final_decision"] = assert_final_decision(final_review, final_row)
    summary["comparison_is_evidence_only"] = assert_comparison_is_evidence_only(
        waiting_row, final_row)
    summary["comparison_not_a_policy_input"] = (
        assert_comparison_is_not_a_policy_input())

    # The comparison must still name A and B on the attempt that decided.
    if isinstance(final_row.get("metadata_comparison"), dict):
        summary["final_attempt_comparison"] = assert_comparison(
            final_row["metadata_comparison"], observation_a["snapshot_id"],
            observation_b["snapshot_id"])

    # --- publication ------------------------------------------------------
    summary["publication"] = _verify_publication(
        dsn, review_id, pull["pr_number"], final_review["head_sha"],
        gate["app_id"])

    summary["phase_b_inputs"] = {
        "review_id": review_id, "attempt": int(final_review["attempt"]),
        "baseline_snapshot_id": observation_a["snapshot_id"],
        "current_snapshot_id": observation_b["snapshot_id"],
        "evidence_export_path":
            f"/api/reviews/{review_id}/attempts/{final_review['attempt']}"
            f"/metadata-evidence.json",
        "unchanged_signal": UNCHANGED_SIGNAL,
        "unchanged_signal_value": UNCHANGED_FINGERPRINT}

    # Written only after every product assertion above has passed.
    _write("integrated-product-summary.json", summary)
    result = cleanup("normal")
    if not result.get("passed"):
        raise StageFailure(f"cleanup failed: {result.get('failures')}")
    return 0


def _fixture_main():
    status, ref = GH("GET", f"/repos/{REPO}/git/ref/heads/main", FIXTURE_TOKEN,
                     bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture main: HTTP {status}")
    sha = ref["object"]["sha"]
    files = dfp.read_fixture_project(GH, REPO, FIXTURE_TOKEN, sha,
                                     required_models=sf.ALLOW_MUTATED_MODELS)
    return sha, files


def _issue_collector_token(dsn):
    from metadata_review_e2e import issue_token

    return issue_token(dsn, OWNER, REPO_NAME)


def _poll_correlated_delivery(since_utc, pr_number, head_sha):
    """Reuse the proven payload-correlated delivery selector.

    Selecting by position is what run 31360954740 proved wrong: with two pull
    requests in one run the window still holds the earlier one's delivery. This
    run always has two, so payload correlation is mandatory, not optional.
    """
    from semantic_diff_e2e import _poll_for_correlated_delivery

    return _poll_for_correlated_delivery(since_utc, pr_number, head_sha)


def _verify_publication(dsn, review_id, pr_number, head_sha, expected_app_id):
    """The existing publication contract: one sticky comment, one check run."""
    store = _store(dsn)
    try:
        review = store.get_review(OWNER, REPO_NAME, review_id)
    finally:
        store.close()

    def reconciled():
        status, comments = GH("GET", f"/repos/{REPO}/issues/{pr_number}/comments",
                              FIXTURE_TOKEN, bearer=False)
        if status != 200:
            return None
        owned = [c for c in comments
                 if (c.get("performed_via_github_app") or {}).get("id")
                 == expected_app_id]
        if len(owned) != 1:
            return None
        check_status, checks = GH(
            "GET", f"/repos/{REPO}/commits/{head_sha}/check-runs",
            FIXTURE_TOKEN, bearer=False)
        if check_status != 200:
            return None
        runs = [c for c in checks.get("check_runs", [])
                if (c.get("app") or {}).get("id") == expected_app_id]
        if len(runs) != 1 or runs[0].get("status") != "completed":
            return None
        return {"comment": owned[0], "check": runs[0]}

    published = lf.poll(reconciled, timeout=300, interval=5,
                        description="the App to publish the final review")
    return {"comment_id": published["comment"]["id"],
            "check_run_id": published["check"]["id"],
            "check_conclusion": published["check"].get("conclusion"),
            "duplicate_comments": 0, "duplicate_checks": 0,
            "stored_comment_id": review.get("github_comment_id"),
            "stored_check_run_id": review.get("github_check_run_id"),
            "bound_to_review": review_id}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageFailure as exc:
        # Cleanup is mandatory after ANY stage failure, not only a clean exit.
        outcome = cleanup(f"stage-failure: {exc}")
        _write("integrated-product-failure.json",
               {"error": str(exc), "cleanup": outcome})
        print(f"INTEGRATED PRODUCT E2E FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
