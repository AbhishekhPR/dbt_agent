"""Deterministic RCA ranking; explanations may be layered on top but cannot alter it."""

from __future__ import annotations


_PRIORITY = {"INVARIANT_REMOVED": 100, "INVARIANT_CHANGED": 95, "UPSTREAM_DEPENDENCY_ADDED": 90, "UPSTREAM_DEPENDENCY_CHANGED": 85, "UPSTREAM_DEPENDENCY_REMOVED": 85, "CONTRACT_MEANING_CHANGED": 80, "ASSUMPTION_CHANGED": 80, "KPI_DEFINITION_CHANGED": 80, "MODEL_GRAIN_CHANGED": 80, "RELATED_COLUMN_CHANGED": 60, "RELATED_MODEL_CHANGED": 55, "METADATA_ANOMALY": 40, "GENERIC": 10}


def build_rca(*, anomaly: dict, deployments: list[dict], sql_findings: list[dict], lineage: dict) -> dict:
    model = anomaly.get("model") or anomaly.get("affected_model")
    detected_at = anomaly.get("detected_at")
    candidates = [deployment for deployment in deployments if model in deployment.get("models", []) and (detected_at is None or deployment.get("merge_time", 0) <= detected_at)]
    candidates.sort(key=lambda item: item.get("merge_time", 0), reverse=True)
    relevant_findings = [finding for finding in sql_findings if not finding.get("model") or finding.get("model") == model]
    ranked = sorted(relevant_findings, key=lambda item: _PRIORITY.get(item.get("finding_type", "GENERIC"), 0), reverse=True)
    evidence = [{"kind": "sql_finding", "finding_type": item.get("finding_type"), "description": item.get("description") or item.get("evidence"), "model": item.get("model", model)} for item in ranked]
    unevaluated = []
    if not candidates:
        unevaluated.append("deployment")
    model_lineage = lineage.get(model, {}) if isinstance(lineage, dict) else {}
    if not model_lineage:
        unevaluated.append("lineage")
    elif (model_lineage.get("completeness") or {}).get("column") == "incomplete":
        unevaluated.append("column lineage")
    primary = ranked[0].get("description") if ranked else "UNATTRIBUTED"
    causality_proven = bool(len(candidates) == 1 and ranked and evidence and detected_at is not None)
    if not candidates:
        primary = "UNATTRIBUTED"
    classification = "HIGH" if causality_proven and not unevaluated else "MEDIUM" if (candidates or ranked) else "LOW"
    alternatives = [{"deployment_id": item.get("deployment_id"), "cause": "Candidate deployment requires corroborating evidence."} for item in candidates]
    return {"incident_id": anomaly.get("incident_id"), "detection_timestamp": detected_at, "affected_model": model, "affected_downstream_models": model_lineage.get("downstream_models", []), "affected_kpis": anomaly.get("kpis", []), "most_likely_deployment": candidates[0].get("deployment_id") if candidates else None, "primary_root_cause": primary, "alternative_causes": alternatives, "evidence": evidence, "confidence": {"classification": classification, "score": 90 if classification == "HIGH" else 60 if classification == "MEDIUM" else 20, "reason": "Deterministic ranking with explicit missing evidence."}, "unevaluated_evidence": unevaluated, "causality": {"proven": causality_proven, "requirements": ["temporal ordering", "relevant deployment", "supporting evidence"]}, "remediation": "Review the primary evidence and verify the affected KPI before remediation.", "rollback_recommendation": bool(candidates), "verification_steps": ["Re-run the affected model", "Compare resulting-data metrics after remediation"], "status": "OPEN"}
