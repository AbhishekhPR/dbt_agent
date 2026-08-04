"""Production RCA execution path.

Loads tenant-scoped evidence, applies the existing deterministic ranking in
``agent.rca_engine``, and persists exactly one RCA result. No language model
selects the cause, creates evidence or raises confidence: this module only
gathers stored facts and records what the deterministic engine returns.

Causality requires timing, lineage, a relevant deployed change and
anomaly/effect agreement. A recent deployment on its own is never sufficient,
and insufficient evidence is persisted as UNATTRIBUTED rather than guessed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agent.rca_engine import build_rca

# Confidence is derived from the deterministic engine's classification, never
# raised because a narrative sounded convincing.
_CONFIDENCE = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


def _as_epoch(value):
    if isinstance(value, datetime):
        return value.timestamp()
    return value


def gather_evidence(store, organization_id, repository_id, environment, incident):
    """Collect every stored fact the deterministic engine is allowed to see."""
    anomaly = None
    if incident.get("anomaly_id"):
        anomaly = store.get_anomaly(organization_id, repository_id, incident["anomaly_id"])

    affected_models = list((anomaly or {}).get("affected_models") or [])
    model = affected_models[0] if affected_models else None

    # Candidate deployments: everything in scope, with the models each touched.
    page = store.list_deployments(organization_id, repository_id,
                                  environment=environment, limit=100, offset=0)
    deployments = []
    for record in page["items"]:
        payload = record.get("payload") or {}
        models = payload.get("models") or payload.get("changed_models") or []
        deployments.append({
            "deployment_id": record["deployment_id"],
            "models": list(models),
            "merge_time": _as_epoch(record.get("created_at")),
            "reviewed_sha": record.get("reviewed_sha"),
            "merge_sha": record.get("merge_sha"),
        })

    # Relevant deployed changes, as recorded on the deployment payloads.
    sql_findings = []
    for record in page["items"]:
        payload = record.get("payload") or {}
        for finding in payload.get("sql_findings") or []:
            if isinstance(finding, dict):
                sql_findings.append({**finding, "deployment_id": record["deployment_id"]})

    lineage = {}
    for snapshot in store.lineage_for_model(organization_id, repository_id, model or "",
                                            environment=environment) if model else []:
        payload = snapshot.get("payload") or {}
        lineage[model] = {
            "downstream_models": [e["downstream_model"] for e in snapshot.get("edges", [])
                                  if e["upstream_model"] == model],
            "completeness": {"column": snapshot.get("completeness") or "unknown"},
            **({} if not isinstance(payload, dict) else {"payload": payload}),
        }

    kpis = list((anomaly or {}).get("affected_kpis") or [])
    return {
        "anomaly": {
            "incident_id": incident["incident_id"],
            "model": model,
            "affected_model": model,
            "detected_at": _as_epoch((anomaly or {}).get("detected_at")
                                     or (anomaly or {}).get("created_at")),
            "kpis": kpis,
            "evidence": (anomaly or {}).get("payload") or {},
            "kind": (anomaly or {}).get("kind"),
        },
        "deployments": deployments,
        "sql_findings": sql_findings,
        "lineage": lineage,
    }


def run_rca(store, organization_id, repository_id, environment, incident_id):
    """Execute deterministic RCA for one incident and persist a single result.

    Idempotent: a completed report already present is returned unchanged, so a
    retry after a crash never produces a second one.
    """
    existing = [r for r in store.rca_for_incident(organization_id, repository_id, incident_id)
                if r["status"] == "completed"]
    if existing:
        return existing[0], False

    incident = store.get_incident(organization_id, repository_id, incident_id)
    if incident is None:
        raise LookupError("incident not found in scope")

    evidence = gather_evidence(store, organization_id, repository_id, environment, incident)
    result = build_rca(
        anomaly=evidence["anomaly"],
        deployments=evidence["deployments"],
        sql_findings=evidence["sql_findings"],
        lineage=evidence["lineage"],
    )

    attributed = result.get("most_likely_deployment")
    primary = result.get("primary_root_cause")
    causality_proven = bool((result.get("causality") or {}).get("proven"))

    # Attribution requires the deterministic engine to have proven causality.
    # A recent deployment alone leaves the incident UNATTRIBUTED.
    if primary == "UNATTRIBUTED" or not causality_proven:
        status = "unattributed"
        attributed = None
        primary_cause = None
    else:
        status = "completed"
        primary_cause = {"description": primary, "deployment_id": attributed}

    confidence = _CONFIDENCE.get((result.get("confidence") or {}).get("classification"), "low")
    lineage_entry = evidence["lineage"].get(evidence["anomaly"]["model"]) or {}
    completeness = (lineage_entry.get("completeness") or {}).get("column", "unknown")

    record = store.create_rca(
        incident_id, organization_id, repository_id, environment,
        status=status,
        primary_cause=primary_cause,
        alternative_causes=result.get("alternative_causes") or [],
        contributing_factors=result.get("evidence") or [],
        downstream_symptoms=[{"model": m} for m in result.get("affected_downstream_models") or []],
        unrelated_concurrent_changes=[
            {"deployment_id": d["deployment_id"]}
            for d in evidence["deployments"]
            if d["deployment_id"] != attributed
        ],
        confidence=confidence,
        unevaluated_evidence=[{"evidence": item} for item in result.get("unevaluated_evidence") or []],
        rca_id=str(uuid.uuid4()),
    )

    store.connection.execute(
        "UPDATE rca_reports SET attributed_deployment_id=%s, deployment_candidates=%s, "
        "affected_model=%s, downstream_models=%s, affected_kpis=%s, remediation=%s, "
        "rollback_recommendation=%s, verification_steps=%s, lineage_level=%s, "
        "lineage_completeness=%s, evidence_coverage=%s "
        "WHERE organization_id=%s AND repository_id=%s AND rca_id=%s",
        (
            attributed,
            store._Jsonb([d["deployment_id"] for d in evidence["deployments"]]),
            evidence["anomaly"]["model"],
            store._Jsonb(result.get("affected_downstream_models") or []),
            store._Jsonb(result.get("affected_kpis") or []),
            store._Jsonb([result.get("remediation")] if result.get("remediation") else []),
            "rollback" if result.get("rollback_recommendation") else "none",
            store._Jsonb(result.get("verification_steps") or []),
            "model" if evidence["lineage"] else "unknown",
            completeness,
            "COMPLETE" if not result.get("unevaluated_evidence") else "INCOMPLETE",
            organization_id, repository_id, record["rca_id"],
        ),
    )

    store.update_incident_status(
        organization_id, repository_id, incident_id,
        "investigating" if status == "completed" else "unattributed",
    )
    store.append_audit(
        organization_id, repository_id, actor="worker:rca",
        event_type=f"incident.rca_{status}", reference_type="incident",
        reference_id=incident_id,
        payload={"attributed_deployment_id": attributed, "confidence": confidence},
    )
    return store.get_rca(organization_id, repository_id, record["rca_id"]), True
