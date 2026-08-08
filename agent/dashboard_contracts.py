DASHBOARD_RESOURCES = {
    "review_list": "/api/reviews",
    "review_detail": "/api/reviews/{review_id}",
    "deployment_list": "/api/deployments",
    "deployment_detail": "/api/deployments/{deployment_id}",
    "monitoring_status": "/api/monitoring",
    "anomaly_list": "/api/anomalies",
    "incident_detail": "/api/incidents/{incident_id}",
    "model_lineage": "/api/models/{model}/lineage",
    "kpi_impact": "/api/kpis/{kpi}/impact",
    "repository_settings": "/api/repositories/{repository}/settings",
    # Added by the public API release. The program requires dashboard reads for
    # RCA, evidence coverage and delivery status, which the original contract
    # named nowhere; the ten resources above are unchanged for compatibility.
    "incident_rca": "/api/incidents/{incident_id}/rca",
    "evidence_coverage": "/api/evidence-coverage",
    "delivery_status": "/api/delivery-status",
    # Added so the customer-facing dashboard can render a review's evidence
    # rather than only its verdict. Every one reads state the lifecycle store
    # already persisted.
    "review_rerun": "/api/reviews/{review_id}/rerun",
    "review_change_requests": "/api/reviews/{review_id}/change-requests",
    "review_exceptions": "/api/reviews/{review_id}/exceptions",
    "review_findings": "/api/reviews/{review_id}/findings",
    "review_attempts": "/api/reviews/{review_id}/attempts",
    "review_collection_requests": "/api/reviews/{review_id}/collection-requests",
    "review_snapshots": "/api/reviews/{review_id}/snapshots",
    "review_publications": "/api/reviews/{review_id}/publications",
    "review_evidence_coverage": "/api/reviews/{review_id}/evidence-coverage",
    "metadata_snapshot": "/api/metadata-snapshots/{snapshot_id}",
    "collection_request_detail": "/api/collection-requests/{request_id}",
}


def dashboard_contract(resource: str) -> dict:
    return {"resource": resource, "path": DASHBOARD_RESOURCES[resource], "tenant_scoped": True, "evidence_fields": ["status", "evidence_links", "coverage"]}
