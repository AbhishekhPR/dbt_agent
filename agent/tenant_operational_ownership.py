"""Read-only inventory for authoritative tenant operational ownership.

No function in this module accepts ownership assertions from an HTTP payload.
The customer-facing entry point refreshes Foundation 1 authorization and uses
only the tenant id in the resulting server-side context.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent.api.workspace_membership import WorkspaceAuthorizationContext


TENANT_OWNED_TABLES = (
    "tenants",
    "tenant_operational_roots",
    "tenant_memberships",
    "tenant_membership_sync_state",
    "tenant_onboarding_state",
    "tenant_billing",
    "billing_webhook_deliveries",
    "github_installation_states",
    "tenant_github_installations",
    "tenant_repositories",
    "tenant_repository_dbt_detection",
    "github_installations",
)

# These tables are deliberately not workspace-owned. They must be visible in
# the ownership model so later lifecycle work does not accidentally purge a
# shared user identity, an ephemeral OAuth flow, or migration history.
EXCLUDED_SHARED_TABLES = (
    "clerk_github_identities",
    "oauth_authorization_states",
    "schema_migrations",
)

LEGACY_OPERATIONAL_TABLES = (
    "organizations",
    "repositories",
    "environments",
    "api_service_tokens",
    "dashboard_sessions",
    "manifest_evidence",
    "reviews",
    "review_attempts",
    "review_change_requests",
    "review_evidence_coverage",
    "review_exceptions",
    "review_lifecycle_transitions",
    "collection_requests",
    "collection_request_targets",
    "collector_identities",
    "metadata_snapshots",
    "snapshot_relations",
    "snapshot_columns",
    "snapshot_metrics",
    "snapshot_review_bindings",
    "configuration_versions",
    "evidence",
    "metadata_baselines",
    "monitoring_observations",
    "deployments",
    "deployment_transitions",
    "anomalies",
    "incidents",
    "rca_reports",
    "rca_evidence_links",
    "lineage_records",
    "lineage_edges",
    "kpi_impact",
    "delivery_journal",
    "event_receipts",
    "outbox_events",
    "outbox_dead_letters",
    "audit_events",
    "retention_tombstones",
    "review_recomputation_jobs",
)


class TenantOperationalOwnershipUnavailable(Exception):
    """A trusted workspace context or complete ownership answer is absent."""


def inventory_for_current_workspace(store, *, principal, authorizer,
                                    repository_storage=None):
    """Return the current authenticated workspace's non-secret inventory."""
    context = authorizer.require_admin_or_owner(principal)
    if (not isinstance(context, WorkspaceAuthorizationContext)
            or context.ownership_status != "authoritative"
            or not context.tenant_id):
        raise TenantOperationalOwnershipUnavailable(
            "authoritative workspace context is required")
    inventory = store.tenant_operational_inventory(context.tenant_id)
    if repository_storage is not None:
        inventory["filesystem"] = filesystem_inventory(
            store, tenant_id=context.tenant_id,
            storage_root=repository_storage)
    return inventory


def filesystem_inventory(store, *, tenant_id, storage_root):
    """Count repository files without opening or returning any file content."""
    root = Path(storage_root).resolve()
    repository_ids = {
        int(row["github_repository_id"])
        for row in store.tenant_repositories(tenant_id)
    }
    owned_directories = 0
    owned_files = 0
    unmapped_directories = 0
    if root.exists() and root.is_dir():
        for candidate in root.iterdir():
            if (candidate.is_symlink() or not candidate.is_dir()
                    or not candidate.name.isdigit()
                    or int(candidate.name) <= 0):
                continue
            if int(candidate.name) in repository_ids:
                owned_directories += 1
                for current_directory, directories, files in os.walk(
                        candidate, followlinks=False):
                    directories[:] = [
                        name for name in directories
                        if not (Path(current_directory) / name).is_symlink()
                    ]
                    owned_files += len(files)
            else:
                unmapped_directories += 1
    return {
        "owned_repository_directories": owned_directories,
        "owned_files": owned_files,
        "unmapped_repository_directories": unmapped_directories,
    }


def filesystem_ownership_audit(store, *, storage_root):
    """Classify repository storage globally using persisted numeric IDs.

    Only directory and file counts are returned. Files are never opened and
    symlinked directories are not traversed.
    """
    root = Path(storage_root).resolve()
    mapped_repository_ids = {
        int(row["github_repository_id"])
        for row in store.connection.execute(
            "SELECT github_repository_id FROM tenant_repositories"
        ).fetchall()
    }
    mapped_directories = 0
    mapped_files = 0
    unmapped_directories = 0
    if root.exists() and root.is_dir():
        for candidate in root.iterdir():
            if (candidate.is_symlink() or not candidate.is_dir()
                    or not candidate.name.isdigit()
                    or int(candidate.name) <= 0):
                continue
            if int(candidate.name) not in mapped_repository_ids:
                unmapped_directories += 1
                continue
            mapped_directories += 1
            for current_directory, directories, files in os.walk(
                    candidate, followlinks=False):
                directories[:] = [
                    name for name in directories
                    if not (Path(current_directory) / name).is_symlink()
                ]
                mapped_files += len(files)
    return {
        "mapped_repository_directories": mapped_directories,
        "mapped_files": mapped_files,
        "unmapped_repository_directories": unmapped_directories,
    }
