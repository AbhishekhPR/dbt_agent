"""The post-deployment incident RCA flow, exercised end to end.

Deliberately separate from the metadata review. This is the other half of the
product, and its question is a different one:

    review  : "is this proposed change safe to merge?"   (pre-merge, no deploy)
    incident: "which deployed change caused this break?" (post-deploy, observed)

Nothing here touches the review. No metadata-review finding is converted into
an anomaly, because a threshold crossing in evidence about production is not
an observed production failure, and pretending otherwise would make RCA look
wired when it is not.

Everything runs through the real public API and the real worker:

    POST /api/deployments/events   -> deployment exists
    POST /api/anomalies            -> an anomaly is observed
    POST /api/incidents            -> an incident is opened, RCA is queued
    worker                         -> agent.worker.rca_runtime.run_rca
    GET  /api/incidents/{id}/rca   -> the persisted result
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

API_URL = "http://127.0.0.1:8099"


def _call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = uuid.uuid4().hex
    request = urllib.request.Request(f"{API_URL}{path}", data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def run(token, *, dsn, out_path):
    """Drive the flow and return the persisted RCA."""
    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    now = datetime.now(timezone.utc)
    deployment_id = f"dep-{uuid.uuid4().hex[:10]}"
    model = "analytics.fct_orders"
    steps = []

    # -- 1. a deployment that actually changed the model ------------------
    # The engine attributes to a deployment only when it can prove the
    # deployment touched the affected model and preceded the anomaly.
    status, body = _call("POST", "/api/deployments/events", token, {
        "deployment_id": deployment_id, "event_type": "created",
        "deployment": {
            "merge_sha": "d" * 40, "reviewed_sha": "d" * 40,
            "models": [model],
            "sql_findings": [{
                "finding_type": "INVARIANT_REMOVED", "model": model,
                "description": "The de-duplication step was removed from "
                               "analytics.fct_orders.",
            }],
        },
    })
    steps.append({"step": "deployment", "status": status})
    if status not in (200, 201, 202):
        raise SystemExit(f"deployment event rejected: {status} {body}")

    # Lineage gives the engine a downstream path to reason about.
    store = PostgresLifecycleStore(dsn)
    try:
        store.record_lineage(
            "relium-e2e", "relium-e2e-dbt", "production", model,
            {"grain": "order_id"},
            edges=[(model, "analytics.rpt_revenue")], completeness="complete")
    finally:
        store.close()
    steps.append({"step": "lineage", "status": "recorded"})

    # -- 2. an observed post-deployment anomaly --------------------------
    status, anomaly = _call("POST", "/api/anomalies", token, {
        "deployment_id": deployment_id, "kind": "duplicate_explosion",
        "severity": "high",
        "detected_at": (now + timedelta(minutes=5)).isoformat(),
        "affected_models": [model], "affected_kpis": ["net_revenue"],
        "evidence": {"rows_before": 896, "rows_after": 2311,
                     "duplicate_rate": 0.61},
    })
    steps.append({"step": "anomaly", "status": status,
                  "anomaly_id": anomaly.get("anomaly_id")})
    if status not in (200, 201):
        raise SystemExit(f"anomaly rejected: {status} {anomaly}")

    # -- 3. open the incident, which queues RCA on the outbox ------------
    status, incident = _call("POST", "/api/incidents", token, {
        "anomaly_id": anomaly["anomaly_id"], "deployment_id": deployment_id,
    })
    steps.append({"step": "incident", "status": status,
                  "incident_id": incident.get("incident_id")})
    if status not in (200, 202):
        raise SystemExit(f"incident rejected: {status} {incident}")
    incident_id = incident["incident_id"]

    # -- 4. the real worker produces the RCA -----------------------------
    report = None
    deadline = time.time() + 90
    while time.time() < deadline:
        status, body = _call("GET", f"/api/incidents/{incident_id}/rca", token)
        if status == 200 and (body.get("rca") or body.get("rca_id")
                              or body.get("status")):
            report = body
            break
        time.sleep(1.0)
    steps.append({"step": "rca", "produced": report is not None})

    status, detail = _call("GET", f"/api/incidents/{incident_id}", token)

    document = {
        "note": ("Post-deployment incident RCA. This is NOT attached to any "
                 "metadata review, and no review finding was converted into "
                 "an anomaly to produce it."),
        "deployment_id": deployment_id,
        "anomaly_id": anomaly.get("anomaly_id"),
        "incident_id": incident_id,
        "steps": steps,
        "incident": detail,
        "rca": report,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=".live-e2e/state.json")
    parser.add_argument("--out", default="live-product-evidence/incident-rca.json")
    args = parser.parse_args(argv)

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    document = run(state["dashboard_token"], dsn=state["app_dsn"],
                   out_path=args.out)

    rca = document.get("rca") or {}
    print(json.dumps({
        "incident_id": document["incident_id"],
        "rca_status": rca.get("status"),
        "confidence": rca.get("confidence"),
        "attributed_deployment_id": rca.get("attributed_deployment_id"),
        "primary_cause": rca.get("primary_cause"),
        "affected_model": rca.get("affected_model"),
        "downstream_models": rca.get("downstream_models"),
        "remediation": rca.get("remediation"),
        "rollback_recommendation": rca.get("rollback_recommendation"),
    }, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
