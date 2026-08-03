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
}


def dashboard_contract(resource: str) -> dict:
    return {"resource": resource, "path": DASHBOARD_RESOURCES[resource], "tenant_scoped": True, "evidence_fields": ["status", "evidence_links", "coverage"]}
