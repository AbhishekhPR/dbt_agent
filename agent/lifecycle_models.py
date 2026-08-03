from enum import Enum


class DeploymentState(str, Enum):
    REVIEWED = "reviewed"
    APPROVED = "approved"
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_SUCCEEDED = "deployment_succeeded"
    POST_DEPLOYMENT_MONITORING = "post_deployment_monitoring"
    HEALTHY = "healthy"
    DEPLOYMENT_FAILED = "deployment_failed"
    POST_DEPLOYMENT_ANOMALY = "post_deployment_anomaly"
    ROLLED_BACK = "rolled_back"
    INCIDENT_OPEN = "incident_open"
    INCIDENT_RESOLVED = "incident_resolved"


ALLOWED_TRANSITIONS = {
    "reviewed": {"approved"}, "approved": {"deployment_started"},
    "deployment_started": {"deployment_succeeded", "deployment_failed"},
    "deployment_succeeded": {"post_deployment_monitoring"},
    "post_deployment_monitoring": {"healthy", "post_deployment_anomaly"},
    "post_deployment_anomaly": {"rolled_back", "incident_open"},
    "rolled_back": {"post_deployment_monitoring", "incident_open"},
    "incident_open": {"incident_resolved", "rolled_back"},
    "deployment_failed": {"incident_open"}, "healthy": set(), "incident_resolved": set(),
}
