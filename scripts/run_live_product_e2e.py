"""Live product E2E: a warehouse that moves, a review that follows it.

    fixture warehouse changes
      -> relium collector measures them
      -> public metadata snapshot API
      -> PostgreSQL lifecycle store
      -> lifecycle worker recomputes
      -> decision / coverage / health change
      -> GitHub + Slack republication reconciled
      -> dashboard APIs
      -> relium-app

The producer touches ONLY the fixture warehouse, as a role with no privilege
on the Relium database. Every review, decision, attempt, snapshot and
publication in the evidence bundle was created by the product.

Usage:
    python scripts/run_live_product_e2e.py up          # start services, create review
    python scripts/run_live_product_e2e.py phase 1     # advance one phase
    python scripts/run_live_product_e2e.py status
    python scripts/run_live_product_e2e.py down        # cleanup, always safe to repeat
    python scripts/run_live_product_e2e.py all         # every phase, unattended
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "live_e2e"))

import fixture_warehouse as fw                                  # noqa: E402
import roles                                                    # noqa: E402

ORG = "relium-e2e"
REPO = "relium-e2e-dbt"
OWNER = "AbhishekhPR"
ENVIRONMENT = "production"
API_PORT = 8099
API_URL = f"http://127.0.0.1:{API_PORT}"
APP_PORT = 5180

STATE_DIR = REPO_ROOT / ".live-e2e"
EVIDENCE = REPO_ROOT / "live-product-evidence"
STATE_FILE = STATE_DIR / "state.json"

PHASES = [
    ("0", "baseline", "seed", "Healthy production data"),
    ("1", "growth", "growth", "Valid rows appended"),
    ("1b", "growth", "growth", "More valid rows appended"),
    ("2", "null_decay", "null_decay", "Critical column NULL rate crosses policy"),
    ("3", "drop_column", "drop_column", "Required production column removed"),
    ("4", "recovery", "recover", "Column restored and backfilled"),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message):
    print(f"[{now()}] {message}", flush=True)


# --------------------------------------------------------------- state

def read_state():
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def write_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, default=str),
                          encoding="utf-8")


def write_evidence(name, doc):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    return path


# ------------------------------------------------------------ http helper

def api_get(path, token):
    request = urllib.request.Request(
        f"{API_URL}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def wait_for(predicate, *, timeout=45, interval=0.5, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}; last={last!r}")


# ---------------------------------------------------------------- manifests

def manifests():
    """base and head dbt manifests for the fixture pull request.

    The head adds a dependency on ``analytics.orders.customer_id``. That single
    added column is what makes production evidence required, and it is the
    column the producer degrades and then removes.
    """
    source = {
        "schema": fw.SCHEMA, "name": "orders", "database": "fixture_warehouse",
    }
    base = {
        "nodes": {"model.relium_e2e.fct_orders": {
            "resource_type": "model", "name": "fct_orders", "schema": fw.SCHEMA,
            "alias": "fct_orders", "database": "fixture_warehouse",
            "depends_on": {"nodes": ["source.relium_e2e.analytics.orders"]},
            "columns": {"order_id": {"name": "order_id"},
                        "amount": {"name": "amount"}}}},
        "sources": {"source.relium_e2e.analytics.orders": {
            **source, "columns": {"order_id": {}, "amount": {}}}},
    }
    head = {
        "nodes": {"model.relium_e2e.fct_orders": {
            "resource_type": "model", "name": "fct_orders", "schema": fw.SCHEMA,
            "alias": "fct_orders", "database": "fixture_warehouse",
            "depends_on": {"nodes": ["source.relium_e2e.analytics.orders"]},
            "columns": {"order_id": {"name": "order_id"},
                        "amount": {"name": "amount"},
                        "customer_id": {"name": "customer_id"}}}},
        "sources": {"source.relium_e2e.analytics.orders": {
            **source,
            "columns": {"order_id": {}, "amount": {}, "customer_id": {}}}},
    }
    return base, head


# -------------------------------------------------------------------- up

def command_up(args):
    from agent.api.auth import generate_token, hash_secret
    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    from agent.postgres_migrate import apply_migrations
    from agent.metadata_evidence.review_lifecycle import begin_review

    import psycopg

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {"started_at": now()}

    log("validating environment")
    if not _cluster_reachable():
        raise SystemExit(
            "the isolated PostgreSQL cluster is not reachable on 127.0.0.1:55433. "
            "Start it with: pg_ctl -D C:\\relium-e2e\\pgdata start")

    log("provisioning least-privileged roles")
    dsns = roles.provision()
    state["dsn_summary"] = {
        "app": "relium_app@127.0.0.1:55433/relium_lifecycle",
        "collector": "relium_collector@127.0.0.1:55433/fixture_warehouse",
        "producer": "fixture_producer@127.0.0.1:55433/fixture_warehouse",
    }

    # Migrations are DDL, so they run as the schema owner - not as the
    # application role, which deliberately holds DML only. The application
    # grants are re-applied afterwards because a grant cannot cover a table
    # that did not exist when it ran.
    log("applying migrations (as the schema owner)")
    with psycopg.connect(roles.owner_lifecycle_dsn(), autocommit=True,
                         row_factory=psycopg.rows.dict_row) as conn:
        applied = apply_migrations(conn)
    roles.grant_lifecycle_privileges()
    state["migrations_applied"] = applied

    log("seeding the fixture warehouse (phase 0 baseline)")
    baseline = fw.seed(dsns["producer_dsn"])
    roles.grant_collector_read(fw.SCHEMA)
    state["baseline_warehouse"] = baseline

    log("issuing tokens")
    store = PostgresLifecycleStore(dsns["app_dsn"])
    try:
        store.ensure_tenant(ORG, REPO, ENVIRONMENT)
        collector_id, collector_secret, collector_token = generate_token()
        store.create_service_token(collector_id, hash_secret(collector_secret),
                                   ORG, REPO, environment=ENVIRONMENT,
                                   description="live-e2e collector")
        dash_id, dash_secret, dash_token = generate_token()
        store.create_service_token(dash_id, hash_secret(dash_secret), ORG, REPO,
                                   environment=ENVIRONMENT,
                                   description="live-e2e dashboard")
        store.register_collector(ORG, REPO, ENVIRONMENT,
                                 collector_id="relium-e2e-collector",
                                 collector_version="live-e2e",
                                 adapter_type="postgres",
                                 description="live product E2E collector")
    finally:
        store.close()
    state["token_ids"] = {"collector": collector_id, "dashboard": dash_id}

    log(f"starting API on {API_URL}")
    api = _start_api(dsns["app_dsn"])
    state["api_pid"] = api.pid
    wait_for(lambda: _healthz(), timeout=60, what="API health")

    log("starting lifecycle worker (real worker process, recorded transports)")
    worker = _start_worker(dsns["app_dsn"], notify_warn=args.notify_warn)
    state["worker_pid"] = worker.pid

    log("creating the fixture review through the real lifecycle")
    base_manifest, head_manifest = manifests()
    head_sha = args.head_sha or ("e2e" + os.urandom(18).hex())[:40]
    store = PostgresLifecycleStore(dsns["app_dsn"])
    try:
        outcome = begin_review(
            store, organization_id=ORG, repository_id=REPO,
            environment=ENVIRONMENT, pull_number=args.pull_number,
            base_sha=("base" + os.urandom(18).hex())[:40], head_sha=head_sha,
            base_manifest=base_manifest, head_manifest=head_manifest,
            changed_models=["fct_orders"], enforcement_mode=args.enforcement_mode)
        # The waiting publication the webhook path would have made. Recording
        # it here is what gives republication a sticky identity to reconcile.
        store.record_review_publication(
            ORG, REPO, outcome.review_id,
            comment_id="900001", check_run_id="800001")
    finally:
        store.close()

    state.update({
        "review_id": outcome.review_id, "request_id": outcome.request_id,
        "pull_number": args.pull_number, "head_sha": head_sha,
        "enforcement_mode": args.enforcement_mode,
        "collector_token": collector_token, "dashboard_token": dash_token,
        "app_dsn": dsns["app_dsn"], "producer_dsn": dsns["producer_dsn"],
        "collector_warehouse_dsn": dsns["collector_warehouse_dsn"],
        "phase_index": 0, "timeline": [],
    })
    write_state(state)

    log(f"review {outcome.review_id} is {outcome.lifecycle_state} "
        f"(decision={outcome.decision!r})")
    _capture(state, phase="waiting", label="WAITING_FOR_METADATA",
             description="Review created; production metadata requested",
             warehouse=baseline, ran_collector=False)

    log("")
    log(f"  Dashboard token and API URL for relium-app:")
    log(f"    VITE_RELIUM_API_URL={API_URL}")
    log(f"    VITE_RELIUM_API_TOKEN=<written to {STATE_FILE.name}, not printed>")
    log("")
    log(f"  Next: python scripts/run_live_product_e2e.py phase 0")
    return 0


def _cluster_reachable():
    import psycopg
    try:
        with psycopg.connect("postgresql://relium_e2e@127.0.0.1:55433/postgres",
                             connect_timeout=5):
            return True
    except Exception:
        return False


def _healthz():
    try:
        with urllib.request.urlopen(f"{API_URL}/healthz", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _start_api(dsn):
    """The real served application, under uvicorn.

    The GitHub App settings are a throwaway local key: the app must be able to
    construct, but this run never contacts github.com. The webhook path is not
    exercised locally because the dedicated E2E App credentials are not
    available here.
    """
    key_path = STATE_DIR / "local-app-key.pem"
    if not key_path.is_file():
        _write_throwaway_key(key_path)

    launcher = (
        "import logging, uvicorn;"
        "logging.basicConfig(level=logging.INFO,"
        " format='%(levelname)s %(name)s %(message)s');"
        "from agent.github_app.server import build_application;"
        "from agent.github_app.settings import load_settings;"
        "import os;"
        "s=load_settings({"
        "'RELIUM_GITHUB_APP_ID': os.environ['RELIUM_GITHUB_APP_ID'],"
        "'RELIUM_GITHUB_WEBHOOK_SECRET': os.environ['RELIUM_GITHUB_WEBHOOK_SECRET'],"
        "'RELIUM_GITHUB_PRIVATE_KEY_PATH': os.environ['RELIUM_GITHUB_PRIVATE_KEY_PATH'],"
        "'RELIUM_STORAGE_ROOT': os.environ['RELIUM_STORAGE_ROOT'],"
        "'RELIUM_DATABASE_URL': os.environ['RELIUM_DATABASE_URL'],"
        "'RELIUM_CORS_ALLOWED_ORIGINS': os.environ['RELIUM_CORS_ALLOWED_ORIGINS'],"
        f"'RELIUM_PORT': '{API_PORT}', 'RELIUM_HOST': '127.0.0.1',"
        "'RELIUM_WORKER_COUNT': '2'});"
        f"uvicorn.run(build_application(s), host='127.0.0.1', port={API_PORT},"
        " log_level='info')"
    )
    env = {
        **os.environ,
        "RELIUM_DATABASE_URL": dsn,
        "RELIUM_STORAGE_ROOT": str(STATE_DIR / "storage"),
        "RELIUM_GITHUB_APP_ID": "424242",
        "RELIUM_GITHUB_WEBHOOK_SECRET": "local-e2e-webhook-secret",
        "RELIUM_GITHUB_PRIVATE_KEY_PATH": str(key_path),
        # The dashboard runs on its own origin, so the API must permit it
        # explicitly. Both spellings of localhost, because the dev server and
        # the browser do not always agree on which one they used.
        # Both dashboards: the restored original reference on APP_PORT and the
        # integrated build on APP_PORT + 1. Listed exactly, never wildcarded.
        "RELIUM_CORS_ALLOWED_ORIGINS": ",".join(
            f"http://{host}:{port}"
            for port in (APP_PORT, APP_PORT + 1)
            for host in ("localhost", "127.0.0.1")),
        "PYTHONPATH": str(REPO_ROOT),
    }
    handle = open(STATE_DIR / "api.log", "w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-c", launcher], env=env,
                               stdout=handle, stderr=subprocess.STDOUT,
                               cwd=str(REPO_ROOT))
    return process


def _write_throwaway_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))


def _start_worker(dsn, *, notify_warn):
    env = {**os.environ, "RELIUM_DATABASE_URL": dsn,
           "PYTHONPATH": str(REPO_ROOT)}
    command = [sys.executable, str(REPO_ROOT / "scripts" / "live_e2e" / "worker_main.py"),
               "--evidence", str(EVIDENCE), "--owner", OWNER, "--repository", REPO]
    if notify_warn:
        command.append("--notify-warn")
    handle = open(STATE_DIR / "worker.log", "w", encoding="utf-8")
    return subprocess.Popen(command, env=env, stdout=handle,
                            stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))


# ----------------------------------------------------------------- phases

def command_phase(args):
    state = read_state()
    if not state.get("review_id"):
        raise SystemExit("no live review; run `up` first")

    index = args.index if args.index is not None else state.get("phase_index", 0)
    if index >= len(PHASES):
        log("every phase has already run")
        return 0
    key, producer_phase, fn_name, description = PHASES[index]

    log(f"--- phase {key}: {description} ---")
    warehouse = _mutate(state, fn_name)
    log(f"    warehouse: rows={warehouse['row_count']} "
        f"null_rate={warehouse['customer_id_null_rate']} "
        f"customer_id={warehouse['customer_id_present']}")

    request_id = _ensure_collection_request(state)
    log(f"    collection request: {request_id}")

    log("    running the collector")
    collection = _collect(state, request_id)
    log(f"    collector: ok={collection['ok']} "
        f"snapshot={collection.get('snapshot_id')}")

    log("    waiting for the worker to recompute and republish")
    observed = _await_decision(state, collection.get("snapshot_id"))
    log(f"    review: attempt={observed['attempt']} "
        f"decision={observed['decision']} coverage={observed['coverage']} "
        f"health={observed['health']}")

    record = _capture(state, phase=key, label=description,
                      description=description, warehouse=warehouse,
                      collection=collection, ran_collector=True)
    state["phase_index"] = index + 1
    write_state(state)
    log(f"--- phase {key} complete ---")
    return 0


def _mutate(state, fn_name):
    fn = fw.PHASES[fn_name]
    result = fn(state["producer_dsn"])
    roles.grant_collector_read(fw.SCHEMA)
    return result


def _ensure_collection_request(state):
    """Make sure an actionable collection request exists for this review.

    The collector is request-driven: ``run_collection`` acts on a collection
    request and nothing else. ``begin_review`` raises exactly one, and it is
    CLOSED as soon as the first snapshot satisfies it — so a review whose
    production state later changes has nothing for a collector to act on.
    There is no scheduled re-collection in the product today.

    This is Relium's own control-plane action, using the product's own store
    method and the plan the product already computed. It requests evidence; it
    does not supply any. Every decision still comes from the engine, and the
    fixture producer — which cannot reach this database at all — is untouched
    by it.
    """
    from datetime import timedelta

    from agent.metadata_evidence.collection_plan import ttl_minutes_for
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    store = PostgresLifecycleStore(state["app_dsn"])
    try:
        existing = store.collection_requests_for_review(ORG, REPO, state["review_id"])
        actionable = [r for r in existing
                      if r["state"] in ("PENDING", "ACKNOWLEDGED")]
        if actionable:
            return actionable[0]["request_id"]

        review = store.get_review(ORG, REPO, state["review_id"])
        plan = (review.get("payload") or {}).get("plan") or {}
        targets = [t for t in plan.get("targets", [])
                   if t.get("dependency_kind") == "external"]
        criticality = "critical" if any(
            t.get("criticality") == "critical" for t in targets) else "standard"
        request_id = f"req-{state['review_id']}-refresh-{len(existing) + 1}"
        store.create_collection_request(
            ORG, REPO, ENVIRONMENT,
            request_id=request_id, review_id=state["review_id"],
            reason="scheduled_refresh",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            targets=targets,
            base_sha=review["base_sha"], head_sha=review["head_sha"],
            base_manifest_hash=review["base_manifest_hash"],
            head_manifest_hash=review["head_manifest_hash"],
            priority=criticality,
            required_evidence_level=plan.get("required_evidence_level", "profile"),
            plan={"attempt": review.get("attempt"),
                  "policy_version": review.get("policy_version"),
                  "policy_hash": review.get("policy_hash"),
                  "ttl_minutes": ttl_minutes_for(criticality),
                  "refresh": True})
        state["request_id"] = request_id
        write_state(state)
        return request_id
    finally:
        store.close()


def _collect(state, request_id):
    """Run the real collector against the real public API."""
    from agent.collector import CollectorConfig, run_collection

    config = CollectorConfig(
        api_url=API_URL, api_token=state["collector_token"],
        warehouse_dsn=state["collector_warehouse_dsn"],
        environment=ENVIRONMENT, collector_id="relium-e2e-collector",
        adapter_type="postgres")
    outcome = run_collection(config, request_id=request_id)
    doc = outcome.as_dict() if hasattr(outcome, "as_dict") else dict(outcome)
    if not doc.get("ok"):
        log(f"    collector FAILED: {doc.get('reason')}")
    return doc


def _await_decision(state, snapshot_id):
    """Wait until an attempt exists for THIS snapshot, then read it."""
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    store = PostgresLifecycleStore(state["app_dsn"])
    try:
        def check():
            attempts = store.review_attempts(ORG, REPO, state["review_id"])
            if snapshot_id:
                matched = [a for a in attempts if a.get("snapshot_id") == snapshot_id]
                if not matched:
                    return None
                return matched[-1]
            return attempts[-1] if attempts else None

        attempt = wait_for(check, timeout=90, what="recomputation for this snapshot")
        review = store.get_review(ORG, REPO, state["review_id"])
        return {
            "attempt": attempt["attempt"], "decision": attempt["decision"],
            "coverage": attempt["evidence_coverage"], "health": attempt["health"],
            "lifecycle_state": review["lifecycle_state"],
            "snapshot_id": attempt.get("snapshot_id"),
        }
    finally:
        store.close()


# ------------------------------------------------------------- capture

def _capture(state, *, phase, label, description, warehouse,
             collection=None, ran_collector=True):
    """Record every surface for one phase, and assert they agree."""
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    token = state["dashboard_token"]
    review_id = state["review_id"]

    store = PostgresLifecycleStore(state["app_dsn"])
    try:
        pg_review = store.get_review(ORG, REPO, review_id)
        pg_attempts = store.review_attempts(ORG, REPO, review_id)
        pg_transitions = store.review_transitions(ORG, REPO, review_id)
    finally:
        store.close()

    routes = {
        "review": f"/api/reviews/{review_id}",
        "findings": f"/api/reviews/{review_id}/findings",
        "attempts": f"/api/reviews/{review_id}/attempts",
        "coverage": f"/api/reviews/{review_id}/evidence-coverage",
        "collection_requests": f"/api/reviews/{review_id}/collection-requests",
        "snapshots": f"/api/reviews/{review_id}/snapshots",
        "publications": f"/api/reviews/{review_id}/publications",
        "review_list": "/api/reviews?limit=50",
    }
    api = {}
    for name, path in routes.items():
        status, body = api_get(path, token)
        api[name] = {"status": status, "body": body}
        write_evidence(f"dashboard-api/phase-{phase}-{name}.json",
                       {"path": path, "status": status, "body": body})

    latest = pg_attempts[-1] if pg_attempts else {}
    findings = (latest.get("payload") or {}).get("findings", [])

    github = _read_json(EVIDENCE / "github-publications.json")
    slack = _read_json(EVIDENCE / "slack-publications.json")

    consistency = _assert_consistency(pg_review, latest, api, github)

    record = {
        "phase": phase, "label": label, "description": description,
        "at": now(),
        "warehouse": warehouse,
        "collector": collection,
        "postgres": {
            "review_id": pg_review["review_id"],
            "attempt": pg_review.get("attempt"),
            "decision": pg_review.get("decision"),
            "evidence_coverage": pg_review.get("evidence_coverage"),
            "health": pg_review.get("health"),
            "lifecycle_state": pg_review.get("lifecycle_state"),
            "base_sha": pg_review.get("base_sha"),
            "head_sha": pg_review.get("head_sha"),
            "base_manifest_hash": pg_review.get("base_manifest_hash"),
            "head_manifest_hash": pg_review.get("head_manifest_hash"),
            "github_comment_id": pg_review.get("github_comment_id"),
            "github_check_run_id": pg_review.get("github_check_run_id"),
            "attempt_count": len(pg_attempts),
            "transition_count": len(pg_transitions),
        },
        "findings": findings,
        "api_status": {k: v["status"] for k, v in api.items()},
        "api_review": api["review"]["body"],
        "github_publication": {
            "transport": "recorded", "live_published": False,
            "call_count": github.get("call_count", 0),
            "last_check_conclusion": _last_check_conclusion(github),
            "sticky_comment_id": github.get("sticky_comment_id"),
            "check_run_id": github.get("check_run_id"),
        },
        "slack_publication": {
            "transport": "recorded", "live_published": False,
            "message_count": slack.get("message_count", 0),
        },
        "consistency": consistency,
    }

    write_evidence(f"phase-{phase}-capture.json", record)
    write_evidence(f"findings-{phase}.json", findings)
    write_evidence(f"decisions-{phase}.json", {
        "decision": pg_review.get("decision"),
        "attempt": pg_review.get("attempt"),
        "lifecycle_state": pg_review.get("lifecycle_state"),
    })
    write_evidence(f"coverage-{phase}.json", api["coverage"]["body"])

    timeline = state.setdefault("timeline", [])
    timeline.append({k: record[k] for k in
                     ("phase", "label", "at", "warehouse", "postgres",
                      "consistency")})
    write_state(state)
    write_evidence("lifecycle-timeline.json", timeline)
    return record


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _last_check_conclusion(github):
    for call in reversed(github.get("calls", [])):
        conclusion = (call.get("body") or {}).get("conclusion")
        if conclusion:
            return conclusion
    return None


def _assert_consistency(pg_review, latest_attempt, api, github):
    """The same review must not read differently on two surfaces."""
    api_review = api["review"]["body"]
    api_coverage = api["coverage"]["body"]
    checks = []

    def check(name, left, right):
        ok = left == right
        checks.append({"check": name, "ok": ok, "postgres": left, "api": right})
        return ok

    check("review_id", pg_review["review_id"], api_review.get("review_id"))
    check("attempt", pg_review.get("attempt"), api_review.get("attempt"))
    check("decision", pg_review.get("decision"), api_review.get("decision"))
    check("evidence_coverage", pg_review.get("evidence_coverage"),
          api_review.get("evidence_coverage"))
    check("health", pg_review.get("health"), api_review.get("health"))
    check("lifecycle_state", pg_review.get("lifecycle_state"),
          api_review.get("lifecycle_state"))
    check("base_sha", pg_review.get("base_sha"), api_review.get("base_sha"))
    check("head_sha", pg_review.get("head_sha"), api_review.get("head_sha"))
    check("head_manifest_hash", pg_review.get("head_manifest_hash"),
          api_review.get("head_manifest_hash"))

    # The per-review coverage route is a second, independent read path.
    checks.append({
        "check": "coverage_route_decision", "ok":
            api_coverage.get("decision") == pg_review.get("decision"),
        "postgres": pg_review.get("decision"), "api": api_coverage.get("decision")})

    # GitHub must carry the decision the store holds, once a decision exists.
    expected = _expected_conclusion(pg_review.get("decision"),
                                    pg_review.get("enforcement_mode"))
    actual = _last_check_conclusion(github)
    checks.append({
        "check": "github_check_conclusion",
        "ok": expected is None or actual == expected,
        "postgres": expected, "api": actual})

    failed = [c for c in checks if not c["ok"]]
    return {"all_agree": not failed, "checks": checks, "failed": failed}


def _expected_conclusion(decision, enforcement_mode):
    if decision is None:
        return None
    from agent.github_app.checks import conclusion_for_decision
    return conclusion_for_decision(decision,
                                   enforcement_mode=enforcement_mode or "shadow")


# ----------------------------------------------------------------- status

def command_status(_args):
    state = read_state()
    if not state.get("review_id"):
        log("no live review")
        return 0
    status, body = api_get(f"/api/reviews/{state['review_id']}",
                           state["dashboard_token"])
    log(f"review {state['review_id']} -> HTTP {status}")
    print(json.dumps(body, indent=2, sort_keys=True))
    log(f"phase index: {state.get('phase_index')} of {len(PHASES)}")
    return 0


# ------------------------------------------------------------------- all

def command_all(args):
    command_up(args)
    state = read_state()
    for index in range(len(PHASES)):
        args.index = index
        command_phase(args)
        time.sleep(args.dwell)
        state = read_state()
    _write_summary(state)
    return 0


def _write_summary(state):
    timeline = state.get("timeline", [])
    failures = [t for t in timeline if not t["consistency"]["all_agree"]]
    summary = {
        "review_id": state.get("review_id"),
        "pull_number": state.get("pull_number"),
        "enforcement_mode": state.get("enforcement_mode"),
        "phases": [{"phase": t["phase"], "label": t["label"],
                    "decision": t["postgres"]["decision"],
                    "attempt": t["postgres"]["attempt"],
                    "coverage": t["postgres"]["evidence_coverage"],
                    "health": t["postgres"]["health"],
                    "lifecycle_state": t["postgres"]["lifecycle_state"],
                    "rows": t["warehouse"]["row_count"],
                    "null_rate": t["warehouse"]["customer_id_null_rate"],
                    "customer_id_present": t["warehouse"]["customer_id_present"]}
                   for t in timeline],
        "consistency_failures": failures,
        "all_surfaces_agree": not failures,
        "github_transport": "recorded (not live-published)",
        "slack_transport": "recorded (not live-published)",
    }
    write_evidence("live-product-e2e-summary.json", summary)
    log("")
    log("phase summary:")
    for phase in summary["phases"]:
        log(f"  {phase['phase']:<4} {str(phase['decision']):<9} "
            f"attempt={phase['attempt']:<3} coverage={str(phase['coverage']):<11} "
            f"health={str(phase['health']):<5} rows={phase['rows']:<5} "
            f"null_rate={phase['null_rate']}")
    log(f"all surfaces agree: {summary['all_surfaces_agree']}")


# ------------------------------------------------------------------ down

def command_down(_args):
    """Cleanup. Safe to run repeatedly, and safe to run after a crash."""
    state = read_state()
    results = {}

    for name, key in (("worker", "worker_pid"), ("api", "api_pid")):
        pid = state.get(key)
        results[name] = _kill(pid) if pid else "not running"
        log(f"  {name}: {results[name]}")

    # Killing only the recorded PID is not enough. A worker restarted by hand,
    # or a parent/child pair where the recorded id is the wrong half, survives
    # and keeps claiming outbox jobs. Two workers from different code versions
    # ran concurrently once and produced a delivery journal that was half old
    # behaviour and half new — so cleanup now sweeps by command signature.
    results["orphans"] = _kill_matching("worker_main.py")
    log(f"  orphan workers: {results['orphans']}")
    results["orphan_api"] = _kill_matching("build_application")
    log(f"  orphan API processes: {results['orphan_api']}")

    results["relium_app"] = _kill_port(APP_PORT)
    log(f"  relium-app: {results['relium_app']}")

    # Revoke the tokens this run issued. They are tenant-scoped and local, but
    # a token that outlives its run is a token nobody is tracking.
    revoked = []
    if state.get("app_dsn") and state.get("token_ids"):
        try:
            from agent.postgres_lifecycle_store import PostgresLifecycleStore
            store = PostgresLifecycleStore(state["app_dsn"])
            try:
                for label, token_id in state["token_ids"].items():
                    if store.revoke_service_token(token_id):
                        revoked.append(label)
            finally:
                store.close()
        except Exception as exc:                       # pragma: no cover
            results["token_revocation_error"] = type(exc).__name__
    results["tokens_revoked"] = revoked
    log(f"  tokens revoked: {revoked or 'none'}")

    results["listeners"] = _listeners()
    log(f"  listeners remaining on E2E ports: {results['listeners'] or 'none'}")

    results["github"] = ("no fixture PR was created: the dedicated E2E App "
                         "credentials are not available locally")
    results["fixture_cluster"] = ("left running by design; stop with "
                                  "`pg_ctl -D C:\\relium-e2e\\pgdata stop`")

    if STATE_FILE.is_file():
        state["torn_down_at"] = now()
        state.pop("collector_token", None)
        state.pop("dashboard_token", None)
        state.pop("app_dsn", None)
        state.pop("producer_dsn", None)
        state.pop("collector_warehouse_dsn", None)
        write_state(state)
    write_evidence("cleanup.json", {"at": now(), **results})
    log("cleanup complete")
    return 0


def _kill(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return "already stopped"
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(int(pid), 0)
        except OSError:
            return "stopped"
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    return "force stopped"


def _kill_matching(needle):
    """Kill every python process whose command line contains ``needle``.

    Returns the pids it stopped. The recorded-PID path above is kept because
    it is the fast, precise one; this is the sweep that makes "no E2E process
    remains" true rather than merely likely.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return "sweep unavailable"
    pids = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=20)
    return pids or "none"


def _kill_port(port):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return "unknown"
    pids = {line.split()[-1] for line in out.splitlines()
            if f":{port} " in line and "LISTENING" in line}
    if not pids:
        return "not running"
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"],
                       capture_output=True, timeout=15)
    return f"stopped {len(pids)} process(es)"


def _listeners():
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return []
    return [line.strip() for line in out.splitlines()
            if ("LISTENING" in line
                and (f":{API_PORT} " in line or f":{APP_PORT} " in line))]


# ------------------------------------------------------------------ main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["up", "phase", "status", "down", "all"])
    parser.add_argument("index", nargs="?", type=int, default=None,
                        help="phase index for `phase`")
    parser.add_argument("--pull-number", type=int, default=101)
    parser.add_argument("--head-sha", default=None)
    parser.add_argument("--enforcement-mode", default="enforce",
                        choices=["enforce", "shadow"])
    parser.add_argument("--notify-warn", action="store_true",
                        help="configure the Slack sink to alert on WARN")
    parser.add_argument("--dwell", type=float, default=2.0,
                        help="seconds between phases in `all`")
    args = parser.parse_args(argv)

    handler = {"up": command_up, "phase": command_phase, "status": command_status,
               "down": command_down, "all": command_all}[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        log("interrupted; running cleanup")
        command_down(args)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
